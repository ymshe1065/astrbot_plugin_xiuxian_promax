"""季节轮转周期、季节动态成熟时间与装备五行标注回归。"""
import datetime
import time
import unittest
from unittest.mock import patch

import test_equipment_instances as fixtures
from test_equipment_instances import ROOT, _imp

YEAR_START = datetime.datetime(2026, 1, 1, 0, 0, 0)
EPOCH = YEAR_START.timestamp()


def _ts(mon, day, hour=0, minute=0):
    """2026 年某月某日的时间戳。"""
    return int((datetime.datetime(2026, mon, day, hour, minute) - YEAR_START).total_seconds() + EPOCH)


def _fake_localtime(value=None):
    """避免依赖真实日期：把任意时间戳映射到 2026 年同位置。"""
    moment = time.time() if value is None else value
    return (YEAR_START + datetime.timedelta(seconds=float(moment) - EPOCH)).timetuple()


def _local(year, mon, day):
    yday = datetime.date(year, mon, day).timetuple().tm_yday
    wday = datetime.date(year, mon, day).weekday()
    return time.struct_time((year, mon, day, 12, 0, 0, wday, yday, -1))


class TestSeasonPeriod(unittest.TestCase):
    def setUp(self):
        self.sf = _imp("managers.spirit_farm_manager")
        self.sf.configure_season_period(2)

    def tearDown(self):
        self.sf.configure_season_period(2)

    def _season_at(self, monthday, period=2):
        self.sf.configure_season_period(period)
        with patch.object(self.sf.time, "localtime", return_value=_local(2026, *monthday)):
            return self.sf.get_current_season_index(), self.sf.get_season_days_left()

    def test_default_period_is_two_days(self):
        self.assertEqual(self.sf.SEASON_PERIOD_DAYS, 2)

    def test_period_two_days_matches_calendar(self):
        # 1/1-1/2 春，1/3-1/4 夏，1/5-1/6 秋，1/7-1/8 冬，1/9 回到春
        cases = {
            (1, 1): 0, (1, 2): 0,
            (1, 3): 1, (1, 4): 1,
            (1, 5): 2, (1, 6): 2,
            (1, 7): 3, (1, 8): 3,
            (1, 9): 0,
        }
        for (mon, day), expected in cases.items():
            index, _ = self._season_at((mon, day))
            self.assertEqual(index, expected, f"{mon}/{day}")

    def test_days_left_counts_today(self):
        self.assertEqual(self._season_at((1, 1))[1], 2)   # 换季当天还剩 2 天（含今天）
        self.assertEqual(self._season_at((1, 2))[1], 1)   # 今晚换季

    def test_period_is_configurable(self):
        index, left = self._season_at((1, 5), period=7)
        self.assertEqual((index, left), (0, 3))
        index, left = self._season_at((1, 7), period=7)
        self.assertEqual((index, left), (0, 1))
        index, left = self._season_at((1, 8), period=7)
        self.assertEqual((index, left), (1, 7))

    def test_configure_clamps_and_ignores_invalid(self):
        self.assertEqual(self.sf.configure_season_period(0), 1)
        self.assertEqual(self.sf.configure_season_period(999), 30)
        self.assertEqual(self.sf.configure_season_period("x"), 30)

    def test_progress_payload_has_display_fields(self):
        self.sf.configure_season_period(2)
        with patch.object(self.sf.time, "localtime", return_value=_local(2026, 1, 2)):
            progress = self.sf.get_season_progress()
        self.assertEqual(progress["index"], 0)
        self.assertEqual(progress["period_days"], 2)
        self.assertEqual(progress["cycle_text"], "每 2 天轮转")
        self.assertEqual(progress["change_text"], "今晚换季")

    def test_calendar_helpers_follow_period(self):
        cal = _imp("managers.calamity_manager")
        self.sf.configure_season_period(5)
        with patch.object(self.sf.time, "localtime", return_value=_local(2026, 1, 2)):
            self.assertEqual(cal.get_season_cycle(), (5, 4))
            self.assertEqual(cal.get_season_index_pub(), 0)


