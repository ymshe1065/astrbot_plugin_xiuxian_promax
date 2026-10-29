"""按 asyncio 任务隔离的 SQLite 连接路由。

共享单连接时，两个任务各自执行 ``BEGIN IMMEDIATE`` 会交错到同一条连接上，
导致 "cannot start a transaction within a transaction"，甚至一个任务提交/回滚
另一个任务的事务。本模块让每个 asyncio 任务使用独立连接，并在数据库层通过
WAL + busy_timeout 串行化写入。

同时提供与连接绑定的“事务状态机”：
- 事务内再次 ``BEGIN`` 自动降级为 ``SAVEPOINT``，内部 ``COMMIT``/``ROLLBACK``
  只影响自己的保存点，不会提交/回滚外层事务；
- ``USING SAVEPOINT`` 让现有 50 处显式事务代码无需逐处改造即可安全嵌套；
- 连接工作在 autocommit 模式，避免隐式只读事务长期持有旧快照。
"""

import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite

from astrbot.api import logger

__all__ = ["TaskConnectionRouter", "configure_connection"]

_BEGIN_RE = re.compile(r"^\s*BEGIN\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^\s*(COMMIT|END)\b", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*ROLLBACK\b(?!\s+TO\b)", re.IGNORECASE)
_BUSY_MARKERS = ("locked", "busy")


class _TxState:
    """单个连接上的事务深度。0 表示空闲，1 表示真实事务，>1 为保存点层。"""

    __slots__ = ("depth",)

    def __init__(self) -> None:
        self.depth = 0

    @property
    def active(self) -> bool:
        return self.depth > 0


def _savepoint_name(depth: int) -> str:
    return f"xiuxian_sp_{depth}"


async def _begin_immediate(conn: aiosqlite.Connection, retries: int = 5) -> None:
    """获取写事务；被其他连接占用时等待并重试。"""
    last_error: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            await conn.execute("BEGIN IMMEDIATE")
            return
        except (sqlite3.OperationalError, aiosqlite.Error) as e:
            last_error = e
            if not any(marker in str(e).lower() for marker in _BUSY_MARKERS):
                raise
            if attempt < retries - 1:
                await asyncio.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


async def _begin(conn: aiosqlite.Connection, state: _TxState) -> None:
    if state.depth == 0:
        # autocommit 模式下理论上不会残留事务；若因外部代码开启过，先结束它。
        if getattr(conn, "in_transaction", False):
            try:
                await conn.commit()
            except Exception:
                await conn.rollback()
        await _begin_immediate(conn)
        state.depth = 1
        return
    await conn.execute(f"SAVEPOINT {_savepoint_name(state.depth)}")
    state.depth += 1


async def _commit(conn: aiosqlite.Connection, state: _TxState) -> None:
    if state.depth <= 1:
        await conn.commit()
        state.depth = 0
        return
    name = _savepoint_name(state.depth - 1)
    await conn.execute(f"RELEASE {name}")
    state.depth -= 1


async def _rollback(conn: aiosqlite.Connection, state: _TxState) -> None:
    if state.depth <= 1:
        await conn.rollback()
        state.depth = 0
        return
    name = _savepoint_name(state.depth - 1)
    try:
        await conn.execute(f"ROLLBACK TO {name}")
        await conn.execute(f"RELEASE {name}")
    finally:
        state.depth -= 1


async def _execute_raw(conn, sql: str, parameters: Any, state: Optional[_TxState]):
    """执行 SQL；事务控制语句交给状态机，嵌套时自动降级为保存点。"""
    if state is not None:
        if _BEGIN_RE.match(sql):
            await _begin(conn, state)
            return await conn.execute("SELECT 1")
        if _COMMIT_RE.match(sql):
            await _commit(conn, state)
            return await conn.execute("SELECT 1")
        if _ROLLBACK_RE.match(sql):
            await _rollback(conn, state)
            return await conn.execute("SELECT 1")
    if parameters is None:
        return await conn.execute(sql)
    return await conn.execute(sql, parameters)


class _CursorProxy:
    """兼容 aiosqlite 的 ``await conn.execute(...)`` 与 ``async with conn.execute(...)``。"""

    __slots__ = ("_factory", "_cursor")

    def __init__(self, factory):
        self._factory = factory
        self._cursor = None

    async def _resolve(self):
        if self._cursor is None:
            self._cursor = await self._factory()
        return self._cursor

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self):
        return await self._resolve()

    async def __aexit__(self, exc_type, exc, tb):
        cursor = await self._resolve()
        await cursor.close()
        return False


