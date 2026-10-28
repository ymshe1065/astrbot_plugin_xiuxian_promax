# handlers/pet_handlers.py
import time
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.pet_manager import PetManager
from ..models import Player
from .utils import player_required

class PetHandlers:
    def __init__(self, db: DataBase, pet_mgr: PetManager, config_manager=None):
        self.db = db
        self.pet_mgr = pet_mgr
        self.config_manager = config_manager

    async def _owner_stats(self, player: Player):
        if self.config_manager is not None:
            try:
                from ..managers.combat_manager import CombatManager
                stats = CombatManager.build_player_combat_stats(
                    player, self.config_manager, db=self.db
                )
                return max(1, int(stats.max_hp)), max(1, int(stats.atk))
            except Exception:
                pass
        owner_hp = max(1, int(player.hp or 0), player.experience // 2)
        owner_atk = max(1, int(player.atk or 0), player.experience // 10)
        return owner_hp, owner_atk

    def _pet_status(self, pet, max_hp: int, now: int) -> str:
        dead_until = int(pet.get("dead_until") or 0)
        if dead_until > now:
            remain = dead_until - now
            return f"💀 复活中（{remain // 60} 分 {remain % 60} 秒）"
        hp = int(pet.get("hp") or 0)
        if hp < max_hp:
            return "🌙 恢复中"
        return "✨ 精神饱满"

    @player_required
    async def handle_my_pet(self, player: Player, event: AstrMessageEvent):
        pets = await self.pet_mgr.get_pets(player.user_id)
        if not pets:
            yield event.plain_result("🐾 你当前没有携带任何灵兽。\n💡 发送 `/孵化灵兽 <名字>` 契约一只。")
            return

        owner_hp, owner_atk = await self._owner_stats(player)
        slots = self.pet_mgr.get_pet_slots(player)
        now = int(time.time())
        sections = []
        lines = [
            f"🐾 你的灵兽（{len(pets)}/{slots}）",
            "━━━━━━━━━━━━━━━",
        ]
        for index, pet in enumerate(pets, 1):
            pet = await self.pet_mgr.sync_pet_stats(pet, owner_hp, owner_atk, 0)
            pet = await self.pet_mgr.refresh_pet(pet)
            stats = self.pet_mgr.get_pet_stats(pet, owner_hp, owner_atk, 0)
            max_hp = stats["max_hp"]
            hp = int(pet.get("hp") or 0)
            hp_pct = int(hp / max_hp * 100) if max_hp else 0
            status = self._pet_status(pet, max_hp, now)
            is_main = int(pet.get("is_main") or 0) == 1
            main_tag = " [主战]" if is_main else ""
            pet_info = self.pet_mgr.get_pet_info_for_level(
                pet.get("pet_type", ""), pet.get("level", 1)
            )
            stage_name = pet_info.get("name") or pet.get("pet_type", "")
            stage_no = int(pet_info.get("stage", 0)) + 1
            next_lv = pet_info.get("next_evolution_level")
            evo_text = f"Lv.{next_lv} 进化" if next_lv else "已终极进化"
            species_tag = "" if stage_name in pet["pet_name"] else f" · {stage_name}"
            lines.append(f"{index}. 【{pet['pet_name']}】{main_tag}{species_tag}")
            lines.append(f"   Lv.{pet['level']} · {status} · {stage_name}（{evo_text}）")
            lines.append(f"   精力 {hp}/{max_hp} ({hp_pct}%) · 攻 {stats['atk']} · 防 {stats['defense']}")
            sections.append({
                "name": f"{index}. {pet['pet_name']}{main_tag}{species_tag}",
                "rows": [
                    ("品种", f"{stage_name}（第{stage_no}阶）"),
                    ("进化", evo_text),
                    ("等级", f"Lv.{pet['level']}"),
                    ("经验", f"{pet['exp']}/{pet['level'] * 500}"),
                    ("精力", f"{hp}/{max_hp} ({hp_pct}%)"),
                    ("状态", status),
                    ("攻击", str(stats["atk"])),
                    ("防御", str(stats["defense"])),
                ],
            })
        lines.append("━━━━━━━━━━━━━━━")
        if len(pets) > slots:
            lines.append(f"⚠️ 当前仅允许 {slots} 只灵兽出战，超出槽位的灵兽不会参战")
        elif len(pets) < slots:
            lines.append(f"🐾 空槽 {slots - len(pets)} 个 · 历练/秘境结算可触发灵兽奇遇")
        life_bonus = self.pet_mgr.get_life_bonus_from_pets(pets, "gold_bonus")
        if life_bonus > 0:
            lines.append(f"💰 生活加成叠加：灵石收益 +{life_bonus:.1%}（每只灵兽分别生效）")
        if getattr(player, "cultivation_type", "") == "御兽":
            lines.append("🐾 御兽：喂养-20% · 复活-30% · 修为共享30%（每只各得） · 兽群指挥")
        else:
            lines.append("🐾 非御兽仅 1 个灵兽槽 · 修为共享10%（每只各得） · 主战宠提供主人被动")
        lines.append("💡 /灵兽出战 切换主战 · /喂养灵兽 仅耗灵石 · 每回合协战")
        msg = "\n".join(lines)

        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_PET"):
                card_path = await render_card_async(
                    "pet",
                    f"灵兽 · {len(pets)}只",
                    f"槽位 {len(pets)}/{slots} · 主战灵兽提供主人被动",
                    sections=sections,
                    footer="/灵兽出战 切换主战 · Lv20/Lv40 自动进化 · 精力耗尽需时间复活",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(msg)
        except Exception:
            yield event.plain_result(msg)

    @player_required
    async def handle_adopt(self, player: Player, event: AstrMessageEvent, pet_name: str = ""):
        pets = await self.pet_mgr.get_pets(player.user_id)
        slots = self.pet_mgr.get_pet_slots(player)
        if not pet_name:
            available_lines = []
            for key, info in self.pet_mgr.BASE_PET_TYPES.items():
                chain = self.pet_mgr.PET_EVOLUTIONS.get(key, [])
                evolution = " → ".join(stage["name"] for stage in chain) if chain else key
                available_lines.append(f"· {key}：{info['desc']}\n    进化：{evolution}")
            available = "\n".join(available_lines)
            free_note = "（御兽职业首次契约免费）" if getattr(player, "cultivation_type", "") == "御兽" and not pets else ""
            yield event.plain_result(
                f"请指定你要契约的灵兽名称{free_note}：\n{available}\n"
                f"当前槽位：{len(pets)}/{slots}\n💡 发送 `/孵化灵兽 <名字>` 契约一只。"
            )
            return

        success, msg = await self.pet_mgr.adopt_pet(player, pet_name)
        yield event.plain_result(msg)

    @player_required
    async def handle_feed(self, player: Player, event: AstrMessageEvent, pet_selector: str = ""):
        cost_mult = 1.0
        try:
            async with self.db.conn.execute(
                "SELECT furniture FROM blessed_lands WHERE user_id = ?", (player.user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0] and "灵兽窝" in row[0]:
                cost_mult = 0.8
        except Exception:
            pass
        success, msg = await self.pet_mgr.feed_pet(player, pet_selector, cost_mult)
        if success and cost_mult < 1.0:
            msg += "\n🛋️ 灵兽窝生效：喂养消耗-20%"
        yield event.plain_result(msg)

    @player_required
    async def handle_set_main(self, player: Player, event: AstrMessageEvent, pet_selector: str = ""):
        pets = await self.pet_mgr.get_pets(player.user_id)
        if not pets:
            yield event.plain_result("❌ 你还没有灵兽。")
            return
        if not pet_selector:
            lines = ["请指定要出战的主战灵兽（序号或名字）："]
            for index, pet in enumerate(pets, 1):
                main_tag = " [当前主战]" if int(pet.get("is_main") or 0) == 1 else ""
                lines.append(f"{index}. {pet['pet_name']}{main_tag}")
            yield event.plain_result("\n".join(lines))
            return
        pet, error = self.pet_mgr._select_pet(pets, pet_selector)
        if not pet:
            yield event.plain_result(error)
            return
        success, msg = await self.pet_mgr.set_main_pet(player.user_id, pet["id"])
        yield event.plain_result(msg)

    @player_required
    async def handle_rename(self, player: Player, event: AstrMessageEvent, args: str = ""):
        args = (args or "").strip()
        if not args:
            yield event.plain_result("用法：/灵兽改名 <序号或名字> <新名字>\n💡 默认名字如「火灵狐 1」，建议直接用序号。")
            return
        pets = await self.pet_mgr.get_pets(player.user_id)
        if not pets:
            yield event.plain_result("❌ 你还没有灵兽。")
            return

        selector = ""
        new_name = ""
        for pet in sorted(pets, key=lambda p: len(p.get("pet_name", "")), reverse=True):
            name = pet.get("pet_name", "")
            if name and args.startswith(name) and len(args) > len(name) and args[len(name)].isspace():
                selector = name
                new_name = args[len(name):].strip()
                break
        if not new_name:
            parts = args.split(None, 1)
            if len(parts) < 2:
                yield event.plain_result("用法：/灵兽改名 <序号或名字> <新名字>")
                return
            selector, new_name = parts[0], parts[1].strip()

        pet, error = self.pet_mgr._select_pet(pets, selector)
        if not pet:
            yield event.plain_result(error)
            return
        success, msg = await self.pet_mgr.rename_pet(player.user_id, pet["id"], new_name)
        yield event.plain_result(msg)

    @player_required
    async def handle_release(self, player: Player, event: AstrMessageEvent, pet_selector: str = ""):
        success, msg = await self.pet_mgr.release_pet(player, pet_selector)
        yield event.plain_result(msg)

    @player_required
    async def handle_devour(self, player: Player, event: AstrMessageEvent, pet_selector: str = ""):
        success, msg = await self.pet_mgr.devour_pet(player, pet_selector)
        yield event.plain_result(msg)