class TestSeasonRestartSafety(unittest.TestCase):
    """季节是「日期函数」而非「运行状态」：重启多少次都不该改变季节。"""

    def setUp(self):
        self.sf = _imp("managers.spirit_farm_manager")
        self.sf.configure_season_period(2)

    def tearDown(self):
        self.sf.configure_season_period(2)

    def test_repeated_reads_within_a_day_are_stable(self):
        day = _ts(3, 10, 9)
        with patch.object(self.sf.time, "time", return_value=day), \
                patch.object(self.sf.time, "localtime", _fake_localtime):
            seen = {self.sf.get_current_season_index() for _ in range(5)}
        self.assertEqual(len(seen), 1)

    def test_season_depends_only_on_calendar_date(self):
        with patch.object(self.sf.time, "localtime", _fake_localtime):
            morning = self.sf.season_index_at(_ts(3, 10, 0))
            evening = self.sf.season_index_at(_ts(3, 10, 23))
            last_day = self.sf.season_index_at(_ts(3, 11, 23))
            after_change = self.sf.season_index_at(_ts(3, 12, 0))
        self.assertEqual(morning, evening)      # 同一天内不变
        self.assertEqual(last_day, morning)     # 季节最后一天仍是同一季
        self.assertEqual(after_change, (morning + 1) % 4)

    def test_next_change_points_to_midnight(self):
        with patch.object(self.sf.time, "time", return_value=_ts(1, 2, 13)), \
                patch.object(self.sf.time, "localtime", _fake_localtime):
            ts = self.sf.get_next_season_change_ts()
            text = self.sf.get_next_season_change_text()
            left = self.sf.get_season_days_left()
        self.assertEqual(left, 1)
        self.assertEqual(ts, _ts(1, 3, 0))
        self.assertEqual(text, "01-03 00:00")

    def test_invalid_config_keeps_previous_period(self):
        self.sf.configure_season_period(2)
        self.assertEqual(self.sf.configure_season_period(None), 2)
        self.assertEqual(self.sf.configure_season_period("坏值"), 2)