class TaskConnectionRouter:
    """把 ``db.conn`` 的调用路由到当前任务自己的 aiosqlite 连接。

    - 无任务上下文（同步回调/启动阶段）使用默认连接。
    - 每个任务首次访问时惰性创建连接，任务结束时自动回滚并关闭。
    - 所有连接共享 row_factory、WAL、busy_timeout 与外键设置。
    """

    def __init__(self, db_path: Path, default_conn: aiosqlite.Connection):
        self._path = str(db_path)
        self._default = default_conn
        self._conns: Dict[int, aiosqlite.Connection] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._states: Dict[int, _TxState] = {}
        self._creating = asyncio.Lock()
        self._closed = False
        self._row_factory = aiosqlite.Row

    # ---------- 兼容原有 conn 属性 ----------
    @property
    def default_connection(self) -> aiosqlite.Connection:
        return self._default

    @property
    def _connection(self):
        """兼容 DataBase._connection_alive 的底层连接探测。"""
        return getattr(self._default, "_connection", None)

    @property
    def alive(self) -> bool:
        return getattr(self._default, "_connection", None) is not None

    @property
    def in_transaction(self) -> bool:
        task = self._current_task()
        state = self._states.get(id(task) if task is not None else 0)
        if state is not None and state.active:
            return True
        conn = self._conns.get(id(task)) if task is not None else self._default
        return bool(conn.in_transaction) if conn is not None else False

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._row_factory = value
        if self._default is not None:
            self._default.row_factory = value

    # ---------- 连接获取 ----------
    @staticmethod
    def _current_task() -> Optional[asyncio.Task]:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    async def _open(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = self._row_factory
        # journal_mode 已在默认连接中设置；这里只设置连接级 PRAGMA。
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _state(self) -> _TxState:
        task = self._current_task()
        key = id(task) if task is not None else 0
        state = self._states.get(key)
        if state is None:
            state = _TxState()
            self._states[key] = state
        return state

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._closed:
            raise RuntimeError("数据库连接已关闭")
        task = self._current_task()
        if task is None:
            return self._default
        conn = self._conns.get(id(task))
        if conn is not None:
            return conn
        async with self._creating:
            conn = self._conns.get(id(task))
            if conn is not None:
                return conn
            conn = await self._open()
            self._conns[id(task)] = conn
            self._tasks[id(task)] = task
            self._state()
            task.add_done_callback(self._on_task_done)
            return conn

    async def current_connection(self) -> aiosqlite.Connection:
        """返回当前任务使用的原始连接（供事务 API 使用）。"""
        return await self._get_conn()

    def _on_task_done(self, task: asyncio.Task) -> None:
        key = id(task)
        conn = self._conns.pop(key, None)
        self._tasks.pop(key, None)
        self._states.pop(key, None)
        if conn is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(self._close_quietly(conn))
        except RuntimeError:
            # 事件循环正在关闭，连接交给进程退出回收
            pass

    @staticmethod
    async def _close_quietly(conn: aiosqlite.Connection) -> None:
        try:
            await conn.rollback()
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass

    # ---------- 连接代理 ----------
    def execute(self, sql: str, parameters: Any = None):
        return _CursorProxy(lambda: self._execute(sql, parameters))

    def executemany(self, sql: str, parameters):
        return _CursorProxy(lambda: self._executemany(sql, parameters))

    async def _execute(self, sql: str, parameters: Any):
        conn = await self._get_conn()
        return await _execute_raw(conn, sql, parameters, self._state())

    async def _executemany(self, sql: str, parameters):
        conn = await self._get_conn()
        return await conn.executemany(sql, parameters)

    async def executescript(self, sql: str):
        conn = await self._get_conn()
        state = self._state()
        try:
            return await conn.executescript(sql)
        finally:
            # sqlite 的 executescript 会隐式提交当前事务，状态需要同步归零。
            if state.active:
                logger.warning("[database] executescript 隐式提交了事务，已重置事务状态")
                state.depth = 0

    async def cursor(self):
        conn = await self._get_conn()
        return await conn.cursor()

    async def commit(self):
        conn = await self._get_conn()
        await _commit(conn, self._state())

    async def rollback(self):
        conn = await self._get_conn()
        await _rollback(conn, self._state())

    @asynccontextmanager
    async def transaction(self):
        """显式事务上下文，可安全嵌套（内层退化为保存点）。"""
        conn = await self._get_conn()
        state = self._state()
        await _begin(conn, state)
        try:
            yield conn
        except BaseException:
            try:
                await _rollback(conn, state)
            except Exception as e:
                logger.error(f"[database] 回滚事务失败: {e}")
                state.depth = 0
            raise
        else:
            try:
                await _commit(conn, state)
            except Exception:
                try:
                    await conn.rollback()
                finally:
                    state.depth = 0
                raise

    async def close(self):
        self._closed = True
        conns = list(self._conns.values())
        self._conns.clear()
        self._tasks.clear()
        self._states.clear()
        if self._default is not None:
            conns.append(self._default)
        for conn in conns:
            await self._close_quietly(conn)

    def __getattr__(self, name: str):
        # 未显式代理的属性（如 total_changes）回退到默认连接。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._default, name)


async def configure_connection(conn: aiosqlite.Connection) -> None:
    """为默认连接应用与任务连接一致的 PRAGMA。"""
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        logger.warning(f"[database] 初始化连接 PRAGMA 失败: {e}")
