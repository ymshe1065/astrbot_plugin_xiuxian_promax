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
            {"cmd": "/重修", "label": "弃道重修"},
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
            {"cmd": "/我的坊市", "label": "我的坊市", "panel": True},
            {"cmd": "/坊市卖出", "label": "坊市卖出", "arg": "问价编号 或 all"},
            {"cmd": "/黑市", "label": "黑市", "panel": True},
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
            {"cmd": "/讨伐", "label": "讨伐"},
            {"cmd": "/江湖事件", "label": "江湖事件", "panel": True},
            {"cmd": "/修仙帮助", "label": "修仙帮助", "panel": True},
        ],
    },
]

# 看板常驻卡片：进房后预热拉取
PANEL_PREFETCH = ["/我的信息"]

# 带参指令规格：空着发出去只会收到「请输入 xx」的报错，点按钮时应先弹输入框。
# 键为不带 / 的指令名；min 是至少需要的参数个数；hint 给输入框提示，example 给可直接照抄的样例。
# 只收「必填」的指令——丹阁/符阁/心法/行情这类留空合法的不放进来。
ARG_SPEC: dict[str, dict[str, Any]] = {
    # 交易与储物
    "购买": {"hint": "物品名 数量", "example": "炼气丹 1", "min": 1},
    "取出": {"hint": "物品名 数量", "example": "炼气丹 5", "min": 1},
    "回收": {"hint": "物品名 数量", "example": "炼气丹 1", "min": 1},
    "赠予": {"hint": "道号 物品名 数量", "example": "云无月 炼气丹 1", "min": 2},
    "装备": {"hint": "物品名", "example": "青锋剑", "min": 1},
    "卸下": {"hint": "戒指1/戒指2/功法/装备名", "example": "戒指1", "min": 1},
    "更换储物戒": {"hint": "储物戒名", "example": "纳物戒", "min": 1},
    "物品信息": {"hint": "物品名", "example": "炼气丹", "min": 1},
    "丹药信息": {"hint": "丹药名", "example": "炼气丹", "min": 1},
    "搜索物品": {"hint": "关键词", "example": "丹", "min": 1},
    "服用丹药": {"hint": "丹药名 数量", "example": "炼气丹 1", "min": 1},
    "炼丹": {"hint": "丹方名", "example": "聚气丹", "min": 1},
    "学习丹方": {"hint": "丹方名", "example": "聚气丹", "min": 1},
    # 仙市
    "买入股": {"hint": "股票代码 手数（1 手 = 100 股）", "example": "S01 2", "min": 2},
    "卖出股": {"hint": "股票代码 手数", "example": "S01 1", "min": 2},
    "申购": {"hint": "基金代码 金额", "example": "F01 10000", "min": 2},
    "赎回": {"hint": "基金代码 份额", "example": "F01 100", "min": 2},
    # 坊市（NPC 问价与上架）
    "坊市上架": {"hint": "物品名 价格", "example": "青锋剑 500", "min": 2},
    "坊市下架": {"hint": "我的上架编号", "example": "1", "min": 1},
    "坊市购买": {"hint": "商品编号", "example": "1", "min": 1},
    "坊市卖出": {"hint": "问价编号 或 all（一键卖出全部）", "example": "all", "min": 1},
    "坊市等待": {"hint": "问价编号", "example": "1", "min": 1},
    "黑市购买": {"hint": "商品编号", "example": "1", "min": 1},
    # 秘境与历练
    "探索秘境": {"hint": "秘境ID（见秘境列表，1-5）", "example": "1", "min": 1},
    "开始历练": {"hint": "历练路线", "example": "青云山", "min": 1},
    "接取悬赏": {"hint": "悬赏编号", "example": "1", "min": 1},
    "抢占灵眼": {"hint": "灵眼编号", "example": "1", "min": 1},
    # 宗门
    "加入宗门": {"hint": "宗门名", "example": "青云宗", "min": 1},
    "创建宗门": {"hint": "宗门名", "example": "青云宗", "min": 1},
    "宗门捐献": {"hint": "灵石数量", "example": "1000", "min": 1},
    "改道号": {"hint": "新道号", "example": "云无月", "min": 1},
    "宗主传位": {"hint": "道号", "example": "云无月", "min": 1},
    "踢出成员": {"hint": "道号", "example": "云无月", "min": 1},
    "职位变更": {"hint": "道号 职位", "example": "云无月 长老", "min": 2},
    # 钱庄与洞天
    "存灵石": {"hint": "数量", "example": "1000", "min": 1},
    "取灵石": {"hint": "数量", "example": "1000", "min": 1},
    "贷款": {"hint": "数量", "example": "1000", "min": 1},
    "还款": {"hint": "数量", "example": "1000", "min": 1},
    "突破贷款": {"hint": "数量", "example": "1000", "min": 1},
    "购买洞天": {"hint": "洞天类型编号", "example": "1", "min": 1},
    "升级洞天": {"hint": "升级项", "example": "灵气", "min": 1},
    "进阶洞天": {"hint": "目标类型编号", "example": "2", "min": 1},
    # 灵田
    "种植": {"hint": "种子名", "example": "聚灵草", "min": 1},
    "铲除": {"hint": "田块编号", "example": "1", "min": 1},
    # 兑换与事件
    "仙缘兑换": {"hint": "兑换项编号", "example": "1", "min": 1},
    "师德兑换": {"hint": "兑换项编号", "example": "1", "min": 1},
    "奇遇选择": {"hint": "选项编号", "example": "1", "min": 1},
    "抵御魔潮": {"hint": "捐献灵石数", "example": "1000", "min": 1},
    # 社交与修行
    "决斗": {"hint": "对手道号", "example": "云无月", "min": 1},
    "切磋": {"hint": "对手道号", "example": "云无月", "min": 1},
    "双修": {"hint": "道号", "example": "云无月", "min": 1},
    # 注：v7 起「/讨伐」不再吃参数（直接攻击当前江湖事件），故不入表，避免误弹输入框
    "传承挑战": {"hint": "目标道号", "example": "云无月", "min": 1},
    "突破": {"hint": "确认突破（可带破境丹）", "example": "确认突破", "min": 1},
    "选择灵根": {"hint": "灵根选项（1-3）", "example": "1", "min": 1},
    "我要修仙": {"hint": "修炼方式（体修/灵修）", "example": "灵修", "min": 1},
    "自动代理": {"hint": "任务类型", "example": "押镖", "min": 1},
    "关闭代理": {"hint": "任务类型", "example": "押镖", "min": 1},
}


def all_commands() -> dict[str, Any]:
    total = sum(len(c["items"]) for c in CATEGORIES)
    return {
        "categories": CATEGORIES,
        "total": total,
        "prefetch": PANEL_PREFETCH,
        "arg_spec": ARG_SPEC,
    }
