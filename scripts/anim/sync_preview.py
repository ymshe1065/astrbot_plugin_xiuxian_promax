# -*- coding: utf-8 -*-
"""把生成的动作数据注入 webchat_room/static/anim/preview.html（七场景原型）。

preview.html 是单文件原型，角色帧以 base64 内联，所以改完 acts.py 之后
必须重新注入，否则页面里跑的还是旧帧。

用法::

    python scripts/anim/sync_preview.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acts  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PREVIEW = os.path.join(ROOT, 'webchat_room', 'static', 'anim', 'preview.html')
MARK = 'const SPR_B64 = '


def main() -> int:
    if not os.path.exists(PREVIEW):
        print('找不到 %s' % PREVIEW)
        return 1
    with open(PREVIEW, encoding='utf-8') as f:
        lines = f.read().split('\n')

    idx = [i for i, l in enumerate(lines) if l.startswith(MARK)]
    if len(idx) != 1:
        print('期望恰好一处 %r，实际 %d 处，中止' % (MARK, len(idx)))
        return 1

    js = acts.make_js()
    lines[idx[0]] = js
    with open(PREVIEW, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('已注入 %s（%.1f KB 数据）' % (PREVIEW, len(js) / 1024.0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
