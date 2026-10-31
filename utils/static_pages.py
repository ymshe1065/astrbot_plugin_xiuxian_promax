# utils/static_pages.py
"""固定接口列表页内容源（文本与图片共用，避免图文不一致）"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

HELP_VERSION = "v6.47.2"
HELP_HEADER = f"📖 修仙指令大全 {HELP_VERSION} (一键接取·境界跌落·灵田肥力·自动施肥)"
HELP_HINT = "💡 提示：所有指令均可带 / 前缀使用"
HELP_TITLE = "修仙指令大全"
HELP_SUBTITLE = f"📖 {HELP_VERSION} · 所有指令均可带 / 前缀使用"
HELP_FOOTER = "仙途漫漫 · 输入 /修仙帮助 随时查阅"
DIVIDER = "━━━━━━━━━━━━━━━"

HELP_SECTIONS: List[Dict[str, Any]] = [
    {
        "name": "入门与基础",
        "icon": "🌱",
        "lines": [
            "/我要修仙 [职业] (灵/体/魔/剑/鬼/佛/御兽) /我的信息 /我的装备 /修仙帮助",
            "/调息 /收功 /签到 /一键日常 /解卡",
            "/改道号 /弃道重修 确认 /行善",
            "/选择生活职业 <炼器师/符箓师/阵法师/炼丹师> /制符 [符名] [数量]",
            "生活职业更换费用随境界和更换次数提高，72小时衰减至基础费用",
        ],
    },
    {
        "name": "修炼与突破",
        "icon": "🧘",
        "lines": [
            "/闭关 /出关 (受家具/宗门/魔潮/化身影响)",
            "/突破信息 /突破 [破境丹] [品质] /升级丹炉 /激活化身",
            "/修为超过当前境界门槛后可获得修为溢出加成，见 /我的信息",
        ],
    },
    {
        "name": "战斗与竞技",
        "icon": "⚔️",
        "lines": [
            "/切磋 @人 /接受切磋 /拒绝切磋 /决斗 @人",
            "/挑战世界Boss /世界Boss状态 /世界Boss",
            "/挑战团本 /团本状态 /世界团本 /团本排行",
            "/讨伐 /追捕 /挑战通缉犯 /江湖事件 /通缉令",
            "/传承挑战 @人 /传承信息 /传承排行",
        ],
    },
    {
        "name": "历练与任务",
        "icon": "🗺️",
        "lines": [
            "/历练信息 /开始历练 <路线> /历练状态 /完成历练 /放弃历练",
            "/秘境列表 /探索秘境 <ID> /完成探索 /退出秘境",
            "/任务榜 /接取任务 <品阶/all> /放弃任务 <编号> /领取任务奖励",
            "/自动代理 秘境 <ID>（到期自动结算并重新进入）",
            "/悬赏令 /接取悬赏 <ID> /悬赏状态 /完成悬赏 /放弃悬赏",
            "/奇遇选择 <编号> (历练/秘境归来可触发)",
        ],
    },
    {
        "name": "丹道与符箓",
        "icon": "💊",
        "lines": [
            "/丹药背包 /丹药信息 <名> /服用丹药 <名> [品质] [数量]",
            "/丹方 /学习丹方 <ID或名称> /丹药配方",
            "/炼丹 <ID> /火候 /升级丹炉",
            "/符箓 (8 类共 34 张，含效果与熟练度) /符箓配方 (配方长图)",
            "/制符 <符名> [数量] (符箓师绘制；不带参数领每日护劫符)",
            "/使用符箓 <符名> [数量] (攻击/防御/控制/回复/炼丹/探索/增益/遁术)",
            "/符阁 <符箓|符图> (成品符只卖凡/黄；玄品以上买符图自己画)",
            "熟练度决定成功率与额外产出：成功按张数×品阶记点，失败按半计",
            "丹劫按丹药品阶：凡/黄不渡劫；护劫符护盾 ≥ 劫数即保全，一张只挡一次",
        ],
    },
    {
        "name": "商店与交易",
        "icon": "🏪",
        "lines": [
            "/丹阁 <丹药|丹方|材料> /器阁 /符阁 <符箓|符图> /百宝阁",
            "三阁均以单张长图展示：器阁常规装备多、百宝阁稀有高品阶为主",
            "符箓已从百宝阁移入符阁；装备类库存固定为 1，材料/丹药/符箓可多件",
            "/购买 <物> [数量] /购买物品 <物> [数量] /物品信息 <名>",
            "/装备 <物名> [序号] [部位] /装备 <功法名> [栏位1-3]",
            "/卸下 <部位/心法/功法名> [栏位] (如 /卸下 戒指 2)",
            "/我的装备 比较 <装备名> [序号]",
            "/储物戒 /丢弃 <物> [数量] /搜索物品 <词> /领取溢出",
            "/赠予 @人 <物/丹> [品质] [数量] /赠予 @人 灵石 <数量> /接收 /拒绝",
            "/更换储物戒 <名> /储物戒附魔 [数量]",
            "/坊市 /坊市上架 /坊市购买 /我的坊市 /坊市卖出 /坊市等待",
            "/拍卖会 /拍卖会信息 /拍卖会报名 /拍卖会入场 /拍卖会出价 /拍卖会领取",
            "/仙市 /行情 <代码> /我的持仓 /仙市流水 /仙市订阅 (07:00-22:00 每小时一盘，16 盘/日)",
            "/买入股 <代码> <手> /卖出股 <代码> <手> /申购 <代码> <金额> /赎回 <代码> <份额|全部>",
            "/切原石 /黑市 /黑市购买 <编号> /强化 /洗练 [部位]",
            "/炼器 洗练 <部位> <词条> <妖兽材料> [数量1-5] (妖兽材料定向词条)",
            "黑市稀有货占比提升；真品高价、假货小概率，购买前看清鉴定",
        ],
    },
    {
        "name": "洞天·灵田·灵眼·灵兽",
        "icon": "🏔️",
        "lines": [
            "/我的洞天 /购买洞天 1 /升级洞天 [次数|1-5|all] /洞天收取",
            "/洞天装修 <家具> /进阶洞天 <编号>",
            "/我的灵田 /开垦灵田 /种植 <草> [数量|1-20|all] /收获",
            "/铲除 <格号...|1-3|all> /施肥 /杂交 <草A> <草B> /升级灵田 /季节",
            "/自动代理 灵田 [格号...] /自动代理 施肥 [阈值%]（肥力低于阈值自动补肥）",
            "/灵眼信息 /抢占灵眼 <ID> /灵眼收取 /释放灵眼",
            "/我的灵兽 /孵化灵兽 <名> /喂养灵兽 [序号/名] /灵兽出战 /灵兽改名",
            "/放生灵兽 /吞噬灵兽 /灵兽吞噬 <序号/名> [灵兽内丹]",
            "灵兽随主人修为获得经验；Lv20/Lv40 自动进化；御兽可拥有多只",
        ],
    },
    {
        "name": "宗门与社交",
        "icon": "🏛️",
        "lines": [
            "/创建宗门 /加入宗门 /退出宗门 /解散宗门 确认 /我的宗门 /宗门列表",
            "/宗门捐献 <数> /宗门任务 /完成宗门任务 /放弃宗门任务",
            "/宗门建筑 /升级建筑 <名称> /宗门商店 /宗门兑换 <编号>",
            "/宗门命令 @人 捐献 1000 24 /宗门命令列表 /宗门命令撤销 <编号>",
            "/宗门大比开启 /宗门大比报名 /宗门大比状态",
            "/宗门宣战 <宗门名> /宗门战争状态 /宗门战报 [编号]",
            "/踢出成员 @人 /宗主传位 @人 /职位变更 @人 <1-4>",
            "/开启护府 /潜入洞府 @人 /双修 @人 /接受双修 /拒绝双修",
            "/仙灵结缘 [名] /仙灵共修 /仙灵代收 /仙灵解除",
            "/拜师 @人 /师徒 /出师 @人 /逐出师门 @人 /师德兑换 <编号>",
            "/称号图鉴 /佩戴称号 <名> /我的称号 110称号",
        ],
    },
    {
        "name": "活动·排行·飞升",
        "icon": "🌌",
        "lines": [
            "/仙界日历 /魔潮状态 /抵御魔潮 <灵石> /天魔状态 /查验天魔 @人",
            "/境界排行 /战力排行 /灵石排行 /存款排行 /宗门排行",
            "/贡献排行 /功德榜 /气运榜 /师德榜 /气运",
            "/飞升 [封印装备名] /解封传家宝 /仙缘商店 /仙缘兑换 <编号>",
        ],
    },
    {
        "name": "管理员",
        "icon": "🛠️",
        "note": "(需配置 BOSS_ADMINS)",
        "divider_before": True,
        "lines": [
            "/仙界管理 /修仙诊断",
            "/生成世界Boss /生成Boss /开启团本 [1-3] /结束团本 /开启魔潮",
            "/生成江湖事件 [wanted/beast/demon/guardian] [1-5]",
            "/开启天魔入侵 [1-48] /结束天魔入侵",
        ],
    },
]


def _section_header(sec: Dict[str, Any]) -> str:
    icon = sec.get("icon", "")
    prefix = f"{icon}" if icon else ""
    return f"{prefix}【{sec['name']}】{sec.get('note', '')}"


def build_help_text() -> str:
    """由统一内容源生成帮助文本（图片不可用时的降级输出）"""
    chunks: List[str] = []
    for sec in HELP_SECTIONS:
        if sec.get("divider_before"):
            chunks.append(DIVIDER)
        lines = [_section_header(sec)] + [f"  {line}" for line in sec.get("lines", [])]
        chunks.append("\n".join(lines))
    return "\n".join([HELP_HEADER, HELP_HINT, DIVIDER]) + "\n" + "\n\n".join(chunks)


def build_help_image_sections() -> List[Dict[str, Any]]:
    """转换为 render_card 的 sections 结构"""
    sections: List[Dict[str, Any]] = []
    for sec in HELP_SECTIONS:
        icon = sec.get("icon", "")
        name = f"{icon} {sec['name']}" if icon else sec["name"]
        note = sec.get("note", "")
        if note:
            name = f"{name} {note}"
        sections.append({"name": name, "lines": list(sec.get("lines", []))})
    return sections


PAGES: Dict[str, Dict[str, Any]] = {
    "help": {
        "title": HELP_TITLE,
        "subtitle": HELP_SUBTITLE,
        "footer": HELP_FOOTER,
        "background": "info",
        "renderer": "help_columns",
        "width": 1600,
        "sections": build_help_image_sections(),
        "text_builder": build_help_text,
    },
}


def page_fingerprint(page_key: str) -> str:
    """页面内容指纹：文案/标题/版本任一变化都会触发图片重生成"""
    spec = PAGES[page_key]
    payload = {
        "title": spec.get("title", ""),
        "subtitle": spec.get("subtitle", ""),
        "footer": spec.get("footer", ""),
        "background": spec.get("background", "default"),
        "renderer": spec.get("renderer", "card"),
        "width": spec.get("width", 1000),
        "sections": spec.get("sections", []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
