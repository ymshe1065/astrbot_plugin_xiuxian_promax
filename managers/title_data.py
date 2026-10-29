# managers/title_data.py
"""称号大全（100+，六品阶，含属性加成）—— v6.17
获取路径 key 约定（由 title_manager 校验颁发）：
  manual: 管理员/商店/师德等手动授予
  breakthrough: 突破至某境界自动
  win_pvp / spar_count / boss_kill / escort_count / karma / virtue / reincarnation /
  alchemy_perfect / hybrid / pet_level / furnance / sect_battle / rebirth / festival / demon_catch
加成 stats: atk/def 物理伤害、magic 法伤、phys_def/magic_def 防御、crit 会心率%、lifesteal 吸血%、exp 修为%、luck 气运
同一时间只生效最高品阶的一个称号加成；称号本身全部永久收藏在图鉴中。
"""


def T(name, tier, how, condition=None, **stats):
    return {"name": name, "tier": tier, "how": how, "condition": condition, "stats": stats}


# 品阶定义：tier -> (名称, 排序权重, 颜色)
TIERS = {
    1: ("凡品", 1),
    2: ("灵品", 2),
    3: ("地品", 3),
    4: ("天品", 4),
    5: ("圣品", 5),
    6: ("帝品", 6),
}

TITLES = [
    # ========== 凡品（入门/累积类，25）==========
    T("初入仙途", 1, "breakthrough", condition=1, exp=0.02),
    T("炼气小成", 1, "breakthrough", condition=4, exp=0.02),
    T("晨钟暮鼓", 1, "checkin_count", condition=7, exp=0.02),
    T("风雨无阻", 1, "checkin_count", condition=30, exp=0.03),
    T("初露锋芒", 1, "sect_battle_win", condition=1, atk=10),
    T("菜市场常客", 1, "buy_count", condition=20, luck=1),
    T("坊市赌怪", 1, "gamble_count", condition=10, luck=2),
    T("耕耘者", 1, "harvest_count", condition=10, exp=0.02),
    T("灵植新手", 1, "harvest_count", condition=30, exp=0.03),
    T("第一桶金", 1, "gold_reach", condition=100000, luck=1),
    T("小有积蓄", 1, "gold_reach", condition=1000000, luck=2),
    T("锻炼新手", 1, "forge_count", condition=5, atk=8),
    T("打磨匠人", 1, "forge_count", condition=20, atk=15),
    T("驯兽新人", 1, "pet_level", condition=10, atk=10),
    T("御兽熟手", 1, "pet_level", condition=25, atk=18),
    T("矿工之友", 1, "craft_count", condition=10, exp=0.02),
    T("丹炉学徒", 1, "craft_count", condition=30, exp=0.03),
    T("行走商贩", 1, "market_sell", condition=5, luck=2),
    T("信誉摊主", 1, "market_sell", condition=20, luck=3),
    T("护田使者", 1, "shield_count", condition=5, phys_def=10),
    T("巡逻队员", 1, "spar_count", condition=10, atk=10),
    T("历练常客", 1, "adventure_count", condition=10, exp=0.02),
    T("秘境游客", 1, "rift_count", condition=5, exp=0.03),
    T("勤修不辍", 1, "cultivate_count", condition=10, exp=0.02),
    T("仙途行者", 1, "cultivate_count", condition=50, exp=0.03),

    # ========== 灵品（进阶玩法，25）==========
    T("百战老兵", 2, "spar_count", condition=50, atk=40),
    T("决斗之王", 2, "win_pvp", condition=10, atk=50),
    T("不胜不败", 2, "win_pvp", condition=30, atk=70, crit=2),
    T("斗战胜佛", 2, "win_pvp", condition=60, atk=90, crit=3),
    T("破境者", 2, "breakthrough", condition=9, exp=0.05),
    T("金丹在望", 2, "breakthrough", condition=13, exp=0.05),
    T("丹道新星", 2, "craft_perfect", condition=5, exp=0.04),
    T("极品丹师", 2, "craft_perfect", condition=15, exp=0.06),
    T("炼器学徒", 2, "enhance_count", condition=10, atk=30),
    T("强化狂人", 2, "enhance_count", condition=30, atk=50),
    T("洗练专家", 2, "wash_count", condition=15, luck=3),
    T("赌石行家", 2, "gamble_count", condition=50, luck=4),
    T("镖行新秀", 2, "escort_count", condition=5, gold_pct=0.05),
    T("金牌镖师", 2, "escort_count", condition=20, gold_pct=0.08),
    T("镖路无双", 2, "escort_count", condition=50, gold_pct=0.12, atk=30),
    T("洞天福主", 2, "land_level", condition=5, exp=0.04),
    T("灵田大户", 2, "harvest_count", condition=80, exp=0.05),
    T("黑市常客", 2, "blackmarket_buy", condition=10, luck=3),
    T("坊市红人", 2, "market_sell", condition=50, luck=5),
    T("灵兽伙伴", 2, "pet_level", condition=40, atk=45),
    T("驯兽大师", 2, "pet_level", condition=50, atk=60, crit=2),
    T("降魔先锋", 2, "boss_damage", condition=10, atk=45),
    T("Boss猎手", 2, "boss_kill_old", condition=3, atk=55),
    T("天魔猎人", 2, "demon_catch", condition=1, crit=3),
    T("照妖明镜", 2, "demon_catch", condition=3, crit=5),

    # ========== 地品（高难玩法，22）==========
    T("筑基人杰", 3, "breakthrough", condition=10, exp=0.08, atk=60),
    T("金丹真人", 3, "breakthrough", condition=13, exp=0.10, atk=90),
    T("元婴老祖", 3, "breakthrough", condition=16, exp=0.12, atk=120),
    T("传道真人", 3, "manual", atk=60, exp=0.05),
    T("天下第一善", 3, "karma", condition=20, luck=8, exp=0.06),
    T("积善之家", 3, "karma", condition=10, luck=5),
    T("魔道新秀", 3, "karma_neg", condition=-10, atk=60, lifesteal=3),
    T("魔道巨擘", 3, "karma_neg", condition=-25, atk=100, lifesteal=5),
    T("炼丹宗师", 3, "craft_perfect", condition=40, exp=0.10),
    T("丹圣", 3, "craft_perfect", condition=80, exp=0.15, luck=5),
    T("五品炉神", 3, "furnace_max", condition=5, exp=0.10, atk=60),  # ★ 修复：添加了 condition=5，解决死称号问题
    T("大派栋梁", 3, "sect_battle_win", condition=10, atk=70),
    T("宗门柱石", 3, "sect_battle_win", condition=25, atk=100, phys_def=50),
    T("一代宗师", 3, "sect_battle_win", condition=50, atk=140, crit=4),
    T("飞升者", 3, "reincarnation", condition=1, exp=0.08, luck=5),
    T("轮回老祖", 3, "reincarnation", condition=3, exp=0.12, luck=8),
    T("万世轮回", 3, "reincarnation", condition=6, exp=0.18, luck=12),
    T("寻宝奇人", 3, "luck_event", condition=10, luck=8),
    T("机缘深厚", 3, "luck_event", condition=25, luck=12, exp=0.06),
    T("天命之子", 3, "luck_event", condition=50, luck=18, exp=0.10),
    T("镇魔都尉", 3, "demon_catch", condition=8, atk=90, crit=5),
    T("护道者", 3, "escort_count", condition=100, gold_pct=0.15, phys_def=80),

    # ========== 天品（巅峰成就，18）==========
    T("化神尊者", 4, "breakthrough", condition=19, exp=0.15, atk=180),
    T("炼虚合道", 4, "breakthrough", condition=22, exp=0.18, atk=220),
    T("大乘巅峰", 4, "breakthrough", condition=28, exp=0.20, atk=260),
    T("渡劫真君", 4, "breakthrough", condition=31, exp=0.25, atk=300),
    T("灭世魔神", 4, "manual", atk=200, crit=5),
    T("初露锋芒·极", 4, "sect_battle_win", condition=80, atk=200, crit=6),
    T("天下无双", 4, "win_pvp", condition=120, atk=260, crit=8),
    T("横扫八荒", 4, "win_pvp", condition=200, atk=320, crit=10),
    T("屠龙英雄", 4, "boss_kill_old", condition=15, atk=240, lifesteal=4),
    T("魔潮砥柱", 4, "tide_resist", condition=3, exp=0.15, phys_def=150),
    T("中流砥柱", 4, "tide_resist", condition=8, exp=0.20, phys_def=200, magic_def=150),
    T("灵兽至尊", 4, "pet_level", condition=50, atk=200, crit=6),
    T("极品丹神", 4, "craft_perfect", condition=150, exp=0.22, luck=10),
    T("赌圣", 4, "gamble_count", condition=150, luck=15),
    T("洞天主宰", 4, "land_level", condition=5, exp=0.15, magic_def=120),
    T("百变星君", 4, "rebirth", condition=3, exp=0.15, luck=10),
    T("轮回至尊", 4, "reincarnation", condition=10, exp=0.25, luck=18),
    T("功德无量", 4, "karma", condition=50, luck=20, exp=0.20),

    # ========== 圣品（传奇，12）==========
    T("仙界传说", 5, "manual", atk=400, crit=10, exp=0.30),
    T("万古一帝", 5, "reincarnation", condition=20, exp=0.35, luck=25, atk=350),
    T("天道亲女儿", 5, "luck_event", condition=100, luck=30, exp=0.30),
    T("炼丹圣人", 5, "craft_perfect", condition=300, exp=0.35, luck=20),
    T("武道圣者", 5, "win_pvp", condition=400, atk=500, crit=15),
    T("圣域主宰", 5, "breakthrough", condition=33, exp=0.35, atk=450),
    T("功德圣者", 5, "karma", condition=100, luck=35, exp=0.35),
    T("魔帝", 5, "karma_neg", condition=-60, atk=450, lifesteal=10),
    T("天下第一镖", 5, "escort_count", condition=250, gold_pct=0.25, atk=300),
    T("百炼成圣", 5, "enhance_count", condition=100, atk=380, phys_def=300),
    T("驯兽神王", 5, "pet_level", condition=50, atk=380, crit=12),
    T("镇魔大元帅", 5, "demon_catch", condition=20, atk=380, crit=12),

    # ========== 帝品（唯一性/隐藏，8）==========
    T("灭世魔神·帝", 6, "manual", atk=600, crit=15, lifesteal=8),
    T("天下第一", 6, "manual", atk=550, crit=12, exp=0.30),
    T("天道化身", 6, "tide_resist", condition=20, exp=0.45, luck=40, phys_def=400),
    T("气运之子", 6, "luck_event", condition=200, luck=50, exp=0.40),
    T("丹帝", 6, "craft_perfect", condition=600, exp=0.50, luck=30),
    T("万胜武帝", 6, "win_pvp", condition=800, atk=700, crit=20),
    T("轮回帝尊", 6, "reincarnation", condition=35, exp=0.55, luck=45, atk=500),
    T("仙帝", 6, "breakthrough", condition=35, exp=0.60, atk=650, crit=15),
]