class TestSeasonMatureTime(unittest.TestCase):
    """成熟时间随季节动态伸缩：作物只存基础时长，跨季按速率积分。"""

    def setUp(self):
        self.sf = _imp("managers.spirit_farm_manager")
        self.sf.configure_season_period(2)
        patcher = patch.object(self.sf.time, "localtime", _fake_localtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.sf.configure_season_period(2)

    def _mature(self, herb, plant_ts, base):
        return self.sf.compute_crop_mature_time(herb, plant_ts, base)

    def test_spring_speeds_up_plain_herb(self):
        # 幽冥草基础 48 小时，春季 ×0.75 → 36 小时（整段仍在春季内）
        self.assertEqual(self._mature("幽冥草", _ts(1, 1), 172800), _ts(1, 2, 12))

    def test_crossing_to_summer_extends_proportionally(self):
        # 1/2 00:00 种下：春季只剩 24 小时（推进 32 小时基础），剩 16 小时基础由夏季按 1.0 走完
        self.assertEqual(self._mature("幽冥草", _ts(1, 2), 172800), _ts(1, 3, 16))

    def test_winter_slows_everything_but_cold_herb(self):
        self.assertEqual(self._mature("幽冥草", _ts(1, 7), 172800), _ts(1, 9, 12))
        self.assertEqual(self._mature("血灵草", _ts(1, 7), 7200), _ts(1, 7, 3))
        self.assertEqual(self._mature("冰心草", _ts(1, 7), 14400), _ts(1, 7, 4))

    def test_result_is_stable_regardless_of_query_moment(self):
        # 同一作物在不同时刻查询，结果一致（不随查询漂移，只有换季才变）
        first = self._mature("九叶灵芝", _ts(1, 2), 86400)
        self.assertEqual(first, _ts(1, 2, 18))   # 春季 ×0.75 → 24 小时变 18 小时
        with patch.object(self.sf.time, "time", return_value=_ts(1, 3, 10)):
            self.assertEqual(self._mature("九叶灵芝", _ts(1, 2), 86400), first)
            self.assertEqual(self.sf.get_current_season_index(), 1)

    def test_sync_recomputes_stale_mature_time(self):
        # 旧逻辑按种植瞬间倍率算成 1/4 00:00，同步后应变成 1/3 16:00（提前）
        crop = {"name": "幽冥草", "slot": 1, "plant_time": _ts(1, 2),
                "grow_base": 172800, "mature_time": _ts(1, 4)}
        self.assertTrue(self.sf.sync_crop_season_progress([crop]))
        self.assertEqual(crop["mature_time"], _ts(1, 3, 16))

    def test_sync_backfills_legacy_crop_without_changing_time(self):
        legacy = {"name": "九叶灵芝", "slot": 1, "plant_time": _ts(1, 1),
                  "mature_time": _ts(1, 2)}
        self.assertTrue(self.sf.sync_crop_season_progress([legacy]))
        self.assertIn("grow_base", legacy)
        self.assertEqual(legacy["mature_time"], _ts(1, 2))
        # 第二轮同步保持稳定，不再反复改
        self.assertFalse(self.sf.sync_crop_season_progress([legacy]))
        self.assertEqual(legacy["mature_time"], _ts(1, 2))

    def test_sync_handles_hybrid_jobs(self):
        hybrid = {"name": self.sf.HYBRID_MARKER, "job_id": "j1", "slot": 1,
                  "product": "幽冥草", "plant_time": _ts(1, 2),
                  "grow_base": 172800, "mature_time": _ts(1, 4)}
        self.assertTrue(self.sf.sync_crop_season_progress([hybrid]))
        self.assertEqual(hybrid["mature_time"], _ts(1, 3, 16))

    def test_mature_crop_never_regresses(self):
        # 已经成熟的作物，换季不会把它变回未成熟
        crop = {"name": "幽冥草", "slot": 1, "plant_time": _ts(1, 1),
                "grow_base": 172800, "mature_time": _ts(1, 2, 12)}
        with patch.object(self.sf.time, "time", return_value=_ts(1, 3)):
            self.sf.sync_crop_season_progress([crop])
        self.assertLessEqual(crop["mature_time"], _ts(1, 2, 12))


class TestHerbBalance(unittest.TestCase):
    """种植节奏设计约束：最长 12 小时（短于一个季节），单位时间产出有下限。"""

    def setUp(self):
        self.sf = _imp("managers.spirit_farm_manager")

    def test_no_herb_exceeds_half_a_season(self):
        limit = max(12 * 3600, self.sf.SEASON_PERIOD_DAYS * 86400 // 4)
        for name, cfg in self.sf.SPIRIT_HERBS.items():
            self.assertLessEqual(cfg["grow_time"], limit, name)

    def test_per_slot_day_output_stays_in_band(self):
        for name, cfg in self.sf.SPIRIT_HERBS.items():
            per_day = cfg["exp_yield"] * 86400 / cfg["grow_time"]
            self.assertGreaterEqual(per_day, 12000, name)
            self.assertLessEqual(per_day, 80000, name)


class TestEquipmentElementTag(unittest.TestCase):
    def setUp(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        handler_mod = _imp("handlers.equipment_handler")
        self.handler = handler_mod.EquipmentHandler.__new__(handler_mod.EquipmentHandler)
        self.handler.config_manager = cm

    def test_known_element_renders_parentheses(self):
        cm = self.handler.config_manager
        name, element = next(
            (n, e) for n, e in cm._element_index.items() if e
        )
        self.assertEqual(self.handler._element_tag(name), f"（{element}）")

    def test_unknown_or_blank_name_returns_empty(self):
        self.assertEqual(self.handler._element_tag("不存在的装备"), "")
        self.assertEqual(self.handler._element_tag(""), "")


if __name__ == "__main__":
    unittest.main()
