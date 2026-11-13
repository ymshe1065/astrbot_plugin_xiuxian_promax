# -*- coding: utf-8 -*-
"""生成《呈现层全命令渲染普查》第八章的全量命令表。

数据源：docs/_render_audit.json（由 scripts/audit_render.py 产出）。
本脚本只负责「缺陷码 / 目标块 / 优先级」的规则映射，便于评审时调规则后一键重出表。

用法：
    python scripts/audit_render.py           # 先产出通道与形态
    python scripts/gen_render_table.py       # 再产出 markdown 表（打印到 stdout）
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "docs" / "_render_audit.json"

# 未出现在目录按钮里的命令，按语义归组
MANUAL_GROUP = {
    "一键取回": "储物与仓库", "世界Boss": "战斗与竞技", "世界boss": "战斗与竞技",
    "世界团本": "战斗与竞技", "可制符箓": "丹道符箓", "坊市我的": "商店与仙市",
    "拍卖会信息": "商店与仙市", "拍卖会入场": "商店与仙市", "挑战通缉犯": "战斗与竞技",
    "追捕": "战斗与竞技", "通缉令": "战斗与竞技", "放弃历练": "秘境历练", "重修": "修行",
}

GROUP_BLOCK = {
    "修行": "stats + kv", "丹道符箓": "recipe", "储物与仓库": "items",
    "商店与仙市": "shelf", "宗门": "stats + table", "战斗与竞技": "timeline + table",
    "排行": "table", "洞天灵田": "progress + items", "灵兽": "items + stats",
    "社交与师徒": "kv + table", "秘境历练": "entries", "管理工具": "table",
    "自动代理": "stats + kv", "装备心法": "slots + compare",
    "钱庄": "stats + timeline", "飞升与活动": "stats + compare",
}

GROUP_FAULT = {
    "修行": "F6", "丹道符箓": "F1", "储物与仓库": "F1", "商店与仙市": "F2",
    "宗门": "F1", "战斗与竞技": "F4", "排行": "F3", "洞天灵田": "F1",
    "灵兽": "F1", "社交与师徒": "F3", "秘境历练": "F5", "管理工具": "F7",
    "自动代理": "—", "装备心法": "F6", "钱庄": "F4", "飞升与活动": "F3",
}

# 逐条覆盖：只在确实读过其输出形态后填写
FAULT = {
    # 丹道：配方清单是三级嵌套 + 假按钮
    "符箓书": "F1 F8", "可制符箓": "F1 F8", "丹方": "F1 F8", "丹药配方": "F1 F8",
    "符箓配方": "F1", "符箓": "F1", "选择生活职业": "F5",
    # 编号选择器
    "奇遇选择": "F5 F8", "火候": "F5 F8", "选择灵根": "F5", "灵根图鉴": "F5",
    "切原石": "F5 F8",
    # 伪表格
    "我的持仓": "F2", "行情": "—",
    # 时间流水
    "银行流水": "F4", "仙市流水": "F4",
    # 超长
    "世界Boss": "F7", "世界boss": "F7", "我的信息": "F7", "修仙帮助": "F7",
    "宗门任务": "F1 F7", "周本加入": "F1 F7", "接受双修": "F1",
    # 排行榜类
    "境界排行": "F3", "战力排行": "F3", "灵石排行": "F3", "存款排行": "F3",
    "宗门排行": "F3", "贡献排行": "F3", "功德榜": "F3", "气运榜": "F3", "师德榜": "F3",
    # 纯动作回执，现状无缺陷
    "购买": "—", "买入股": "—", "卖出股": "—", "申购": "—", "赎回": "—",
    "强化": "F6", "洗练": "F6", "炼器": "F6", "炼丹": "F6", "制符": "F6",
    "接受切磋": "—", "拒绝切磋": "—", "双修": "F6", "拒绝双修": "—",
    "签到": "—", "行善": "—", "调息": "—", "收功": "—", "解卡": "—",
    "改道号": "—", "激活化身": "—", "突破": "F6", "飞升": "F6",
}

BLOCK = {
    # 配方与图鉴
    "符箓书": "recipe", "可制符箓": "recipe", "丹方": "recipe", "丹药配方": "recipe",
    "符箓配方": "recipe", "符箓": "entries", "火候": "compare",
    # 详情
    "我的信息": "stats + progress + kv", "突破信息": "progress + stats + compare",
    "行情": "table + chart", "我的持仓": "table",
    "境界排行": "table", "战力排行": "table", "灵石排行": "table", "存款排行": "table",
    "宗门排行": "table", "贡献排行": "table", "功德榜": "table", "气运榜": "table",
    "师德榜": "table", "传承排行": "table", "周本排行": "table", "团本排行": "table",
    "宗门任务": "entries + progress", "周本加入": "table + note",
    "银行流水": "timeline", "仙市流水": "timeline",
    "我的灵田": "progress + items", "我的灵兽": "items + stats",
    "我的装备": "slots + compare", "秘境列表": "entries", "历练信息": "entries",
    "奇遇选择": "note + actions", "选择灵根": "note + actions", "选择生活职业": "note + actions",
    "丹阁": "shelf", "符阁": "shelf", "器阁": "shelf", "百宝阁": "shelf",
    "仓库": "items + progress", "储物戒": "items", "我的坊市": "shelf", "坊市我的": "shelf",
    "拍卖会信息": "entries", "拍卖会入场": "entries",
    "世界Boss": "stats + table", "世界boss": "stats + table", "世界团本": "table",
    "宗门建筑": "progress + table", "我的宗门": "stats + kv + table", "宗门列表": "table",
    "我的称号": "items", "称号图鉴": "items", "师徒": "kv + table",
    "修仙帮助": "kv", "一键取回": "items", "重修": "note",
    # 结算类：结果是「收获清单 + 数值变化」，不是一句话
    "完成历练": "stats + items", "完成悬赏": "stats + items",
    "完成探索": "stats + items", "领取任务奖励": "stats + items",
    "收获": "stats + items", "杂交": "stats + items",
    "升级丹炉": "note + stats", "购买洞天": "note + stats",
}

# 动作类命令：结果是一句话回执，正确形态是 note（+stats 展示变化），不该套查询类大布局
ACTION_VERBS = (
    "购买", "买入", "卖出", "赎回", "申购", "服用", "使用", "取出", "回收", "接收",
    "拒绝", "赠予", "上架", "下架", "出价", "报名", "领取", "升级", "强化", "洗练",
    "炼器", "炼丹", "制符", "种植", "收获", "施肥", "杂交", "铲除", "开垦", "闭关",
    "出关", "调息", "收功", "签到", "行善", "解卡", "突破", "飞升", "重修", "改道号",
    "选择", "激活", "创建", "加入", "退出", "解散", "踢出", "捐献", "兑换", "传位",
    "宣战", "开启", "关闭", "取消", "确认", "备战", "开始", "完成", "放弃", "接取",
    "接受", "挑战", "讨伐", "追捕", "劫镖", "押镖", "潜入", "抢占", "释放", "孵化",
    "喂养", "放生", "吞噬", "出战", "改名", "佩戴", "拜师", "出师", "逐出", "结缘",
    "解除", "共修", "代收", "转移", "更换", "附魔", "卸下", "装备", "存灵石",
    "取灵石", "贷款", "还款", "抵御", "查验", "解封", "潜修", "打坐",
)
# 动作类里仍有「前后对比 / 开箱结果」价值的
ACTION_WITH_DELTA = {"强化", "洗练", "炼器", "炼丹", "制符", "突破", "飞升", "吞噬",
                     "孵化", "切原石", "赠予", "服用"}

P0 = {
    "符箓书", "可制符箓", "丹方", "丹药配方", "突破信息", "我的信息", "仓库", "储物戒",
    "丹阁", "符阁", "器阁", "百宝阁", "坊市", "我的坊市", "行情", "我的持仓", "我的装备",
    "境界排行", "战力排行", "灵石排行", "宗门任务", "我的宗门", "秘境列表", "历练信息",
    "奇遇选择", "火候", "银行流水",
}
P2 = {r for r in ()} | {"通缉令", "追捕", "挑战通缉犯"}


# 动词命中但实质是「查询 / 菜单」的命令，必须计入待改（否则会被误判成一句话回执）
QUERY_FORCE = {"突破信息", "我的装备", "可制符箓", "奇遇选择", "选择灵根", "选择生活职业",
               "丹药背包", "丹药信息", "符箓"}


def is_action(cmd: str) -> bool:
    if cmd in QUERY_FORCE:
        return False
    return any(v in cmd for v in ACTION_VERBS)


def build() -> list[str]:
    rows = json.loads(AUDIT.read_text(encoding="utf-8"))
    p2 = set(P2) | {r["cmd"] for r in rows if r["group"] == "管理工具"}
    out: list[str] = []
    for r in sorted(rows, key=lambda x: (MANUAL_GROUP.get(x["cmd"], x["group"]) or "—", x["cmd"])):
        cmd = r["cmd"]
        g = MANUAL_GROUP.get(cmd, r["group"]) or "—"
        if cmd in FAULT:
            fault = FAULT[cmd]
        elif is_action(cmd):
            fault = "F6" if cmd in ACTION_WITH_DELTA else "—"
        else:
            fault = GROUP_FAULT.get(g, "F3")
        if cmd in BLOCK:
            block = BLOCK[cmd]
        elif is_action(cmd):
            block = "note + stats" if cmd in ACTION_WITH_DELTA else "note"
        else:
            block = GROUP_BLOCK.get(g, "kv")
        pri = "P0" if cmd in P0 else ("P2" if cmd in p2 else "P1")
        ch = {"text": "文本", "image+text": "卡片↓文本", "panel": "看板面板",
              "payload": "结构化"}[r["channel"]]
        out.append(f"| `/{cmd}` | {g} | {ch} | {fault} | {block} | {pri} |")
    return out


if __name__ == "__main__":
    print("\n".join(build()))
