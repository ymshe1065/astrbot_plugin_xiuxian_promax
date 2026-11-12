# -*- coding: utf-8 -*-
"""仙缘【一心二用】：忙碌状态下可并行处理的指令。

为什么不能真的「同时开两项活动」
--------------------------------
一个玩家只有**一个**活动状态位（``user_cd.type`` + ``scheduled_time`` + ``extra_data``），
历练路线、秘境随机奖励、镖车信息、宗门任务进度全部挤在这一格里。允许并行开始第二个
耗时活动只会把它覆盖掉，前一项就永久卡死（时间戳没了、奖励 roll 也没了）。所以并行
只能是「做事」，不能「开新活动」——``PARALLEL_FORBIDDEN`` 里的指令任何档位都不放行。

三档递进（一次购买只加一档，最多 3 档）
--------------------------------------
    1 档 · 杂务      储物 / 丹道符箓 / 装备 / 洞天灵田 / 灵兽
    2 档 · 交易社交  商店仙市 / 钱庄 / 宗门 / 师徒 / 排行
    3 档 · 全线      其余一切（除 PARALLEL_FORBIDDEN）
"""

import re
from typing import Any, Tuple

from .reincarnation_rules import item_owned, parse_bonus

# 对应 reincarnation_rules.SHOP_ITEMS 里的【一心二用】
PARALLEL_ITEM_ID = 52

# 档位名（用于文案）
TIER_NOTES = {1: "杂务", 2: "交易与社交", 3: "全线"}

# ---- 第 1 档：杂务 ----------------------------------------------------------
# 对齐网页指令目录的 储物与仓库 / 丹道符箓 / 装备心法 / 洞天灵田 / 灵兽 五组
TIER1_COMMANDS: Tuple[str, ...] = (
    # 储物与仓库
    "储物戒", "更换储物戒", "储物戒附魔", "领取溢出", "仓库", "升级仓库",
    "一键转移", "一键取出", "取出", "一键回收", "回收", "搜索物品", "物品信息",
    "赠予", "接收", "拒绝", "丹药背包", "丹药信息", "服用丹药",
    # 丹道符箓
    "丹方", "丹药配方", "学习丹方", "炼丹", "火候", "选择生活职业",
    "符箓", "符箓配方", "符箓书", "日常制符", "制符", "使用符箓", "升级丹炉",
    # 装备心法
    "我的装备", "心法", "技能", "装备", "卸下", "强化", "洗练", "炼器",
    # 洞天灵田
    "我的洞天", "购买洞天", "升级洞天", "洞天收取", "洞天装修", "进阶洞天",
    "我的灵田", "开垦灵田", "种植", "收获", "铲除", "施肥", "杂交", "升级灵田",
    "季节", "灵眼信息", "抢占灵眼", "灵眼收取", "释放灵眼", "开启护府", "潜入洞府",
    # 灵兽
    "我的灵兽", "孵化灵兽", "喂养灵兽", "灵兽出战", "灵兽改名", "放生灵兽",
    "吞噬灵兽", "灵兽吞噬",
    # 仙缘本身（买完还能接着买下一档，不然会卡在「买了用不了」）
    "仙缘兑换",
)

