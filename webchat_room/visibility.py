# -*- coding: utf-8 -*-
"""消息呈现辅助：识别查询结果中的个人信息。

网页聊天室里，查询指令的结果卡片默认折叠在聊天流中，避免个人资产、配方、
背包、持仓等长内容占满公屏。判定不依赖具体 Handler，只按指令名分类；后续
新增个人查询时，把指令名补进 ``PERSONAL_COMMANDS`` 即可。
"""
from __future__ import annotations

# 不以「我的」开头、但结果含个人信息的查询指令
PERSONAL_COMMANDS = frozenset({
    # 丹道与符箓
    "丹方",
    "丹药配方",
    "丹药背包",
    "符箓配方",
    "符箓书",
    "可制符箓",
    # 资产与钱庄
    "银行",
    "银行流水",
    "仙市流水",
    # 修行与储物
    "心法",
    "技能",
    "仓库",
    "突破信息",
    "传承信息",
    # 宗门与团队
    "宗门命令列表",
    "历练信息",
    "历练状态",
    "悬赏状态",
    "团本状态",
    "周本状态",
    # 洞天、灵兽与社交
    "灵眼信息",
    "师徒",
    "气运",
    "代理状态",
})

# 别名归一到目录里的正式指令名
PERSONAL_ALIASES = {
    "坊市我的": "我的坊市",
}

# 固定送入右侧看板的指令：无论前端是否带 panel 标记，都必须私密。
# 兼容旧版前端（只发 private、不发 panel）以及玩家在输入框直接敲指令的情况。
PANEL_COMMANDS = frozenset({"我的信息", "储物戒"})


def command_name(text: str) -> str:
    """从消息原文里取出指令名。

    兼容 ``/丹药配方``、``丹药配方``、``@机器人 /丹药配方`` 以及带参形式；
    优先取第一个以 ``/`` 开头的 token，否则取第一个 token。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    tokens = raw.split()
    for token in tokens:
        if token.startswith("/"):
            return token.lstrip("/").strip()
    return tokens[0].lstrip("/").strip()


def is_personal_command(text: str) -> bool:
    """该消息是否为个人信息类查询指令。"""
    name = command_name(text)
    if not name:
        return False
    name = PERSONAL_ALIASES.get(name, name)
    return name.startswith("我的") or name in PERSONAL_COMMANDS


def is_panel_command(text: str) -> bool:
    """该消息是否需要归入看板（我的信息 / 储物戒）。"""
    name = command_name(text)
    if not name:
        return False
    return PERSONAL_ALIASES.get(name, name) in PANEL_COMMANDS
