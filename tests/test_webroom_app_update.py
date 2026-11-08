# -*- coding: utf-8 -*-
"""App 应用内更新：版本清单接口与安装包分发。

页面本身走远程 URL 改完即生效，这条链路只服务「原生层变了必须重装 APK」
那一种情况，所以重点回归四件事：
1. 清单缺失 / 损坏 / 指向不存在的包时不要骗客户端去下载；
2. changelog、minVersionCode 这类可选字段要有稳定默认值；
3. 分发时带上正确的 MIME（Android 安装器认这个）与 Range 能力；
4. 路径穿越和非 APK 后缀必须被挡住。

这里直接起一个真实的 aiohttp 服务再用 client 请求，而不是用 TestClient：
本地 aiohttp 3.14 的 TestClient 在构造时就会炸，绕开它更省事也离线上更近。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_astrbot():
    """没有装 AstrBot 时用开发脚本里的最小桩，让接口逻辑可以离线回归。"""
    try:
        # 其它测试可能只注册了顶层 astrbot 桩，须确认本接口实际依赖的子模块。
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # noqa: F401
        return
    except Exception:
        pass
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import dev_webroom

    dev_webroom._install_astrbot_stubs()
    # 测试桩会被本进程的其它用例复用，不能指向开发预览的持久化账号目录。
    test_data_dir = tempfile.mkdtemp(prefix="xiuxian_appupd_boot_")
    sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: test_data_dir


_ensure_astrbot()

try:
    import aiohttp
    from aiohttp import web

    from webchat_room import room_server as rs
except Exception as exc:  # 没装依赖时跳过，不拖累其他用例
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class AppUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_appupd_"))
        self.apk_dir = self.tmp / "apk"
        self.apk_dir.mkdir()
        self.manifest = self.tmp / "app_release.json"

        self._orig = (rs._apk_dir, rs._release_manifest_path)
        rs._apk_dir = lambda: self.apk_dir
        rs._release_manifest_path = lambda: self.manifest

        app = web.Application()
        app.router.add_get("/api/app/version", rs._api_app_version)
        app.router.add_get("/download/{name}", rs._api_apk)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        rs._apk_dir, rs._release_manifest_path = self._orig

    # ---- 辅助 ----

    def _write_apk(self, name="feiyu-1.1.apk", payload=b"PK\x03\x04fake-apk"):
        path = self.apk_dir / name
        path.write_bytes(payload)
        return path

    def _write_manifest(self, **overrides):
        data = {
            "versionCode": 2,
            "versionName": "1.1",
            "file": "feiyu-1.1.apk",
            "sha256": "a" * 64,
            "changelog": ["修复状态栏遮挡", "新增 K 线"],
            "minVersionCode": 0,
            "publishedAt": "2026-11-06T10:00:00+08:00",
        }
        data.update(overrides)
        self.manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    async def _version(self):
        async with self.session.get(self.base + "/api/app/version") as r:
            return r.status, await r.json()

    # ---- /api/app/version ----

    async def test_missing_manifest_is_404(self):
        status, body = await self._version()
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    async def test_manifest_without_apk_is_404(self):
        # 清单写了但包没上传：宁可 404，也不要让客户端弹更新把用户引到死链
        self._write_manifest()
        status, body = await self._version()
        self.assertEqual(status, 404)
        self.assertIn("安装包", body["error"])

    async def test_broken_manifest_is_500(self):
        self.manifest.write_text("{not json", encoding="utf-8")
        status, _body = await self._version()
        self.assertEqual(status, 500)

    async def test_manifest_missing_version_code_is_500(self):
        self._write_apk()
        self._write_manifest(versionCode=0)
        status, body = await self._version()
        self.assertEqual(status, 500)
        self.assertIn("versionCode", body["error"])

    async def test_version_payload(self):
        self._write_apk(payload=b"x" * 1234)
        self._write_manifest(minVersionCode=1)
        status, body = await self._version()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["versionCode"], 2)
        self.assertEqual(body["versionName"], "1.1")
        self.assertEqual(body["size"], 1234)
        self.assertEqual(body["url"], "/download/feiyu-1.1.apk")
        self.assertEqual(body["sha256"], "a" * 64)
        self.assertEqual(body["minVersionCode"], 1)
        self.assertEqual(body["changelog"], ["修复状态栏遮挡", "新增 K 线"])

    async def test_changelog_string_becomes_list(self):
        self._write_apk()
        self._write_manifest(changelog="只有一条说明")
        _status, body = await self._version()
        self.assertEqual(body["changelog"], ["只有一条说明"])

    async def test_optional_fields_have_defaults(self):
        self._write_apk()
        self._write_manifest(changelog=None, sha256=None, minVersionCode=None)
        _status, body = await self._version()
        self.assertEqual(body["changelog"], [])
        self.assertEqual(body["sha256"], "")
        self.assertEqual(body["minVersionCode"], 0)  # 0 = 只提示不强推

    # ---- /download ----

    async def test_download_apk(self):
        payload = b"PK\x03\x04" + b"y" * 500
        self._write_apk(payload=payload)
        async with self.session.get(self.base + "/download/feiyu-1.1.apk") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(
                r.headers["Content-Type"], "application/vnd.android.package-archive"
            )
            self.assertIn("feiyu-1.1.apk", r.headers["Content-Disposition"])
            self.assertEqual(await r.read(), payload)

    async def test_download_supports_range(self):
        # 断点续传靠这个；安装包按文件名带版本，内容不会原地变
        payload = bytes(range(256)) * 4
        self._write_apk(payload=payload)
        async with self.session.get(
            self.base + "/download/feiyu-1.1.apk", headers={"Range": "bytes=0-99"}
        ) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(await r.read(), payload[:100])

    async def test_download_missing_file_is_404(self):
        async with self.session.get(self.base + "/download/nope.apk") as r:
            self.assertEqual(r.status, 404)

    async def test_download_rejects_non_apk(self):
        (self.apk_dir / "notes.txt").write_text("hi", encoding="utf-8")
        async with self.session.get(self.base + "/download/notes.txt") as r:
            self.assertEqual(r.status, 400)

    async def test_download_case_insensitive_suffix(self):
        self._write_apk(name="Feiyu-1.1.APK", payload=b"ok")
        async with self.session.get(self.base + "/download/Feiyu-1.1.APK") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(await r.read(), b"ok")