# ---- 第 2 档：交易与社交 ----------------------------------------------------
# 对齐 商店与仙市 / 钱庄 / 宗门（不含宗门任务）/ 社交与师徒 / 排行
TIER2_COMMANDS: Tuple[str, ...] = (
    # 商店与仙市
    "丹阁", "符阁", "器阁", "百宝阁", "购买", "仙市", "行情", "我的持仓",
    "仙市流水", "仙市订阅", "买入股", "卖出股", "申购", "赎回",
    "坊市", "坊市上架", "坊市购买", "坊市下架", "我的坊市", "坊市卖出", "坊市等待",
    "拍卖会", "拍卖会报名", "拍卖会出价", "拍卖会领取", "切原石", "黑市", "黑市购买",
    # 钱庄
    "银行", "存灵石", "取灵石", "领取利息", "贷款", "还款", "银行流水", "突破贷款",
    # 宗门（开任务那两条不在此列，见 PARALLEL_FORBIDDEN）
    "创建宗门", "加入宗门", "退出宗门", "解散宗门", "我的宗门", "宗门列表", "宗门捐献",
    "宗门商店", "宗门兑换", "宗门建筑", "升级建筑", "宗门命令", "宗门命令列表",
    "宗门命令撤销", "宗门大比状态", "宗门宣战", "宗门战争状态", "宗门战报",
    "踢出成员", "宗主传位", "职位变更",
    # 社交与师徒
    "拜师", "出师", "逐出师门", "师徒", "师德兑换", "仙灵结缘", "仙灵共修",
    "仙灵代收", "仙灵解除", "称号图鉴", "佩戴称号", "我的称号", "修仙帮助",
    # 排行
    "境界排行", "战力排行", "灵石排行", "存款排行", "宗门排行", "贡献排行",
    "功德榜", "气运榜", "师德榜", "气运",
)

# ---- 永远禁止：会改写活动状态位，或直接重置人生 ------------------------------
PARALLEL_FORBIDDEN: Tuple[str, ...] = (
    # 开新活动（会覆盖 user_cd，前一项活动将丢失时间戳与奖励）
    "闭关", "开始历练", "探索秘境", "押镖", "宗门任务", "调息", "周本开始", "周本确认",
    # 重置人生（活动状态位与人物数据会错位）
    "我要修仙", "弃道重修", "飞升",
    "重启修仙",
)


def clean_command(text: str) -> str:
    """归一化成纯指令文本（去 @、空白、前缀斜杠，与 handlers.utils 口径一致）。"""
    clean = (text or "").strip()
    clean = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", clean)
    clean = re.sub(r"^@\S+\s*", "", clean)
    clean = re.sub(r"^[^\w\u4e00-\u9fa5]+", "", clean)
    return clean.strip()


def _matches(clean_text: str, command: str) -> bool:
    return (
        clean_text == command
        or clean_text.startswith(command + " ")
        or clean_text.startswith(command + "\u3000")
    )


def parallel_level(player: Any) -> int:
    """已购买的【一心二用】档位（0 = 未购买）。"""
    try:
        return max(0, int(item_owned(parse_bonus(player), PARALLEL_ITEM_ID)))
    except Exception:
        return 0


def allowed_commands(level: int) -> Tuple[str, ...]:
    """该档位额外放行的指令集合（第 3 档返回空，表示「其余全部」）。"""
    if level >= 3:
        return ()
    if level == 2:
        return TIER1_COMMANDS + TIER2_COMMANDS
    if level == 1:
        return TIER1_COMMANDS
    return ()


def can_act_while_busy(player: Any, command_text: str) -> Tuple[bool, str]:
    """忙碌状态下这条指令能不能执行。

    返回 ``(是否放行, 给玩家的提示)``；提示为空表示沿用「无法分心他顾」的原文案。
    """
    clean = clean_command(command_text)
    if not clean:
        return False, ""

    level = parallel_level(player)

    if any(_matches(clean, cmd) for cmd in PARALLEL_FORBIDDEN):
        if level > 0:
            return False, (
                "⛔ 这个是「开始另一项耗时活动」或「重开人生」类指令，"
                "一心二用也顶不住 —— 活动位只有一个，硬开会把当前进度弄丢。\n"
                "💡 先结束手上这趟，再开新的。"
            )
        return False, ""

    if level <= 0:
        return False, ""

    if level >= 3 or any(_matches(clean, cmd) for cmd in allowed_commands(level)):
        return True, ""

    next_level = min(3, level + 1)
    return False, (
        f"💡 你买了【一心二用】第 {level} 档（{TIER_NOTES.get(level, '')}），"
        f"这条指令要第 {next_level} 档（{TIER_NOTES.get(next_level, '')}）才放行。\n"
        "发 /仙缘商店 或用网页「仙缘」标签页继续升档。"
    )
