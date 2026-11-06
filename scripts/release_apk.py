#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把打好的 APK 发布成「应用内更新」可下发的版本。

用法（在插件根目录执行）：

    python scripts/release_apk.py
    python scripts/release_apk.py --note "修复状态栏遮挡" --note "新增 K 线"
    python scripts/release_apk.py --apk app/android/app/build/outputs/apk/release/app-release.apk --data-dir /srv/astrbot/data

它做三件事：
1. 从 app/android/app/build.gradle 读出 versionCode / versionName（不手抄，避免对不上）；
2. 把 APK 复制到 <数据目录>/webroom/apk/ 下，按版本命名；
3. 重写 <数据目录>/webroom/app_release.json，算出 size 与 sha256。

之后 Web 端 /api/app/version 就会把新版本下发给旧版 App。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APK = ROOT / "app" / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
GRADLE_FILE = ROOT / "app" / "android" / "app" / "build.gradle"


def read_gradle_version() -> tuple[int, str]:
    """从 build.gradle 的 defaultConfig 里取版本，取不到就让调用方补。"""
    text = GRADLE_FILE.read_text(encoding="utf-8")
    code = re.search(r"versionCode\s+(\d+)", text)
    name = re.search(r'versionName\s+"([^"]+)"', text)
    if not code or not name:
        raise SystemExit(f"没能从 {GRADLE_FILE} 读到 versionCode/versionName")
    return int(code.group(1)), name.group(1)


def data_dir(explicit: str | None) -> Path:
    """数据目录默认取 AstrBot 的 data 路径，没装 AstrBot 时用开发桩兜底。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:
        pass
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import dev_webroom

    dev_webroom._install_astrbot_stubs()
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 APK 到应用内更新通道")
    parser.add_argument("--apk", default=str(DEFAULT_APK), help="APK 路径")
    parser.add_argument("--data-dir", default=None, help="AstrBot 数据目录（默认自动探测）")
    parser.add_argument("--note", action="append", default=[], help="更新说明，可重复")
    parser.add_argument("--min-version-code", type=int, default=0,
                        help="低于此版本的客户端强制更新；0 = 只提示")
    parser.add_argument("--keep-notes", action="store_true",
                        help="沿用清单里已有的更新说明，不覆盖")
    args = parser.parse_args()

    apk = Path(args.apk).expanduser().resolve()
    if not apk.is_file():
        raise SystemExit(f"找不到 APK：{apk}\n先跑 cd app/android && ./gradlew assembleRelease")

    version_code, version_name = read_gradle_version()
    base = data_dir(args.data_dir)
    apk_dir = base / "webroom" / "apk"
    apk_dir.mkdir(parents=True, exist_ok=True)
    manifest = base / "webroom" / "app_release.json"

    notes = list(args.note)
    if not notes and args.keep_notes and manifest.is_file():
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
            notes = [str(x) for x in (old.get("changelog") or [])]
        except Exception:
            notes = []

    file_name = f"feiyu-{version_name}.apk"
    target = apk_dir / file_name
    shutil.copyfile(apk, target)

    data = {
        "versionCode": version_code,
        "versionName": version_name,
        "file": file_name,
        "size": target.stat().st_size,
        "sha256": sha256_of(target),
        "changelog": notes,
        "minVersionCode": args.min_version_code,
        "publishedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    size_mb = data["size"] / 1048576
    print(f"已发布 v{version_name} (versionCode={version_code})")
    print(f"  安装包: {target}  ({size_mb:.2f} MB)")
    print(f"  sha256: {data['sha256']}")
    print(f"  清单  : {manifest}")
    print(f"  说明  : {len(notes)} 条" + ("" if notes else "（未填 --note，客户端只显示版本号）"))
    if version_code <= 1:
        print("  ! versionCode=1：这是 0.x 系列的首个包，比它更老的客户端不会收到更新提示")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
