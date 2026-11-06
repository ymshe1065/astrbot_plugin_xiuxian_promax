# -*- coding: utf-8 -*-
"""网页聊天室参数候选：按指令 + 当前已填值，返回下一字段的可点选项。

设计目标：
- 前端不再只给一个输入框和「用示例」，而是把储物戒装备、职业、宗门、
  秘境、持仓等真实数据列成可滚动、可筛选的候选；
- 数据全部来自插件当前存档（players / shop / auction 等），不在前端硬编码；
- 多段参数按字段逐段选择，例如 ``/赠予`` 依次是 对象 → 物品 → 数量。

返回结构（``build_arg_options``）::

    {
      "ok": True,
      "cmd": "赠予",
      "index": 0,                 # 当前第几段（0 起）
      "min": 2,                   # 指令至少需要的段数
      "values": ["web_xxx"],
      "field": {"key": "target", "label": "选择赠予对象", "optional": False},
      "options": [{"label": "云无月", "value": "web_yun", "desc": "..."}],
      "done": False,
      "message": "",
    }

没有配置候选的字段（金额、单价等）返回空 ``options``，前端仍保留手动输入。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .commands import ARG_SPEC

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin

MAX_OPTIONS = 200


# ---------------------------------------------------------------------------
# 字段计划：一个指令由若干段组成，每段可挂一个候选源
# ---------------------------------------------------------------------------

# 常用占位：source 为空表示「没有枚举值，请手动输入」
_MANUAL: dict[str, Any] = {"source": ""}


def _f(key: str, label: str, source: str = "", **extra: Any) -> dict[str, Any]:
    data = {"key": key, "label": label, "source": source}
    data.update(extra)
    return data


FIELD_PLANS: Dict[str, List[dict[str, Any]]] = {
    # ---- 装备与储物 ----
    "装备": [_f("item", "选择要装备的物品", "equip_items")],
    "卸下": [_f("slot", "选择要卸下的栏位", "equipped_slots")],
    "更换储物戒": [_f("ring", "选择要更换的储物戒", "storage_rings")],
    "升级仓库": [_f("times", "选择扩建次数", "warehouse_upgrade_modes", optional=True)],
    "一键转移": [_f("category", "选择要转入仓库的分类", "warehouse_categories", optional=True)],
    "一键取出": [_f("category", "选择要取出到储物戒的分类", "warehouse_categories", optional=True)],
    "洗练": [_f("slot", "选择要洗练的部位", "wash_slots", optional=True)],
    "取出": [
        _f("item", "选择要从藏宝阁取出的物品", "warehouse_items"),
        _f("count", "数量（留空 = 全部）", "", optional=True),
    ],
    "回收": [_f("item", "选择要回收的物品", "recycle_items"), _f("count", "数量（默认 1）", "", optional=True)],
    "赠予": [
        _f("target", "选择赠予对象", "players"),
        _f("item", "选择赠予的物品", "gift_items"),
        _f("count", "数量（默认 1）", "", optional=True),
    ],
    "物品信息": [_f("item", "选择要查询的物品", "item_names")],
    "丹药信息": [_f("pill", "选择要查询的丹药", "pill_names")],
    "服用丹药": [
        _f("pill", "选择要服用的丹药", "pills_inventory"),
        _f("count", "数量（默认 1）", "", optional=True),
    ],
    "购买": [_f("item", "选择要购买的货架物品", "shop_items"), _f("count", "数量（默认 1）", "", optional=True)],
    "丹阁": [_f("section", "选择丹阁分区", "pill_pavilion_sections")],
    "符阁": [_f("section", "选择符阁分区", "talisman_pavilion_sections")],
    # ---- 炼丹 ----
    "炼丹": [
        _f("recipe", "选择已学丹方", "learned_recipes"),
        _f("fire", "火候（可选）", "fire_choices", optional=True),
    ],
    "学习丹方": [_f("recipe", "选择要学习的丹方", "all_recipes")],
    # ---- 仙市 ----
    "行情": [_f("code", "选择股票或基金", "market_codes")],
    "买入股": [_f("code", "选择股票", "stock_codes"), _f("lots", "手数（1 手 = 100 股）", "")],
    "卖出股": [_f("code", "选择持仓股票", "stock_holdings"), _f("lots", "手数", "")],
    "申购": [_f("code", "选择基金", "fund_codes"), _f("amount", "申购金额", "")],
    "赎回": [_f("code", "选择持仓基金", "fund_holdings"), _f("shares", "赎回份额", "")],
    # ---- 坊市 / 黑市 ----
    "坊市上架": [
        _f("item", "选择要上架的物品", "recycle_items"),
        _f("count", "上架数量（默认 1）", ""),
        _f("price", "单价（灵石）", ""),
    ],
    "坊市下架": [_f("id", "选择要下架的商品", "my_listings")],
    "坊市购买": [_f("id", "选择要购买的商品", "market_listings")],
    "坊市卖出": [_f("id", "选择要接受的问价", "market_offers", allow_all=True)],
    "坊市等待": [_f("id", "选择要放弃的问价", "market_offers")],
    "黑市购买": [_f("id", "选择黑市商品", "black_market")],
    # ---- 秘境 / 历练 / 悬赏 / 灵眼 ----
    "探索秘境": [_f("id", "选择秘境", "rift_ids")],
    "开始历练": [_f("route", "选择历练路线", "adventure_routes")],
    "接取悬赏": [_f("id", "选择悬赏", "bounty_ids")],
    "抢占灵眼": [_f("id", "选择无主灵眼", "spirit_eyes")],
    # ---- 宗门 ----
    "加入宗门": [_f("name", "选择宗门", "sect_names")],
    "宗主传位": [_f("target", "选择传位对象", "sect_members")],
    "踢出成员": [_f("target", "选择踢出的成员", "sect_members")],
    "职位变更": [
        _f("target", "选择成员", "sect_members"),
        _f("position", "选择职位", "sect_positions"),
    ],
    # ---- 钱庄 / 洞天 / 灵田 ----
    "购买洞天": [_f("type", "选择洞天类型", "land_types")],
    "升级洞天": [_f("mode", "选择升级方式（留空 = 升 1 级）", "upgrade_modes", optional=True)],
    "进阶洞天": [_f("type", "选择进阶目标", "land_advance_types")],
    "种植": [_f("herb", "选择灵草", "farm_herbs"), _f("count", "数量 / 1-20 / all（可选）", "", optional=True)],
    "铲除": [_f("slot", "选择要铲除的田块", "farm_slots")],
    # ---- 兑换 / 奇遇 ----
    "仙缘兑换": [_f("id", "选择仙缘商品", "fate_shop")],
    "师德兑换": [_f("id", "选择师德商品", "virtue_shop")],
    "奇遇选择": [_f("id", "选择奇遇选项", "encounter_choices")],
    # ---- 社交 / 修行 ----
    "决斗": [_f("target", "选择对手", "players")],
    "切磋": [_f("target", "选择对手", "players")],
    "双修": [_f("target", "选择道侣", "players")],
    "传承挑战": [_f("target", "选择挑战对象", "players")],
    "选择灵根": [_f("id", "选择灵根", "spirit_roots")],
    "我要修仙": [_f("type", "选择修炼职业", "cultivation_types")],
    "自动代理": [_f("task", "选择代理活动", "proxy_tasks")],
    "关闭代理": [_f("task", "选择要关闭的代理", "proxy_off_tasks")],
    # ---- 生活职业 / 符箓 / 炼器 ----
    "火候": [_f("choice", "选择火候", "fire_choices")],
    "选择生活职业": [_f("skill", "选择生活职业", "life_skills")],
    "使用符箓": [
        _f("talisman", "选择要使用的符箓", "talisman_inventory"),
        _f("count", "数量（默认 1）", "", optional=True),
    ],
    "炼器": [
        _f("action", "选择炼器方式", "forge_actions"),
        _f("slot", "选择已装备的部位", "wash_slots"),
        _f("affix", "选择目标词条", "forge_affixes"),
        _f("material", "选择妖兽材料", "monster_materials"),
        _f("count", "材料数量 1-5（默认 1）", "", optional=True),
    ],
    # ---- 宗门扩展 ----
    "宗门兑换": [_f("id", "选择宗门商品", "sect_shop_items")],
    "升级建筑": [_f("name", "选择宗门建筑", "sect_buildings")],
    "宗门命令": [
        _f("target", "选择命令对象", "sect_members"),
        _f("type", "选择命令类型", "sect_order_types"),
        _f("value", "目标数量", ""),
        _f("hours", "时限小时（1-72）", ""),
    ],
    "宗门命令撤销": [_f("id", "选择要撤销的命令", "sect_orders")],
    "宗门宣战": [_f("name", "选择宣战宗门", "sect_names")],
    # ---- 日常任务 / 周本 ----
    "接取任务": [_f("level", "选择任务品阶", "task_levels")],
    "放弃任务": [_f("id", "选择要放弃的任务", "active_tasks")],
    "周本加入": [_f("id", "选择周本队伍", "weekly_runs")],
    "重置周本CD": [_f("target", "选择玩家", "players")],
    # ---- 洞天 / 灵田 / 灵兽 ----
    "洞天装修": [_f("name", "选择家具", "furniture_names")],
    "杂交": [
        _f("a", "选择第一种灵草", "farm_herbs"),
        _f("b", "选择第二种灵草", "farm_herbs"),
    ],
    "潜入洞府": [_f("target", "选择目标道友", "players")],
    "劫镖": [_f("target", "选择目标道友", "players")],
    "查验天魔": [_f("target", "选择查验对象", "players")],
    "孵化灵兽": [_f("pet", "选择灵兽种类", "pet_types")],
    "喂养灵兽": [_f("pet", "选择灵兽", "my_pets")],
    "灵兽出战": [_f("pet", "选择出战灵兽", "my_pets")],
    "灵兽改名": [_f("pet", "选择灵兽", "my_pets"), _f("name", "输入新名字", "")],
    "放生灵兽": [_f("pet", "选择要放生的灵兽", "my_pets")],
    "吞噬灵兽": [_f("pet", "选择要吞噬的灵兽", "my_pets")],
    "灵兽吞噬": [
        _f("pet", "选择灵兽", "my_pets"),
        _f("item", "灵兽内丹（可留空）", "", optional=True),
        _f("count", "数量（默认 1 / all）", "", optional=True),
    ],
    # ---- 社交 / 称号 / 仙灵 ----
    "拜师": [_f("target", "选择师父", "players")],
    "出师": [_f("target", "选择要出师的弟子", "disciples")],
    "逐出师门": [_f("target", "选择要逐出的弟子", "disciples")],
    "佩戴称号": [_f("title", "选择称号", "owned_titles")],
    "仙灵结缘": [_f("name", "选择仙灵", "spirit_companions")],
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _opt(label: Any, value: Any = None, desc: Any = "", group: str = "") -> dict[str, str]:
    text = str(label if label is not None else "").strip()
    val = str(value if value is not None else text).strip()
    item = {"label": text or val, "value": val}
    if desc:
        item["desc"] = str(desc)
    if group:
        item["group"] = group
    return item


def _sort_key(item: dict[str, str]) -> tuple:
    return (item.get("group", ""), item.get("label", ""))


def _dedupe(options: List[dict[str, str]]) -> List[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: List[dict[str, str]] = []
    for item in options:
        key = (item.get("group", ""), item.get("value", ""))
        if not item.get("value") or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _realm(player: Any, plugin: "XiuXianPlugin") -> str:
    try:
        return player.get_level(plugin.config_manager)
    except Exception:
        try:
            levels = plugin.config_manager.get_level_data(player.cultivation_type)
            return levels[player.level_index]["level_name"]
        except Exception:
            return f"境界{getattr(player, 'level_index', 0)}"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nm(value: Any) -> str:
    """数字加千分位，失败原样返回。"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


