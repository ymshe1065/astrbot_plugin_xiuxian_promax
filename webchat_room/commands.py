# -*- coding: utf-8 -*-
"""网页端指令目录：给前端渲染按钮网格用。

只挑常用指令（257 条全铺出来会淹没玩家），按使用场景分组。
字段：
    cmd     实际发送的指令文本（不含唤醒前缀时后端补 /）
    label   按钮文字
    arg     带参指令的输入提示；为空表示可直接点发
    panel   结果是否进看板（查询类 true，动作类 false）
    tone    视觉权重：gold 主行动 / normal 常规
"""
from __future__ import annotations

from typing import Any

CATEGORIES: list[dict[str, Any]] = [
    {
        "id": "core",
        "name": "修行",
        "icon": "☯",
        "items": [
            {"cmd": "/签到", "label": "每日签到", "tone": "gold"},
            {"cmd": "/我的信息", "label": "我的信息", "panel": True},
            {"cmd": "/闭关", "label": "闭关", "tone": "gold"},
            {"cmd": "/出关", "label": "出关"},
            {"cmd": "/调息", "label": "调息"},
            {"cmd": "/收功", "label": "收功"},
            {"cmd": "/突破信息", "label": "突破信息", "panel": True},
            {"cmd": "/突破", "label": "突破", "tone": "gold", "arg": "确认突破"},
            {"cmd": "/我的状态", "label": "我的状态"},
        ],
    },
    {
        "id": "gear",
        "name": "装备心法",
        "icon": "⚔",
        "items": [
            {"cmd": "/我的装备", "label": "我的装备", "panel": True},
            {"cmd": "/心法", "label": "心法", "panel": True},
            {"cmd": "/技能", "label": "技能", "panel": True},
            {"cmd": "/装备", "label": "装备物品", "arg": "物品名"},
            {"cmd": "/卸下", "label": "卸下", "arg": "槽位"},
        ],
    },
    {
        "id": "bag",
        "name": "储物与丹药",
        "icon": "🎒",
        "items": [
            {"cmd": "/储物戒", "label": "储物戒", "panel": True},
            {"cmd": "/丹药背包", "label": "丹药背包", "panel": True},
            {"cmd": "/取出", "label": "取出", "arg": "物品名 数量"},
            {"cmd": "/回收", "label": "回收", "arg": "物品名"},
            {"cmd": "/赠予", "label": "赠予", "arg": "道号 物品名"},
            {"cmd": "/搜索物品", "label": "搜索物品", "arg": "关键词"},
            {"cmd": "/物品信息", "label": "物品信息", "arg": "物品名", "panel": True},
            {"cmd": "/服用丹药", "label": "服用丹药", "arg": "丹药名"},
        ],
    },
    {
        "id": "shop",
        "name": "商铺与仙市",
        "icon": "🏮",
        "items": [
            {"cmd": "/丹阁", "label": "丹阁", "panel": True},
            {"cmd": "/符阁", "label": "符阁", "panel": True},
            {"cmd": "/器阁", "label": "器阁", "panel": True},
            {"cmd": "/百宝阁", "label": "百宝阁", "panel": True},
            {"cmd": "/购买", "label": "购买", "arg": "物品名 数量"},
            {"cmd": "/仙市", "label": "仙市", "panel": True},
            {"cmd": "/行情", "label": "行情", "panel": True},
            {"cmd": "/我的持仓", "label": "我的持仓", "panel": True},
        ],
    },
    {
        "id": "sect",
        "name": "宗门",
        "icon": "⛩",
        "items": [
            {"cmd": "/我的宗门", "label": "我的宗门", "panel": True},
            {"cmd": "/宗门列表", "label": "宗门列表", "panel": True},
            {"cmd": "/宗门任务", "label": "宗门任务", "panel": True},
            {"cmd": "/完成宗门任务", "label": "完成任务"},
            {"cmd": "/宗门捐献", "label": "宗门捐献", "arg": "灵石数"},
            {"cmd": "/加入宗门", "label": "加入宗门", "arg": "宗门名"},
            {"cmd": "/创建宗门", "label": "创建宗门", "arg": "宗门名"},
        ],
    },
    {
        "id": "explore",
        "name": "秘境历练",
        "icon": "🌀",
        "items": [
            {"cmd": "/秘境列表", "label": "秘境列表", "panel": True},
            {"cmd": "/探索秘境", "label": "探索秘境", "arg": "秘境ID"},
            {"cmd": "/完成探索", "label": "完成探索"},
            {"cmd": "/开始历练", "label": "开始历练"},
            {"cmd": "/历练状态", "label": "历练状态"},
            {"cmd": "/世界Boss", "label": "世界Boss", "panel": True},
            {"cmd": "/挑战Boss", "label": "挑战Boss", "tone": "gold"},
        ],
    },
    {
        "id": "rank",
        "name": "排行",
        "icon": "🏆",
        "items": [
            {"cmd": "/境界排行", "label": "境界榜", "panel": True},
            {"cmd": "/战力排行", "label": "战力榜", "panel": True},
            {"cmd": "/灵石排行", "label": "灵石榜", "panel": True},
            {"cmd": "/宗门排行", "label": "宗门榜", "panel": True},
            {"cmd": "/贡献排行", "label": "贡献榜", "panel": True},
        ],
    },
    {
        "id": "bank",
        "name": "钱庄洞天",
        "icon": "🏦",
        "items": [
            {"cmd": "/银行", "label": "钱庄", "panel": True},
            {"cmd": "/存灵石", "label": "存灵石", "arg": "数量"},
            {"cmd": "/取灵石", "label": "取灵石", "arg": "数量"},
            {"cmd": "/领取利息", "label": "领取利息"},
            {"cmd": "/贷款", "label": "贷款", "arg": "数量"},
            {"cmd": "/还款", "label": "还款", "arg": "数量"},
            {"cmd": "/我的洞天", "label": "我的洞天", "panel": True},
            {"cmd": "/我的灵田", "label": "我的灵田", "panel": True},
            {"cmd": "/种植", "label": "种植", "arg": "种子名"},
            {"cmd": "/收获", "label": "收获"},
        ],
    },
    {
        "id": "social",
        "name": "社交",
        "icon": "🤝",
        "items": [
            {"cmd": "/决斗", "label": "决斗", "arg": "道号"},
            {"cmd": "/切磋", "label": "切磋", "arg": "道号"},
            {"cmd": "/双修", "label": "双修", "arg": "道号"},
            {"cmd": "/讨伐", "label": "讨伐", "arg": "目标"},
            {"cmd": "/江湖事件", "label": "江湖事件", "panel": True},
            {"cmd": "/修仙帮助", "label": "修仙帮助", "panel": True},
        ],
    },
]

# 看板常驻卡片：进房后预热拉取
PANEL_PREFETCH = ["/我的信息"]


def all_commands() -> dict[str, Any]:
    total = sum(len(c["items"]) for c in CATEGORIES)
    return {"categories": CATEGORIES, "total": total, "prefetch": PANEL_PREFETCH}
