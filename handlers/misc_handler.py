# handlers/misc_handler.py

from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..models import Player
from ..models_extended import UserStatus
from .utils import player_required, with_player_lock

__all__ = ["MiscHandler"]

class MiscHandler:
    """杂项指令处理器 - 提供帮助信息、一键日常与强制解卡"""

    def __init__(self, db: DataBase):
        self.db = db

    async def handle_help(self, event: AstrMessageEvent):
        """显示 v6.15 修订版的修仙指令大全"""
        help_text = (
            "📖 修仙指令大全 v6.18 (万象更新版 + 宗门拓展)\n"
            "💡 提示：所有指令均可带 / 前缀使用\n"
            "━━━━━━━━━━━━━━━\n"
            "【🌟 入门与基础】\n"
            "  /我要修仙 [职业] (灵/体/魔/剑/鬼/佛/御兽)\n"
            "  /我的信息 (卡片) /我的装备 (卡片) /修仙帮助\n"
            "  /签到 (节日大奖+卡片) /一键日常 /解卡\n"
            "  /改道号 /弃道重修 确认 (需空闲/无贷款) /行善 (积功德)\n"
            "  /选择生活职业 <炼器/符箓/阵法> /制符\n"
            "  /仙灵结缘 [名] (NPC道侣) /仙灵共修 /仙灵代收 /仙灵解除\n"
            "  /拜师 @人 /师徒 /出师 @人 /逐出师门 @人 /师德兑换 <编号>\n"
            "  /称号图鉴 /佩戴称号 <名> /我的称号 (110称号六品阶)\n"
            "\n"
            "【🧘 修炼与突破】\n"
            "  /闭关 /出关 (境界越高收益越高;受家具/宗门/魔潮/化身影响)\n"
            "  /突破信息 (含资费与功德修正) /突破 [破境丹]\n"
            "  /升级丹炉 /激活化身 (30天闭关+20%)\n"
            "\n"
            "【💊 丹药与炼丹】\n"
            "  /丹药背包 /丹药信息 <名> /服用丹药 <名> [数量]\n"
            "  /丹药配方 /炼丹 <ID> (投料后 /火候 <1-3> 控火)\n"
            "\n"
            "【⚔️ 战斗与竞技】(功法/武器带五行克制)\n"
            "  /切磋 @人 /决斗 @人 (均有冷却)\n"
            "  /挑战世界Boss (5分钟CD,单次0.5%-20%,每日不限次)\n"
            "  /世界Boss状态 (血量/规则/剩余时间)\n"
            "  /传承挑战 @人 /传承信息 /传承排行\n"
            "  /挑战Boss /世界boss (旧版)\n"
            "\n"
            "【🏪 经济与交易】\n"
            "  /银行 /存灵石 /取灵石 /领取利息\n"
            "  /贷款 /突破贷款 (大额需持有20%,最低5000)\n"
            "  /还款 [金额] (先息后本;不填全额) /银行流水\n"
            "  /坊市 (卡片) /坊市上架 <物> <数> <价>\n"
            "  /坊市购买 <ID> /坊市下架 <ID>\n"
            "  /切原石 /黑市 (每小时刷新) /黑市购买 <编号> /强化 /洗练\n"
            "\n"
            "【🛍️ 商店与资产】\n"
            "  /丹阁 /器阁 /百宝阁 (卡片货架，自动补货)\n"
            "  /购买 <物> [数] 或 购买物品<物> [数]\n"
            "  /物品信息 <名>\n"
            "  /储物戒 (卡片) /取出 <物> [数] /丢弃 <物> [数]\n"
            "  /搜索物品 <词> /取出所有 <分类> /赠予 @人 <物> [数]\n"
            "  /接收 /拒绝 /卸下 <武器/防具/心法/功法名>\n"
            "  /更换储物戒 <名> /储物戒附魔 [数]\n"
            "\n"
            "【🏔️ 洞天·灵田·灵眼】\n"
            "  /我的洞天 /购买洞天 1 /升级洞天 /洞天收取\n"
            "  /洞天装修 <家具> /进阶洞天 <编号>\n"
            "  /我的灵田 (卡片) /开垦灵田 /种植 <草> [数]\n"
            "  /收获 /施肥 /杂交 <草A> <草B> /升级灵田 /季节\n"
            "  /灵眼信息 /抢占灵眼 <ID> /灵眼收取 /释放灵眼\n"
            "  /我的灵兽 (卡片) /孵化灵兽 <名> /喂养灵兽\n"
            "  /放生灵兽 /吞噬灵兽\n"
            "\n"
            "【📜 任务与冒险】\n"
            "  /任务榜 (今日随机任务+进度) /接取任务 <黄/玄/地/天>\n"
            "  /放弃任务 <编号> /领取任务奖励\n"
            "  /悬赏令 /接取悬赏 <ID> /悬赏状态 /完成悬赏 /放弃悬赏\n"
            "  /历练信息 /开始历练 <路线> /历练状态 /完成历练 /放弃历练\n"
            "  /秘境列表 /探索秘境 <ID> /完成探索 /退出秘境\n"
            "  /奇遇选择 <编号> (110种事件三选一)\n"
            "\n"
            "【🛡️ 押镖与劫道】\n"
            "  /押镖 (高额镖银+功德,30分钟)\n"
            "  /完成押镖 /放弃押镖 /劫镖 @人 (10分钟CD)\n"
            "\n"
            "【🏛️ 宗门】\n"
            "  /创建宗门 /加入宗门 /退出宗门 /解散宗门 确认 /我的宗门 /宗门列表\n"
            "  /宗门捐献 <数> /宗门任务 /完成宗门任务 /放弃宗门任务\n"
            "  /宗门建筑 /升级建筑 <名称>\n"
            "  /宗门商店 (贡献随机5件,1小时刷新) /宗门兑换 <编号>\n"
            "  /宗门命令 @人 捐献 1000 24 /宗门命令列表 /宗门命令撤销 <编号>\n"
            "  /宗门大比开启 /宗门大比报名 /宗门大比状态\n"
            "  /宗门宣战 <宗门名> /宗门战争状态\n"
            "  /踢出成员 @人 /宗主传位 @人 /职位变更 @人 <0-4>\n"
            "  /双修 @人 /接受双修 /拒绝双修\n"
            "  /开启护府 /潜入洞府 @人\n"
            "\n"
            "【🌑 天道天灾与活动】\n"
            "  /仙界日历 (季节+事件预告)\n"
            "  /魔潮状态 /抵御魔潮 <灵石> /天魔状态 /查验天魔 @人\n"
            "\n"
            "【📊 排行榜】\n"
            "  /境界排行 /战力排行 /灵石排行 /存款排行\n"
            "  /宗门排行 /贡献排行 /功德榜 /气运榜 /师德榜\n"
            "\n"
            "【✨ 飞升转生】\n"
            "  /飞升 [封印装备名] /解封传家宝\n"
            "  /仙缘商店 /仙缘兑换 <编号>\n"
            "\n"
            "━━━━━━━━━━━━━━━\n"
            "🛠️【管理员】(需配置BOSS_ADMINS)\n"
            "  /仙界管理 (查玩家/发灵石/重置状态/公告)\n"
            "  /生成世界Boss /生成Boss /开启魔潮\n"
            "  /开启天魔入侵（旧版宗门大比已由宗主开启）"
        )
        yield event.plain_result(help_text)

    @player_required
    async def handle_daily_helper(self, player: Player, event: AstrMessageEvent):
        """一键日常：智能引导玩家结算挂机收益"""
        # 这里为了保持底层逻辑的独立性和稳定性，采用清单指引模式。
        msg = (
            f"📦 【{player.user_name or '道友'}】的每日修行清单\n"
            "━━━━━━━━━━━━━━━\n"
            "由于各系统的结算时间略有不同，请依次发送以下指令获取收益：\n\n"
            "1️⃣ 发送 `/签到` (获取灵石/节日福利)\n"
            "2️⃣ 发送 `/洞天收取` (获取洞天产出的修为和灵石)\n"
            "3️⃣ 发送 `/收获` (获取灵田作物)\n"
            "4️⃣ 发送 `/领取利息` (取出银行利息)\n"
            "5️⃣ 发送 `/灵眼收取` (收取灵眼修为)\n"
            "━━━━━━━━━━━━━━━\n"
            "💡 每天花 5 秒点完，道友早日飞升！"
        )
        yield event.plain_result(msg)

    @with_player_lock
    async def handle_force_unlock(self, event: AstrMessageEvent):
        """强制解卡（不走忙碌状态拦截，保证任何时候都能自救）"""
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("道友尚未踏入仙途，请发送「我要修仙」开启你的旅程。")
            return
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        
        if (not user_cd or user_cd.type == UserStatus.IDLE) and player.state == "空闲":
            yield event.plain_result("✅ 你的状态非常健康，无需解卡。")
            return
            
        # ===== 核心修复：强制清空所有可能残留的状态和进度 =====
        
        # 1. 强制重置数据库中的 CD 表，并且显式清空 extra_data
        await self.db.ext.set_user_busy(player.user_id, UserStatus.IDLE, 0, extra_data={})
        
        # 2. 强制重置玩家模型中的数据
        player.state = "空闲"
        player.cultivation_start_time = 0
        await self.db.update_player(player)
        
        # 3. 暴力清理：取消正在进行中的赠予
        try:
            await self.db.conn.execute("DELETE FROM pending_gifts WHERE sender_id = ? OR receiver_id = ?", (player.user_id, player.user_id))
            await self.db.conn.commit()
        except:
            pass

        yield event.plain_result(
            "🔓 状态已强制重置！\n"
            "━━━━━━━━━━━━━━━\n"
            "无论你之前是在闭关、历练还是探索秘境，现在都已恢复【空闲】。\n"
            "⚠️ 警告：解卡视为**强制放弃**当前所有进度，绝不会发放任何奖励！\n"
            "注：强制解卡不会返还已损失的门票或道具。"
        )