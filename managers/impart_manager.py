# managers/impart_manager.py
"""
传承系统管理器 - 处理功法传承、切磋增加传承等逻辑
参照NoneBot2插件的xn_xiuxian_impart实现
"""

from typing import Tuple, Dict, Optional
from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import ImpartInfo

class ImpartManager:
    """传承系统管理器"""
    
    def __init__(self, db: DataBase):
        self.db = db
    
    async def get_impart_info(self, user_id: str) -> Tuple[bool, str, Optional[ImpartInfo]]:
        """获取传承信息"""
        impart_info = await self.db.ext.get_impart_info(user_id)
        if not impart_info:
            await self.db.ext.ensure_impart_info(user_id)
            impart_info = await self.db.ext.get_impart_info(user_id)
        if not impart_info:
            return False, "❌ 传承系统初始化失败，请稍后再试！", None
        
        msg = f"""
✨ 传承信息
━━━━━━━━━━━━━━━

HP加成：{impart_info.impart_hp_per * 100:.1f}%
MP加成：{impart_info.impart_mp_per * 100:.1f}%
攻击加成：{impart_info.impart_atk_per * 100:.1f}%
会心加成：{impart_info.impart_know_per * 100:.1f}%
爆伤加成：{impart_info.impart_burst_per * 100:.1f}%

混合经验：{impart_info.impart_mix_exp}
        """.strip()
        
        return True, msg, impart_info

    async def update_impart(self, user_id: str, type_name: str, value: float) -> Tuple[bool, str]:
        """
        更新传承属性（通常由物品使用或事件触发）
        type_name: hp/mp/atk/know/burst
        """
        impart_info = await self.db.ext.get_impart_info(user_id)
        if not impart_info:
            await self.db.ext.create_impart_info(user_id)
            impart_info = await self.db.ext.get_impart_info(user_id)
            
        if type_name == "hp":
            impart_info.impart_hp_per += value
        elif type_name == "mp":
            impart_info.impart_mp_per += value
        elif type_name == "atk":
            impart_info.impart_atk_per += value
        elif type_name == "know":
            impart_info.impart_know_per += value
        elif type_name == "burst":
            impart_info.impart_burst_per += value
            
        await self.db.ext.update_impart_info(impart_info)
        return True, f"✨ 你的{type_name}传承属性增加了 {value*100:.1f}%！"