async def _player(plugin: "XiuXianPlugin", user_id: str) -> Any:
    try:
        return await plugin.db.get_player_by_id(user_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 候选源：每个函数返回 (options, message)
# ---------------------------------------------------------------------------


async def _src_equip_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色，请先 /我要修仙"
    options: List[dict[str, str]] = []
    try:
        im = plugin.equipment_handler.equipment_manager.instance_manager
        instances = [x for x in im.list_instances(player, "storage") if x.get("name")]
        grouped: Dict[str, List[dict]] = {}
        for inst in instances:
            grouped.setdefault(str(inst.get("name")), []).append(inst)
        for name in sorted(grouped):
            insts = sorted(grouped[name], key=lambda x: _int(x.get("id")))
            for idx, inst in enumerate(insts, 1):
                quality = str(inst.get("quality") or "一般")
                value = name if len(insts) == 1 else f"{name} {idx}"
                label = name if len(insts) == 1 else f"{name} #{idx}"
                options.append(_opt(label, value, f"品质：{quality}", "储物戒·装备"))
    except Exception:
        pass
    # 功法/心法以可堆叠物品存在储物戒里，直接按名称装备
    try:
        ring = player.get_storage_ring_items() or {}
        techniques = getattr(plugin.config_manager, "techniques_data", {}) or {}
        for name, count in ring.items():
            if _int(count) <= 0 or name not in techniques:
                continue
            cfg = techniques.get(name) or {}
            kind = "心法" if cfg.get("type") in ("main_technique", "心法") else "功法"
            options.append(_opt(name, name, f"{kind} · 持有 x{_int(count)}", f"储物戒·{kind}"))
    except Exception:
        pass
    if not options:
        try:
            overflow = plugin.equipment_handler.equipment_manager.instance_manager.list_instances(
                player, "overflow"
            )
        except Exception:
            overflow = []
        if overflow:
            return [], "储物戒已满，装备在溢出仓库，请先 /领取溢出"
        return [], "储物戒里没有可装备的装备或功法"
    options.sort(key=_sort_key)
    return options, ""


async def _src_equipped_slots(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        from ..core.item_rules import ItemRules

        slots = plugin.equipment_handler.equipment_manager.get_gear_slots(player)
        for slot, (name, _inst) in slots.items():
            if not name:
                continue
            label = ItemRules.SLOT_LABELS.get(slot, slot)
            options.append(_opt(f"{label}：{name}", slot, "已装备", "装备栏"))
        main = getattr(player, "main_technique", "") or ""
        if main:
            options.append(_opt(f"心法：{main}", "心法", "已装备", "功法"))
        for name in player.get_techniques_list():
            if name:
                options.append(_opt(f"功法：{name}", name, "已装备", "功法"))
    except Exception:
        pass
    if not options:
        return [], "当前没有已装备的物品"
    return options, ""


async def _src_storage_rings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        current = player.storage_ring
        current_cap = plugin.storage_ring_mgr.get_ring_capacity(current)
        for ring in plugin.storage_ring_mgr.get_all_storage_rings():
            name = ring.get("name", "")
            cap = _int(ring.get("capacity"))
            need = _int(ring.get("required_level_index"))
            if not name or name == current or cap <= current_cap:
                continue
            if _int(player.level_index) < need:
                continue
            options.append(
                _opt(
                    f"{name}（{cap} 格）",
                    name,
                    f"{ring.get('rank', '')} · 需境界 {need} · {_nm(ring.get('price', 0))} 灵石",
                    "可更换储物戒",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "暂无可更换的更高容量储物戒"
    return options, ""


async def _src_ring_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        for name, count in (player.get_storage_ring_items() or {}).items():
            if _int(count) > 0:
                options.append(_opt(name, name, f"x{_int(count)}", "可堆叠物品"))
        counts = plugin.equipment_handler.equipment_manager.instance_manager.storage_counts(player)
        for name, count in counts.items():
            if _int(count) > 0:
                options.append(_opt(name, name, f"x{_int(count)}（装备实例）", "装备"))
    except Exception:
        pass
    if not options:
        return [], "储物戒是空的"
    options.sort(key=_sort_key)
    return options, ""


async def _src_warehouse_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """藏宝阁（洞天仓库）物品；/取出 只从这里搬回储物戒。"""
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        warehouse = plugin.warehouse_handler.warehouse_manager
        for name, count in (warehouse.get_items(player) or {}).items():
            if _int(count) > 0:
                options.append(_opt(name, name, f"x{_int(count)}", "可堆叠物品"))
        counts: Dict[str, int] = {}
        for inst in warehouse.instance_manager.list_instances(player, "warehouse"):
            name = str(inst.get("name") or "")
            if name:
                counts[name] = counts.get(name, 0) + 1
        for name, count in counts.items():
            if _int(count) > 0:
                options.append(_opt(name, name, f"x{_int(count)}（装备实例）", "装备"))
    except Exception:
        pass
    if not options:
        return [], "藏宝阁是空的，或你还没有修建藏宝阁"
    options.sort(key=_sort_key)
    return options, ""


async def _src_warehouse_categories(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """一键转移 / 一键取出支持粗分类与材料细分，留空 = 全部。"""
    try:
        from ..core.warehouse_manager import MATERIAL_CATEGORIES, TRANSFER_GROUPS
    except Exception:
        # 候选接口不应因重模块导入失败而整段消失，保留与仓库管理器一致的默认分类
        TRANSFER_GROUPS = ("材料", "符箓", "丹方", "装备")
        MATERIAL_CATEGORIES = ("灵植", "矿材", "妖兽", "灵液", "特殊")
    options: List[dict[str, str]] = []
    for name in TRANSFER_GROUPS:
        options.append(_opt(name, name, "按大类整批搬运", "粗分类"))
    for name in MATERIAL_CATEGORIES:
        options.append(_opt(name, name, "只搬运该细分材料", "材料细分"))
    return options, ""


async def _src_warehouse_upgrade_modes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """仓库扩建支持次数与 all；留空等于扩 1 次。"""
    return [
        _opt("扩建 1 次", "1", "单次扩建（+50 格）", "扩建次数"),
        _opt("连扩 3 次", "3", "灵石不足时停在可负担的次数", "扩建次数"),
        _opt("连扩 5 次", "5", "灵石不足时停在可负担的次数", "扩建次数"),
        _opt("扩到当前上限（all）", "all", "尽量扩到洞天品质允许的最高等级", "扩建次数"),
    ], ""


async def _src_pills_inventory(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        pills = getattr(plugin, "pill_handler", None)
        pm = pills.pill_manager if pills is not None else None
        for key, count in (player.get_pills_inventory() or {}).items():
            if _int(count) <= 0:
                continue
            base, quality = (pm.split_pill_key(key) if pm else (key, ""))
            label = f"{base}·{quality}" if quality and quality != "一般" else base
            value = f"{base}|{quality}" if quality and quality != "一般" else base
            data = (getattr(plugin.config_manager, "pills_data", {}) or {}).get(base, {})
            desc = f"x{_int(count)}"
            if data.get("rank"):
                desc += f" · {data.get('rank')}"
            options.append(_opt(label, value, desc, "丹药背包"))
    except Exception:
        pass
    if not options:
        return [], "丹药背包是空的"
    options.sort(key=_sort_key)
    return options, ""


async def _src_recycle_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    ring_opts, _ = await _src_ring_items(plugin, player, field, values)
    pill_opts, _ = await _src_pills_inventory(plugin, player, field, values)
    options = _dedupe(ring_opts + pill_opts)
    if not options:
        return [], "储物戒和丹药背包里都没有可回收的物品"
    return options, ""


async def _src_gift_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options, msg = await _src_recycle_items(plugin, player, field, values)
    if not options:
        return options, msg
    # 灵石可直接转账，放在最前面
    options.insert(0, _opt("灵石（直接转账）", "灵石", "无需对方确认", "货币"))
    return options, ""


async def _src_pill_names(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    groups = (
        ("丹药", getattr(plugin.config_manager, "pills_data", {}) or {}),
        ("经验丹", getattr(plugin.config_manager, "exp_pills_data", {}) or {}),
        ("功能丹", getattr(plugin.config_manager, "utility_pills_data", {}) or {}),
    )
    for group, data in groups:
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            desc = " · ".join(
                str(x) for x in (cfg.get("rank"), cfg.get("subtype"), cfg.get("effect")) if x
            )
            options.append(_opt(name, name, desc, group))
    if not options:
        return [], "没有可查询的丹药"
    options.sort(key=_sort_key)
    return options[:MAX_OPTIONS], ""


async def _src_item_names(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    groups = (
        ("物品", getattr(plugin.config_manager, "items_data", {}) or {}),
        ("武器", getattr(plugin.config_manager, "weapons_data", {}) or {}),
        ("功法", getattr(plugin.config_manager, "techniques_data", {}) or {}),
        ("丹药", getattr(plugin.config_manager, "pills_data", {}) or {}),
        ("储物戒", getattr(plugin.config_manager, "storage_rings_data", {}) or {}),
    )
    for group, data in groups:
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            rank = cfg.get("rank") or cfg.get("type") or ""
            options.append(_opt(name, name, str(rank), group))
    if not options:
        return [], "暂无可查询的物品"
    options.sort(key=_sort_key)
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]
    return options, ""


async def _src_pill_pavilion_sections(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """丹阁必须先选分区；直接 /丹阁 只会返回分区说明。"""
    return [
        _opt("丹药（成品丹）", "丹药", "丹阁默认分区，成品丹药按库存出售", "丹阁分区"),
        _opt("丹方（随机卷轴）", "丹方", "每轮随机刷新 10 个已学未学丹方卷轴", "丹阁分区"),
        _opt("突破丹方（常驻）", "突破丹方", "全部突破丹方，常驻不限量", "丹阁分区"),
        _opt("材料（炼丹材料）", "材料", "灵植 / 矿材 / 妖兽 / 灵液等炼丹材料", "丹阁分区"),
    ], ""


async def _src_talisman_pavilion_sections(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """符阁必须先选分区；直接 /符阁 只会返回分区说明。"""
    return [
        _opt("符箓（成品符）", "符箓", "凡品与黄品成品符，可直接使用", "符阁分区"),
        _opt("符图（玄品以上图纸）", "符图", "玄品以上符箓的绘制图纸，护劫符图常驻", "符阁分区"),
    ], ""


async def _src_shop_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        from ..handlers.shop_handler import BUY_SEARCH_PAVILION_IDS

        pavilion_names = {
            "pill_pavilion": "丹阁·丹药",
            "recipe_pavilion": "丹阁·丹方",
            "breakthrough_recipe_pavilion": "丹阁·突破丹方",
            "material_pavilion": "丹阁·材料",
            "weapon_pavilion": "器阁",
            "treasure_pavilion": "百宝阁",
            "talisman_pavilion": "符阁·符箓",
            "talisman_recipe_pavilion": "符阁·符图",
        }
        for pid in BUY_SEARCH_PAVILION_IDS:
            try:
                _last, items = await plugin.db.get_shop_data(pid)
            except Exception:
                continue
            for it in items or []:
                name = str(it.get("name") or "").strip()
                if not name:
                    continue
                stock = it.get("stock")
                if not it.get("unlimited") and _int(stock) <= 0:
                    continue
                desc = f"{_nm(it.get('price', 0))} 灵石"
                if it.get("unlimited"):
                    desc += " · 不限量"
                else:
                    desc += f" · 库存 {_int(stock)}"
                options.append(_opt(name, name, desc, pavilion_names.get(pid, pid)))
    except Exception:
        pass
    options = _dedupe(options)
    if not options:
        return [], "当前货架没有在售物品（货架每小时刷新）"
    options.sort(key=_sort_key)
    return options[:MAX_OPTIONS], ""


async def _src_learned_recipes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        mgr = plugin.alchemy_mgr
        learned = await mgr.ensure_learned_recipes(player)
        for rid in learned:
            recipe = mgr.recipes.get(_int(rid))
            if not recipe:
                continue
            rate = recipe.get("success_rate", 0)
            options.append(
                _opt(
                    f"[{_int(rid)}] {recipe.get('name')}",
                    _int(rid),
                    f"成功率 {float(rate):.0f}% · 产出 {recipe.get('output_pill', '')}",
                    "已学丹方",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "还没有已学丹方，请先去 /丹阁 丹方 购买"
    options.sort(key=lambda x: _int(x["value"]))
    return options, ""


async def _src_all_recipes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        mgr = plugin.alchemy_mgr
        learned = set(_int(x) for x in (mgr.get_learned_recipe_ids(player) if player else []))
        for rid, recipe in sorted(mgr.recipes.items()):
            if rid in learned:
                continue
            options.append(
                _opt(
                    f"[{rid}] {recipe.get('name')}",
                    rid,
                    f"需境界 {recipe.get('level_required', 0)} · 价格 {_nm(recipe.get('price', 0))}",
                    "可学习丹方",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "没有可学习的丹方"
    return options, ""


async def _src_fire_choices(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    return [
        _opt("猛火（成功率高）", "猛火", "适合冲成功率", "火候"),
        _opt("文火（品质更稳）", "文火", "适合保品质", "火候"),
        _opt("守中（均衡）", "守中", "成功与品质折中", "火候"),
    ], ""


async def _src_stock_codes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    try:
        from ..managers.invest_manager import STOCK_DEFS

        options = [
            _opt(f"{d[0]} {d[1]}", d[0], f"{d[2]} · 基准 {d[3]} 灵石", "股票")
            for d in STOCK_DEFS
        ]
        return options, ""
    except Exception:
        return [], "股票列表暂时不可用"


async def _src_fund_codes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    try:
        from ..managers.invest_manager import FUND_DEFS

        risk = {1: "低风险", 2: "中风险", 3: "高风险"}
        options = [
            _opt(f"{d[0]} {d[1]}", d[0], f"{risk.get(d[2], '未知风险')} · 净值 {d[3]}", "基金")
            for d in FUND_DEFS
        ]
        return options, ""
    except Exception:
        return [], "基金列表暂时不可用"


async def _src_market_codes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """行情必须带标的代码；把股票和基金合并成一份可筛选列表。"""
    stock_opts, stock_msg = await _src_stock_codes(plugin, player, field, values)
    fund_opts, fund_msg = await _src_fund_codes(plugin, player, field, values)
    options = _dedupe(stock_opts + fund_opts)
    if not options:
        return [], stock_msg or fund_msg or "仙市标的暂时不可用"
    return options, ""


async def _src_stock_holdings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        rows = await plugin.invest_mgr._holdings_rows(player.user_id)
        for row in rows:
            if row.get("kind") != "stock":
                continue
            shares = float(row.get("shares") or 0)
            lots = int(shares // 100)
            if lots <= 0:
                continue
            options.append(
                _opt(
                    f"{row.get('code')} {row.get('name')}（{lots} 手）",
                    row.get("code"),
                    f"持有 {shares:.0f} 股 · 现价 {row.get('price')}",
                    "持仓股票",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "没有可卖出的股票持仓"
    return options, ""


async def _src_fund_holdings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        rows = await plugin.invest_mgr._holdings_rows(player.user_id)
        for row in rows:
            if row.get("kind") != "fund":
                continue
            shares = float(row.get("shares") or 0)
            if shares <= 0:
                continue
            options.append(
                _opt(
                    f"{row.get('code')} {row.get('name')}（{shares:.2f} 份）",
                    row.get("code"),
                    f"现价 {row.get('price')}",
                    "持仓基金",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "没有可赎回的基金持仓"
    return options, ""


async def _src_my_listings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        async with plugin.db.conn.execute(
            "SELECT * FROM auction_items WHERE seller_id = ? ORDER BY id DESC",
            (player.user_id,),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            options.append(
                _opt(
                    f"[坊市#{row['id']}] {row.get('item_name')} x{row.get('count')}",
                    row["id"],
                    f"单价 {_nm(row.get('price'))} · 总价 {_nm(_int(row.get('price')) * _int(row.get('count')))}",
                    "我的上架",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "你当前没有在坊市上架商品"
    return options, ""


async def _src_market_listings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        async with plugin.db.conn.execute(
            "SELECT * FROM auction_items ORDER BY id DESC LIMIT 60"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            options.append(
                _opt(
                    f"[#{row['id']}] {row.get('item_name')} x{row.get('count')}",
                    row["id"],
                    f"单价 {_nm(row.get('price'))} · 卖家 {str(row.get('seller_id', ''))[:12]}",
                    "坊市在售",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "坊市目前没有在售商品"
    return options, ""


async def _src_market_offers(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        import time as _time

        now = int(_time.time())
        async with plugin.db.conn.execute(
            "SELECT * FROM auction_npc_offers WHERE seller_id = ? AND status = 'pending' "
            "AND expire_time > ? ORDER BY id",
            (player.user_id, now),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            remain = max(0, _int(row.get("expire_time")) - now)
            options.append(
                _opt(
                    f"[问价#{row['id']}] {row.get('item_name')} x{row.get('count')}",
                    row["id"],
                    f"{row.get('npc_name', 'NPC')} 出价 {_nm(row.get('offer_price'))}/件 · 剩 {remain // 60} 分钟",
                    "待处理问价",
                )
            )
        if field.get("allow_all"):
            options.insert(0, _opt("全部卖出（all）", "all", "一次接受所有待处理问价", "快捷"))
    except Exception:
        pass
    if not options:
        return [], "当前没有待处理的 NPC 问价"
    return options, ""


async def _src_black_market(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        items, _next = plugin.gamble_mgr._get_black_market_items()
        for idx, item in enumerate(items, 1):
            desc = f"{_nm(item.get('price', 0))} 灵石"
            if item.get("rank"):
                desc += f" · {item.get('rank')}"
            if item.get("desc"):
                desc += f" · {item.get('desc')}"
            options.append(_opt(f"[{idx}] {item.get('name')}", idx, desc, "黑市"))
    except Exception:
        pass
    if not options:
        return [], "黑市暂时没有商品"
    return options, ""


async def _src_rift_ids(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        rifts = await plugin.db.ext.get_all_rifts()
        for rift in rifts:
            need = _int(getattr(rift, "required_level", 0))
            rewards = rift.get_rewards() if hasattr(rift, "get_rewards") else {}
            exp_range = rewards.get("exp", [0, 0])
            options.append(
                _opt(
                    f"{getattr(rift, 'rift_name', '秘境')}（ID:{getattr(rift, 'rift_id', '')}）",
                    getattr(rift, "rift_id", ""),
                    f"需境界 {need or '无限制'} · 修为 {_nm(exp_range[0])}-{_nm(exp_range[1])}",
                    "秘境",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前没有开放的秘境"
    return options, ""


async def _src_adventure_routes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        level = _int(getattr(player, "level_index", 0)) if player else 0
        for route in plugin.adventure_mgr.get_route_overview():
            if level < _int(route.get("min_level", 0)):
                continue
            options.append(
                _opt(
                    route.get("name", ""),
                    route.get("name", ""),
                    f"风险 {route.get('risk', '?')} · {_int(route.get('duration', 0)) // 60} 分钟 · {route.get('description', '')}",
                    "可用路线",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前境界没有可走的历练路线"
    return options, ""


async def _src_bounty_ids(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        for b in await plugin.bounty_mgr.get_bounty_list(player):
            reward = b.get("reward") or {}
            options.append(
                _opt(
                    f"[{b.get('id')}] {b.get('name')}（{b.get('difficulty_name', '')}）",
                    b.get("id"),
                    f"目标 x{b.get('count')} · 灵石 {_nm(reward.get('stone', 0))} · 修为 {_nm(reward.get('exp', 0))}",
                    "悬赏令",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前没有可接取的悬赏"
    options.sort(key=lambda x: _int(x["value"]))
    return options, ""


async def _src_spirit_eyes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        for eye in await plugin.spirit_eye_mgr.get_available_spirit_eyes():
            options.append(
                _opt(
                    f"{eye.get('eye_name')}（ID:{eye.get('eye_id')}）",
                    eye.get("eye_id"),
                    f"每小时修为 +{_nm(eye.get('exp_per_hour', 0))}",
                    "无主灵眼",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前没有无主灵眼"
    return options, ""


async def _src_sect_names(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        for sect in await plugin.db.ext.get_all_sects():
            try:
                level = plugin.sect_mgr.get_sect_level(sect)
            except Exception:
                level = "?"
            options.append(
                _opt(
                    getattr(sect, "sect_name", ""),
                    getattr(sect, "sect_name", ""),
                    f"宗门等级 Lv.{level} · 建设度 {getattr(sect, 'sect_scale', 0)}",
                    "宗门列表",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前还没有任何宗门"
    return options, ""


async def _src_sect_members(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    if not _int(getattr(player, "sect_id", 0)):
        return [], "你还未加入宗门"
    options: List[dict[str, str]] = []
    try:
        members = await plugin.db.ext.get_sect_members(player.sect_id)
        positions = getattr(plugin.sect_mgr, "POSITIONS", {})
        for m in members:
            uid = str(getattr(m, "user_id", ""))
            if not uid or uid == player.user_id:
                continue
            name = getattr(m, "user_name", "") or uid
            title = positions.get(_int(getattr(m, "sect_position", 4)), "")
            options.append(
                _opt(
                    f"{name}（{title}）",
                    uid,
                    f"{_realm(m, plugin)} · 贡献 {_nm(getattr(m, 'sect_contribution', 0))}",
                    "宗门成员",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "宗门里没有其他成员"
    return options, ""


async def _src_sect_positions(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        positions = getattr(plugin.sect_mgr, "POSITIONS", {})
        for pid in (1, 2, 3, 4):
            if pid in positions:
                options.append(_opt(positions[pid], pid, f"职位 ID：{pid}", "可选职位"))
    except Exception:
        pass
    return options, ""


async def _src_land_types(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    try:
        from ..managers.blessed_land_manager import BLESSED_LANDS

        options = [
            _opt(
                f"{tid} {cfg['name']}",
                tid,
                f"价格 {_nm(cfg['price'])} 灵石 · 修炼 +{cfg['exp_bonus']:.0%} · 满级 Lv.{cfg['max_level']}",
                "洞天类型",
            )
            for tid, cfg in BLESSED_LANDS.items()
        ]
        return options, ""
    except Exception:
        return [], "洞天配置暂时不可用"


async def _src_land_advance_types(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        from ..managers.blessed_land_manager import BLESSED_LANDS

        land = await plugin.blessed_land_mgr.get_user_blessed_land(player.user_id)
        current = _int((land or {}).get("land_type", 0))
        if not land:
            return [], "你还没有洞天，请先 /购买洞天"
        for tid, cfg in BLESSED_LANDS.items():
            if tid <= current:
                continue
            options.append(
                _opt(
                    f"{tid} {cfg['name']}",
                    tid,
                    f"修炼 +{cfg['exp_bonus']:.0%} · 满级 Lv.{cfg['max_level']}",
                    "进阶目标",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前已是最高阶洞天"
    return options, ""


async def _src_upgrade_modes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options = [
        _opt("升 1 级", "1", "单次升级", "升级方式"),
        _opt("连升 5 级", "5", "灵石不足时自动停在可升等级", "升级方式"),
        _opt("升到满级（all）", "all", "尽量升到当前洞天上限", "升级方式"),
    ]
    try:
        land = await plugin.blessed_land_mgr.get_user_blessed_land(player.user_id) if player else None
        level = _int((land or {}).get("level", 1))
        for target in (5, 10, 15, 20, 30):
            if target > level:
                options.append(_opt(f"升到 Lv.{target}", f"1-{target}", "按目标等级升级", "升级方式"))
                break
    except Exception:
        pass
    return options, ""


async def _src_farm_herbs(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        from ..managers.spirit_farm_manager import (
            SPIRIT_HERBS,
            get_herb_unlock_level,
            requires_seed,
        )

        farm = await plugin.spirit_farm_mgr.get_user_farm(player.user_id)
        if not farm:
            return [], "你还没有灵田，请先 /开垦灵田"
        level = _int(farm.get("level", 1))
        for name, cfg in SPIRIT_HERBS.items():
            need = get_herb_unlock_level(name)
            if level < need:
                continue
            if requires_seed(name):
                have = plugin.storage_ring_mgr.get_item_count(player, name)
                if have <= 0:
                    continue
                desc = f"需杂交种苗（持有 {have}）"
            else:
                desc = "普通灵草"
            hours = max(1, _int(cfg.get("grow_time", 3600)) // 3600)
            desc += f" · {hours} 小时 · 修为 +{_nm(cfg.get('exp_yield', 0))}"
            options.append(_opt(name, name, desc, "可种植灵草"))
    except Exception:
        pass
    if not options:
        return [], "当前没有可种植的灵草"
    return options, ""


async def _src_farm_slots(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        import time as _time

        farm = await plugin.spirit_farm_mgr.get_user_farm(player.user_id)
        if not farm:
            return [], "你还没有灵田"
        now = int(_time.time())
        for crop in farm.get("crops") or []:
            slot = _int(crop.get("slot"))
            if slot <= 0:
                continue
            mature = _int(crop.get("mature_time", 0))
            state = "已成熟" if now >= mature else "生长中"
            options.append(_opt(f"第 {slot} 格：{crop.get('name')}", slot, state, "灵田作物"))
        options.insert(0, _opt("全部铲除（all）", "all", "清空所有作物", "快捷"))
    except Exception:
        pass
    if len(options) <= 1:
        return [], "灵田里没有可铲除的作物"
    return options, ""


async def _src_fate_shop(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    points = _int(getattr(player, "fate_points", 0)) if player else 0
    return [
        _opt("修炼加速 +10%", 1, f"消耗 100 仙缘点 · 当前 {points}", "仙缘商店"),
        _opt("初始灵石 10,000", 2, f"消耗 50 仙缘点 · 当前 {points}", "仙缘商店"),
        _opt("大道馈赠（全属性）", 3, f"消耗 500 仙缘点 · 当前 {points}", "仙缘商店"),
    ], ""


async def _src_virtue_shop(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    points = _int(getattr(player, "virtue_points", 0)) if player else 0
    return [
        _opt("引雷符", 1, f"消耗 80 师德点 · 当前 {points}", "师德商店"),
        _opt("空间石 x2", 2, f"消耗 150 师德点 · 当前 {points}", "师德商店"),
        _opt("称号【传道真人】", 3, f"消耗 300 师德点 · 当前 {points}", "师德商店"),
    ], ""


async def _src_encounter_choices(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    try:
        raw = await plugin.db.ext.get_system_config(f"encounter_{player.user_id}")
        payload = json.loads(raw) if raw else None
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return [], "你当前没有进行中的奇遇"
    key = payload.get("key")
    if key == "__merit__":
        return [
            _opt("[1] 分出四成收获接济他们", 1, "获得功德", "途中善举"),
            _opt("[2] 收下全部收获继续赶路", 2, "不损收获", "途中善举"),
        ], ""
    event = (getattr(plugin.encounter_mgr, "events", {}) or {}).get(key)
    if not event:
        return [], "奇遇事件已失效，请重新触发"
    options = []
    for idx, opt in enumerate(event.get("options") or [], 1):
        options.append(_opt(f"[{idx}] {opt.get('label', '选项')}", idx, opt.get("desc", ""), event.get("title", "奇遇")))
    if not options:
        return [], "当前奇遇没有可选分支"
    return options, ""


async def _src_players(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    self_id = str(getattr(player, "user_id", "")) if player else ""
    try:
        players = await plugin.db.get_all_players()
        players.sort(key=lambda p: (-_int(getattr(p, "level_index", 0)), str(getattr(p, "user_name", ""))))
        for p in players:
            uid = str(getattr(p, "user_id", ""))
            if not uid or uid == self_id:
                continue
            name = getattr(p, "user_name", "") or uid
            options.append(
                _opt(
                    name,
                    uid,
                    f"{_realm(p, plugin)} · {getattr(p, 'cultivation_type', '')}",
                    "道友",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "全服还没有其他道友"
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]
    return options, ""


async def _src_cultivation_types(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    # 常量直接内置一份，避免候选接口因较重模块导入失败而整段消失
    names = ["灵修", "体修", "魔修", "剑修", "鬼修", "佛修", "御兽"]
    roles: Dict[str, dict] = {}
    try:
        from ..handlers.player_handler import CLASS_ROLE_INFO, VALID_CULTIVATIONS

        names = list(VALID_CULTIVATIONS) or names
        roles = dict(CLASS_ROLE_INFO)
    except Exception:
        pass
    options = []
    for name in names:
        role = roles.get(name, {})
        desc = " · ".join(str(x) for x in (role.get("name"), role.get("desc")) if x)
        options.append(_opt(name, name, desc or "七大职业", "七大职业"))
    return options, ""


async def _src_spirit_roots(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    for prefix, label in (("create_roots", "创号灵根"), ("rebirth_options", "重修灵根")):
        try:
            raw = await plugin.db.ext.get_system_config(f"{prefix}_{player.user_id}")
            data = json.loads(raw) if raw else None
        except Exception:
            data = None
        if not isinstance(data, dict):
            continue
        roots = data.get("roots") or []
        options = []
        for idx, root in enumerate(roots, 1):
            name = str(root).replace("灵根", "")
            try:
                speed = plugin.player_handler.cultivation_manager._get_root_speed(name)
                desc = plugin.player_handler.cultivation_manager._get_root_description(name)
                desc = f"{desc} · 修炼 ×{float(speed):.2f}"
            except Exception:
                desc = ""
            options.append(_opt(f"{idx}. 【{name}灵根】", idx, desc, label))
        if options:
            return options, ""
    return [], "当前没有待选择的灵根，请先发送 /我要修仙 职业"


async def _src_proxy_rifts(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    """自动代理秘境只能选当前境界可进入的本，避免选了也开不了。"""
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        level = _int(getattr(player, "level_index", 0))
        for rift in await plugin.db.ext.get_all_rifts():
            need = _int(getattr(rift, "required_level", 0))
            if level < need:
                continue
            options.append(
                _opt(
                    f"{getattr(rift, 'rift_name', '秘境')}（ID:{getattr(rift, 'rift_id', '')}）",
                    getattr(rift, "rift_id", ""),
                    f"需境界 {need or '无限制'} · 到期自动结算并重新进入",
                    "可代理秘境",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前境界没有可代理的秘境"
    options.sort(key=lambda x: _int(x["value"]))
    return options, ""


async def _src_proxy_tasks(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    try:
        from ..managers.auto_proxy_manager import TASK_NAMES

        options = [_opt(name, name, "自动结算并续接", "代理活动") for name in TASK_NAMES.values()]
        return options, ""
    except Exception:
        return [], "自动代理配置暂时不可用"


async def _src_proxy_off_tasks(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options, msg = await _src_proxy_tasks(plugin, player, field, values)
    if not options:
        return options, msg
    options.insert(0, _opt("全部关闭", "全部", "关闭所有自动代理", "快捷"))
    return options, ""


# ---------------------------------------------------------------------------
# v7.0.2 新增：生活职业 / 符箓 / 炼器 / 宗门扩展 / 任务 / 灵兽 / 社交
# ---------------------------------------------------------------------------


async def _src_life_skills(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    from ..managers.life_skill_manager import LIFE_SKILLS

    current = str(getattr(player, "life_skill", "") or "") if player else ""
    options = []
    for name, cfg in LIFE_SKILLS.items():
        desc = str(cfg.get("desc", ""))
        if name == current:
            desc = "当前职业 · " + desc
        options.append(_opt(name, name, desc, "生活职业"))
    return options, ""


async def _src_talisman_inventory(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        recipes = getattr(plugin.config_manager, "talisman_recipes_data", {}) or {}
        for name, count in (player.get_storage_ring_items() or {}).items():
            if _int(count) <= 0 or name not in recipes:
                continue
            recipe = recipes.get(name) or {}
            desc = " · ".join(
                str(x) for x in (recipe.get("rank"), recipe.get("category")) if x
            )
            options.append(_opt(name, name, f"x{_int(count)}" + (f" · {desc}" if desc else ""), "符箓"))
    except Exception:
        pass
    if not options:
        return [], "储物戒里没有可用符箓"
    options.sort(key=_sort_key)
    return options, ""


async def _src_forge_actions(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    # 炼器入口目前只开放「妖兽材料定向洗练」，`洗练` 与 `定向洗练` 两个词都合法
    return [
        _opt("定向洗练（指定词条）", "洗练", "消耗洗练石 + 妖兽材料，指定部位与词条", "炼器方式"),
    ], ""


async def _src_wash_slots(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        from ..core.item_rules import ItemRules

        slots = plugin.equipment_handler.equipment_manager.get_gear_slots(player)
        for slot, (name, _inst) in slots.items():
            if not name:
                continue
            label = ItemRules.SLOT_LABELS.get(slot, slot)
            options.append(_opt(f"{label}：{name}", label, "已装备可洗练", "装备部位"))
    except Exception:
        pass
    if not options:
        return [], "当前没有已装备的装备，请先 /装备"
    return options, ""


async def _src_forge_affixes(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        for affix in plugin.forge_mgr.AFFIX_POOL:
            options.append(
                _opt(affix.get("name", ""), affix.get("name", ""), affix.get("desc", ""), "可指定词条")
            )
    except Exception:
        pass
    if not options:
        return [], "词条池暂时不可用"
    return options, ""


async def _src_monster_materials(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        catalog = getattr(plugin.config_manager, "material_catalog", {}) or {}
        ring = player.get_storage_ring_items() or {}
        for name, count in ring.items():
            if _int(count) <= 0:
                continue
            info = catalog.get(name) or {}
            if info.get("category") != "妖兽":
                continue
            options.append(
                _opt(name, name, f"x{_int(count)} · {info.get('tier', '')}", "储物戒·妖兽材料")
            )
    except Exception:
        pass
    if not options:
        return [], "储物戒里没有妖兽材料，可先在 /坊市 或 /百宝阁 获取"
    options.sort(key=_sort_key)
    return options, ""


async def _src_sect_shop_items(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    if not _int(getattr(player, "sect_id", 0)):
        return [], "你还未加入宗门"
    options: List[dict[str, str]] = []
    try:
        _shop_id, _ts, items = await plugin.sect_shop_handler._get_shop(player.sect_id, player)
        for idx, item in enumerate(items or [], 1):
            stock = _int(item.get("stock", 0))
            if stock <= 0:
                continue
            options.append(
                _opt(
                    f"[{idx}] {item.get('name', '')}",
                    idx,
                    f"{_nm(item.get('price', 0))} 贡献 · {item.get('rank', '')} · 库存 {stock}",
                    "宗门商店",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "宗门商店当前没有可兑换商品"
    return options, ""


async def _src_sect_buildings(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        from ..managers.sect_manager import SECT_BUILDINGS

        for name, cfg in SECT_BUILDINGS.items():
            options.append(
                _opt(
                    name,
                    name,
                    f"{cfg.get('desc', '')} · 满级 Lv.{cfg.get('max_level', 0)}",
                    "宗门建筑",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "宗门建筑配置暂时不可用"
    return options, ""


async def _src_sect_order_types(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    return [
        _opt("捐献灵石", "捐献", "要求成员向宗门捐献指定灵石", "命令类型"),
        _opt("完成宗门任务", "宗门任务", "要求成员完成指定数量的宗门任务", "命令类型"),
    ], ""


async def _src_sect_orders(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        async with plugin.db.conn.execute(
            "SELECT id, order_type, target_value, deadline FROM sect_orders "
            "WHERE issuer_id = ? AND status = 'active' ORDER BY id DESC",
            (player.user_id,),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        import time as _time

        now = int(_time.time())
        type_names = {"donate": "捐献", "task": "宗门任务"}
        for row in rows:
            remain = max(0, _int(row.get("deadline")) - now)
            options.append(
                _opt(
                    f"[#{row['id']}] {type_names.get(row.get('order_type'), row.get('order_type'))}"
                    f" {_nm(row.get('target_value', 0))}",
                    row["id"],
                    f"剩 {remain // 3600} 小时",
                    "进行中命令",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "你当前没有进行中的宗门命令"
    return options, ""


async def _src_task_levels(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    return [
        _opt("黄阶", "黄", "最低品阶", "任务品阶"),
        _opt("玄阶", "玄", "中低品阶", "任务品阶"),
        _opt("地阶", "地", "中高品阶", "任务品阶"),
        _opt("天阶", "天", "最高品阶", "任务品阶"),
        _opt("一键接取全部（all）", "all", "今日所有可接任务", "快捷"),
    ], ""


async def _src_active_tasks(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        rows = await plugin.db.ext.get_user_active_tasks(player.user_id)
        for row in rows:
            options.append(
                _opt(
                    f"[#{row.get('id')}] [{row.get('task_level')}阶] {row.get('title', '')}",
                    row.get("id"),
                    f"进度 {row.get('current_progress', 0)}/{row.get('target_count', 0)}",
                    "进行中任务",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "你当前没有进行中的日常任务"
    return options, ""


async def _src_weekly_runs(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        mgr = plugin.weekly_boss_mgr
        cycle = await mgr.ensure_current_cycle()
        rows = await mgr._fetchall(
            "SELECT * FROM weekly_boss_runs WHERE cycle_id = ? AND status = 0 ORDER BY run_id DESC",
            (cycle.get("cycle_id"),),
        )
        for row in rows:
            options.append(
                _opt(
                    f"#{row.get('run_id')} {row.get('team_name', '')}",
                    row.get("run_id"),
                    f"{row.get('member_count', 0)}/{mgr.max_players} 人 · 等待加入",
                    "周本队伍",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "当前没有可加入的周本队伍，可先 /周本创建"
    return options, ""


async def _src_spirit_companions(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    try:
        from ..managers.social_manager import SPIRIT_COMPANIONS
    except Exception:
        return [], "仙灵名录暂时不可用"
    options = []
    for name, cfg in SPIRIT_COMPANIONS.items():
        options.append(
            _opt(
                f"{name}（{cfg.get('title', '')}）",
                name,
                f"{cfg.get('perk', '')} · {cfg.get('desc', '')}",
                "仙灵",
            )
        )
    return options, ""


async def _src_owned_titles(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        from ..managers.title_data import TIERS

        owned = plugin.title_mgr._load_owned(player)
        worn = str(getattr(player, "title", "") or "")
        stat_names = {
            "atk": "物伤", "def": "物攻", "magic": "法伤",
            "phys_def": "物防", "magic_def": "法防", "crit": "会心",
            "lifesteal": "吸血", "exp": "修为", "luck": "气运",
        }
        for name in owned:
            data = (plugin.title_mgr.titles or {}).get(name) or {}
            tier_name = (TIERS.get(data.get("tier"), ("",)) or [""])[0]
            stats = data.get("stats") or {}
            detail = " · ".join(
                f"{stat_names.get(key, key)}+{value}" for key, value in stats.items()
            )
            desc = " · ".join(x for x in (tier_name, detail) if x)
            if name == worn:
                desc = ("当前佩戴 · " + desc) if desc else "当前佩戴"
            options.append(_opt(name, name, desc, "已拥有称号"))
    except Exception:
        pass
    if not options:
        return [], "你还没有任何称号"
    options.sort(key=_sort_key)
    if len(options) > MAX_OPTIONS:
        options = options[:MAX_OPTIONS]
    return options, ""


async def _src_disciples(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        rows = await plugin.social_mgr.get_disciples(player.user_id)
        for row in rows:
            uid = str(row.get("disciple_id") or "")
            if not uid:
                continue
            d = await _player(plugin, uid)
            name = (getattr(d, "user_name", "") if d else "") or uid
            realm = _realm(d, plugin) if d else "未知境界"
            options.append(_opt(name, uid, f"{realm} · 在学弟子", "弟子"))
    except Exception:
        pass
    if not options:
        return [], "你当前没有在学弟子"
    return options, ""


async def _src_pet_types(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        base_types = getattr(plugin.pet_mgr, "BASE_PET_TYPES", {}) or {}
        for name, cfg in base_types.items():
            options.append(
                _opt(
                    f"{name}（{cfg.get('element', '')}）" if cfg.get("element") else name,
                    name,
                    str(cfg.get("desc", "")),
                    "灵兽种类",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "灵兽名录暂时不可用"
    return options, ""


async def _src_my_pets(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    if not player:
        return [], "尚未创建角色"
    options: List[dict[str, str]] = []
    try:
        pets = await plugin.pet_mgr.get_pets(player.user_id, apply_regen=False)
        for idx, pet in enumerate(pets, 1):
            name = str(pet.get("pet_name") or pet.get("pet_type") or f"灵兽{idx}")
            main = "出战" if _int(pet.get("is_main")) == 1 else "待命"
            options.append(
                _opt(
                    f"{idx}. {name}",
                    idx,
                    f"Lv.{pet.get('level', 1)} · {main}",
                    "我的灵兽",
                )
            )
    except Exception:
        pass
    if not options:
        return [], "你还没有灵兽，请先 /孵化灵兽"
    return options, ""


async def _src_furniture_names(plugin: "XiuXianPlugin", player: Any, field: dict, values: List[str]) -> tuple:
    options: List[dict[str, str]] = []
    try:
        from ..managers.blessed_land_manager import FURNITURES

        owned: List[str] = []
        if player:
            land = await plugin.blessed_land_mgr.get_user_blessed_land(player.user_id)
            owned = list((land or {}).get("furniture_list") or [])
        for name, cfg in FURNITURES.items():
            desc = f"{_nm(cfg.get('price', 0))} 灵石 · {cfg.get('desc', '')}"
            if name in owned:
                desc = "已拥有 · " + desc
            options.append(_opt(name, name, desc, "洞天家具"))
    except Exception:
        pass
    if not options:
        return [], "家具清单暂时不可用"
    return options, ""


SOURCES: Dict[str, Callable[..., Any]] = {
    "equip_items": _src_equip_items,
    "equipped_slots": _src_equipped_slots,
    "storage_rings": _src_storage_rings,
    "ring_items": _src_ring_items,
    "warehouse_items": _src_warehouse_items,
    "warehouse_categories": _src_warehouse_categories,
    "warehouse_upgrade_modes": _src_warehouse_upgrade_modes,
    "recycle_items": _src_recycle_items,
    "gift_items": _src_gift_items,
    "pills_inventory": _src_pills_inventory,
    "pill_names": _src_pill_names,
    "item_names": _src_item_names,
    "shop_items": _src_shop_items,
    "pill_pavilion_sections": _src_pill_pavilion_sections,
    "talisman_pavilion_sections": _src_talisman_pavilion_sections,
    "learned_recipes": _src_learned_recipes,
    "all_recipes": _src_all_recipes,
    "fire_choices": _src_fire_choices,
    "stock_codes": _src_stock_codes,
    "fund_codes": _src_fund_codes,
    "market_codes": _src_market_codes,
    "stock_holdings": _src_stock_holdings,
    "fund_holdings": _src_fund_holdings,
    "my_listings": _src_my_listings,
    "market_listings": _src_market_listings,
    "market_offers": _src_market_offers,
    "black_market": _src_black_market,
    "rift_ids": _src_rift_ids,
    "adventure_routes": _src_adventure_routes,
    "bounty_ids": _src_bounty_ids,
    "spirit_eyes": _src_spirit_eyes,
    "sect_names": _src_sect_names,
    "sect_members": _src_sect_members,
    "sect_positions": _src_sect_positions,
    "land_types": _src_land_types,
    "land_advance_types": _src_land_advance_types,
    "upgrade_modes": _src_upgrade_modes,
    "farm_herbs": _src_farm_herbs,
    "farm_slots": _src_farm_slots,
    "fate_shop": _src_fate_shop,
    "virtue_shop": _src_virtue_shop,
    "encounter_choices": _src_encounter_choices,
    "players": _src_players,
    "cultivation_types": _src_cultivation_types,
    "spirit_roots": _src_spirit_roots,
    "proxy_tasks": _src_proxy_tasks,
    "proxy_off_tasks": _src_proxy_off_tasks,
    "proxy_rifts": _src_proxy_rifts,
    # v7.0.2 扩展候选源
    "life_skills": _src_life_skills,
    "talisman_inventory": _src_talisman_inventory,
    "forge_actions": _src_forge_actions,
    "wash_slots": _src_wash_slots,
    "forge_affixes": _src_forge_affixes,
    "monster_materials": _src_monster_materials,
    "sect_shop_items": _src_sect_shop_items,
    "sect_buildings": _src_sect_buildings,
    "sect_order_types": _src_sect_order_types,
    "sect_orders": _src_sect_orders,
    "task_levels": _src_task_levels,
    "active_tasks": _src_active_tasks,
    "weekly_runs": _src_weekly_runs,
    "spirit_companions": _src_spirit_companions,
    "owned_titles": _src_owned_titles,
    "disciples": _src_disciples,
    "pet_types": _src_pet_types,
    "my_pets": _src_my_pets,
    "furniture_names": _src_furniture_names,
}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def plan_for(cmd: str, values: Optional[List[str]] = None) -> List[dict[str, Any]]:
    name = str(cmd or "").strip().lstrip("/").split()[0:1]
    key = name[0] if name else ""
    plan = FIELD_PLANS.get(key, [])
    first = str(values[0]).strip() if values else ""
    # /自动代理、/关闭代理 的第二段取决于所选活动：
    # 秘境必须补 ID，灵田/施肥可补格号或阈值，押镖/宗门任务无需补参。
    if key == "自动代理" and first == "秘境":
        plan = plan + [_f("rift", "选择要代理的秘境", "proxy_rifts")]
    elif key == "自动代理" and first == "灵田":
        plan = plan + [
            _f("plots", "代理格号（留空 = 全部，如 1-3）", "", optional=True)
        ]
    elif key == "自动代理" and first == "施肥":
        plan = plan + [
            _f("threshold", "施肥阈值 10-100（留空 = 默认）", "", optional=True)
        ]
    elif key == "关闭代理" and first == "灵田":
        plan = plan + [
            _f("plots", "要停止的格号（留空 = 全部，如 1-3）", "", optional=True)
        ]
    return plan


async def build_arg_options(
    plugin: "XiuXianPlugin",
    user_id: str,
    cmd: str,
    values: Optional[List[str]] = None,
) -> dict[str, Any]:
    """按指令和已填值返回下一段候选。"""
    cmd_name = str(cmd or "").strip().lstrip("/").split()[0:1]
    name = cmd_name[0] if cmd_name else ""
    spec = ARG_SPEC.get(name) or {}
    values = [str(v) for v in (values or []) if str(v).strip()]
    plan = plan_for(name, values)

    if not plan:
        return {
            "ok": False,
            "cmd": name,
            "reason": "no_options",
            "message": "该指令没有枚举候选，请直接输入",
            "options": [],
            "values": values,
            "min": spec.get("min", 1),
            "index": len(values),
            "done": len(values) >= spec.get("min", 1),
        }

    index = len(values)
    if index >= len(plan):
        return {
            "ok": True,
            "cmd": name,
            "index": index,
            "total": len(plan),
            "min": spec.get("min", 1),
            "values": values,
            "field": None,
            "options": [],
            "done": True,
            "message": "参数已填完，可以发送",
        }

    field = dict(plan[index])
    source = field.get("source") or ""
    player = await _player(plugin, user_id)
    options: List[dict[str, str]] = []
    message = ""
    if source:
        provider = SOURCES.get(source)
        if provider is not None:
            try:
                options, message = await provider(plugin, player, field, values)
            except Exception as exc:  # 单个候选源失败不能拖垮整个弹层
                options, message = [], f"候选读取失败：{exc}"
        else:
            message = "候选源未实现"

    options = _dedupe(options)
    if not options and source:
        message = message or "没有可选内容，可直接手动输入"

    return {
        "ok": True,
        "cmd": name,
        "index": index,
        "total": len(plan),
        "min": spec.get("min", 1),
        "values": values,
        "field": {
            "key": field.get("key", ""),
            "label": field.get("label", spec.get("hint", "")),
            "optional": bool(field.get("optional")),
        },
        "options": options,
        "done": False,
        "example": spec.get("example", ""),
        "hint": spec.get("hint", ""),
        "message": message,
    }
