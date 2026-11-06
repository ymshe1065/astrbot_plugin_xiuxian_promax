# -*- coding: utf-8 -*-
"""webroom 消息呈现：个人信息指令识别。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

visibility = __import__(f"{PKG}.webchat_room.visibility", fromlist=["x"])


class PersonalCommandTest(unittest.TestCase):
    def test_named_personal_queries(self):
        for text in ("丹药配方", "/丹药配方", "符箓配方", "/符箓配方",
                     "我的坊市", "/我的坊市", "坊市我的", "/坊市我的"):
            self.assertTrue(visibility.is_personal_command(text), text)

    def test_my_prefix_and_others(self):
        for text in ("/我的装备", "/我的宗门", "/丹方", "/符箓书",
                     "/银行流水", "/仙市流水", "/历练状态", "/代理状态"):
            self.assertTrue(visibility.is_personal_command(text), text)

    def test_public_or_action_not_personal(self):
        for text in ("/签到", "/坊市", "/秘境列表", "/境界排行",
                     "/丹阁", "/拍卖会", "/世界Boss状态", "/修仙帮助",
                     "晚上好", ""):
            self.assertFalse(visibility.is_personal_command(text), text)

    def test_argument_message_only_uses_command(self):
        self.assertTrue(visibility.is_personal_command("/丹方 聚气丹"))
        self.assertFalse(visibility.is_personal_command("/购买 聚气丹 1"))
        self.assertEqual(visibility.command_name("@机器人 /我的坊市"), "我的坊市")


if __name__ == "__main__":
    unittest.main()
