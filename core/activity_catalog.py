# -*- coding: utf-8 -*-
"""活动目录：占槽耗时活动的唯一定义处。

为什么要有这张表
----------------
身外化身之后，一个玩家可以同时挂 N 项耗时活动（本体 + 化身各占一槽）。
「槽位怎么占」早就统一在 ``user_cd`` / ``start_activity`` 上了，但「什么算一项
活动」没有统一登记处——枚举、文案、忙碌白名单、重伤白名单、后台结算循环、
自动代理任务表各写一份。加一项新活动要挨个对齐 9 个地方，其中「忙碌白名单」
和「重伤白名单」漏登记的后果是静默的（玩家卡死、或重伤照样开跑），不报错。

这个模块把「一项活动是什么」收成一条 :class:`ActivitySpec`，上层表从它派生：

    exit_commands   → BUSY_STATE_ALLOWED_COMMANDS / BUSY_BYPASS_COMMANDS
    action_commands → INJURED_BLOCKED_COMMANDS
    proxy_mode      → TASK_STATUS / TASK_NAMES

只读，不做执行
--------------
本模块**只放静态描述**，不放结算回调。回调如果塞进模块级常量，``handlers/utils``
就得在导入期去拿 manager 实例，绕成循环依赖。结算调度（S4）通过 ``main.py``
里的适配器绑定，见 docs/身外化身活动接入改造方案.md。

依赖方向
--------
本模块只依赖 ``models_extended``，可以被任何层安全导入。
状态文案的唯一来源是 ``UserStatus.get_name``，这里不重新定义。

什么不进这张表
--------------
- 维护动作：施肥、灵田、坊市补货 —— 不占槽、不进 ``user_cd``，归自动代理的
  ``config_json`` 管，所以在 ``TASK_NAMES`` 里是手写例外。
- 瞬时杂务：炼丹、逛坊市、服用丹药 —— 瞬间完成，不占槽。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from ..models_extended import UserStatus

# proxy_mode 取值
PROXY_NONE = "none"      # 没有自动代理
PROXY_CONFIG = "config"  # 代理靠 config_json 里的开关（秘境 / 历练），不进 TASK_STATUS
PROXY_TASK = "task"      # 作为「/自动代理 <名称>」的主任务，进 TASK_STATUS

_VALID_PROXY_MODES = (PROXY_NONE, PROXY_CONFIG, PROXY_TASK)


@dataclass(frozen=True)
class ActivitySpec:
    """一项占槽耗时活动的描述。

    :param status: ``user_cd.type`` 的取值，活动的唯一身份
    :param key: 与存量数据对齐的英文键（``auto_proxy.task_type`` 的字面量）
    :param name: 中文名，用于提示与任务表
    :param exit_commands: 退出/完成类指令，自动进忙碌白名单
    :param action_commands: 开始/进入类指令，自动进重伤禁行名单
    :param proxy_mode: 自动代理接管方式，见上方 ``PROXY_*``
    """

    status: int
    key: str
    name: str
    exit_commands: Tuple[str, ...] = ()
    action_commands: Tuple[str, ...] = ()
    proxy_mode: str = PROXY_NONE


CULTIVATION = ActivitySpec(
    status=int(UserStatus.CULTIVATING),
    key="cultivation",
    name="闭关",
    exit_commands=("出关",),
    action_commands=("闭关",),
)

ADVENTURE = ActivitySpec(
    status=int(UserStatus.ADVENTURING),
    key="adventure",
    name="历练",
    exit_commands=("完成历练", "退出历练", "放弃历练"),
    action_commands=("开始历练",),
    proxy_mode=PROXY_CONFIG,
)

RIFT = ActivitySpec(
    status=int(UserStatus.EXPLORING),
    key="rift",
    name="秘境",
    exit_commands=("完成探索", "退出秘境"),
    # 秘境有「探索秘境」与旧名「进入秘境」两条入口，重伤时都要拦
    action_commands=("探索秘境", "进入秘境"),
    proxy_mode=PROXY_CONFIG,
)

SECT_TASK = ActivitySpec(
    status=int(UserStatus.SECT_TASK),
    key="sect_task",
    name="宗门任务",
    exit_commands=("完成宗门任务", "放弃宗门任务"),
    action_commands=("接取宗门任务", "宗门任务"),
    proxy_mode=PROXY_TASK,
)

ESCORT = ActivitySpec(
    status=int(UserStatus.DELIVERING),
    key="escort",
    name="押镖",
    exit_commands=("完成押镖", "放弃押镖"),
    action_commands=("押镖",),
    proxy_mode=PROXY_TASK,
)

RESTING = ActivitySpec(
    status=int(UserStatus.RESTING),
    key="resting",
    name="调息",
    exit_commands=("收功",),
    # 调息本身是恢复手段，重伤时不禁
    action_commands=(),
)

CATALOG: Dict[int, ActivitySpec] = {
    spec.status: spec
    for spec in (CULTIVATION, ADVENTURE, RIFT, SECT_TASK, ESCORT, RESTING)
}


def _validate(catalog: Dict[int, ActivitySpec]) -> None:
    """构建期自检。宁可启动就炸，也不要带着错表静默跑。"""
    seen_keys: Dict[str, int] = {}
    for status, spec in catalog.items():
        if int(spec.status) != int(status):
            raise ValueError(
                f"活动目录的 key({status}) 与 spec.status({spec.status}) 不一致：{spec.key}"
            )
        if spec.key in seen_keys:
            raise ValueError(f"活动 key 重复：{spec.key}")
        seen_keys[spec.key] = status
        if spec.proxy_mode not in _VALID_PROXY_MODES:
            raise ValueError(f"{spec.key} 的 proxy_mode 非法：{spec.proxy_mode}")
        for cmd in (*spec.exit_commands, *spec.action_commands):
            if not cmd or cmd != cmd.strip():
                raise ValueError(f"{spec.key} 的指令名不能为空或带空白：{cmd!r}")

    # BUSY_BYPASS_COMMANDS 走 startswith 宽匹配，一条退出指令若是另一条的前缀，
    # 会把本该被拦住的操作整片放出去。
    exits = [c for spec in catalog.values() for c in spec.exit_commands]
    for short in exits:
        for long in exits:
            if short != long and long.startswith(short):
                raise ValueError(
                    f"退出指令 {short!r} 是 {long!r} 的前缀，会被 startswith 误放行"
                )


_validate(CATALOG)


def spec_of(status: int) -> ActivitySpec:
    """按 ``user_cd.type`` 取活动定义；未登记的返回 None。"""
    return CATALOG.get(int(status))


def all_exit_commands() -> Tuple[str, ...]:
    """所有活动的退出/完成指令（去重、保持登记顺序）。"""
    return tuple(
        dict.fromkeys(c for spec in CATALOG.values() for c in spec.exit_commands)
    )


def all_action_commands() -> Tuple[str, ...]:
    """所有活动的开始/进入指令（去重、保持登记顺序）。"""
    return tuple(
        dict.fromkeys(c for spec in CATALOG.values() for c in spec.action_commands)
    )


def task_status() -> Dict[str, int]:
    """``auto_proxy.task_type`` → ``user_cd.type``，只含可作主任务的代理。

    秘境 / 历练是 ``PROXY_CONFIG``，靠 ``config_json`` 的开关驱动，**不能**进这张表：
    ``AutoProxyManager._active_primary`` 会遍历它去查槽位，多出这两项就会把历练 /
    秘境当成押镖一样逐轮结算，等于重复结算。
    """
    return {
        spec.key: spec.status
        for spec in CATALOG.values()
        if spec.proxy_mode == PROXY_TASK
    }


def task_names() -> Dict[str, str]:
    """``auto_proxy.task_type`` → 中文名。

    只覆盖进目录的活动；灵田 / 施肥是维护动作（不占槽），由调用方手写补上。
    """
    return {
        spec.key: spec.name
        for spec in CATALOG.values()
        if spec.proxy_mode in (PROXY_TASK, PROXY_CONFIG)
    }
