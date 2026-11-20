# -*- coding: utf-8 -*-
"""AI 向导（/chat · /chatall · /新手指南）回归测试。

覆盖四件事：
1. 分层检索：分块、打分排序、物品名精确/模糊命中、文档版本告警；
2. 上下文双缓冲：个人与频道隔离、私密回复不进公屏缓冲、TTL、清空、落库；
3. 问答流程：限流、Provider 缺失/超时/异常降级、深度检索触发与 mini agent 降级；
4. 输出安全：Markdown 清洗、按句截断、拆条、玩家快照不含灵石。

全部离线跑，不打真实模型。
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

from importlib import import_module  # noqa: E402

knowledge = import_module(f"{PKG}.utils.knowledge_retriever")
newbie_guide = import_module(f"{PKG}.utils.newbie_guide")
ctx_mod = import_module(f"{PKG}.managers.channel_context_manager")
chat_mod = import_module(f"{PKG}.managers.ai_chat_manager")
handler_mod = import_module(f"{PKG}.handlers.ai_chat_handler")


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _AsyncRunner(unittest.TestCase):
    """每个用例一个干净的事件循环，避免跨用例串状态。"""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)


# ============================ 测试替身 ============================


class _FakeExt:
    """只实现 system_config 读写，够上下文持久化用。"""

    def __init__(self):
        self.store = {}

    async def get_system_config(self, key):
        return self.store.get(key)

    async def set_system_config(self, key, value):
        self.store[key] = value

    async def list_system_configs(self, prefix=""):
        return [(k, v) for k, v in self.store.items() if k.startswith(prefix)]


class _FakeDB:
    def __init__(self, players=None):
        self.ext = _FakeExt()
        self.players = players or {}

    async def get_player_by_id(self, uid):
        return self.players.get(str(uid))


class _Player:
    def __init__(self, **kw):
        self.user_id = kw.get("user_id", "u1")
        self.user_name = kw.get("user_name", "云无月")
        self.level_index = kw.get("level_index", 3)
        self.cultivation_type = kw.get("cultivation_type", "剑修")
        self.spiritual_root = kw.get("spiritual_root", "三灵根")
        self.state = kw.get("state", "空闲")
        self.experience = kw.get("experience", 1200)
        self.gold = kw.get("gold", 888888)
        self.karma = kw.get("karma", 5)
        self.tutorial_step = kw.get("tutorial_step", 4)
        self.created_at = kw.get("created_at", 0)

    def get_level(self, cm):
        return "炼气期四层"

    def get_required_exp(self, cm):
        return 2400


class _FakeConfigManager:
    level_data = [{"level_name": f"境界{i}"} for i in range(36)]


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _FakeContext:
    """假 LLM：按 system_prompt 区分「关键词扩展」与「正式回答」。"""

    AGENT_SYSTEM = "只输出关键词，每行一个。"

    def __init__(self, provider="fake-provider", answer="答案", expand=None,
                 agent_error=None, answer_error=None, accept_kwargs=True):
        self.provider = provider
        self.answer = answer
        self.expand = expand if expand is not None else ["破境丹", "突破失败", "境界跌落"]
        self.agent_error = agent_error
        self.answer_error = answer_error
        self.accept_kwargs = accept_kwargs
        self.calls = []

    async def get_current_chat_provider_id(self, umo):
        if not self.provider:
            raise RuntimeError("Provider not found")
        return self.provider

    async def llm_generate(self, *, chat_provider_id, prompt=None, system_prompt=None, **kwargs):
        is_agent = system_prompt == self.AGENT_SYSTEM
        self.calls.append({"agent": is_agent, "prompt": prompt, "kwargs": dict(kwargs)})
        if is_agent:
            if not self.accept_kwargs and kwargs:
                raise TypeError("unexpected keyword argument")
            if self.agent_error:
                raise self.agent_error
            return _Resp("\n".join(self.expand))
        if self.answer_error:
            raise self.answer_error
        return _Resp(self.answer)


class _FakeEvent:
    def __init__(self, text="", uid="u1", name="云无月", umo="aiocqhttp:GroupMessage:1001",
                 channel="", private=False, panel=False, audience="public"):
        self._text = text
        self._uid = uid
        self._name = name
        self.unified_msg_origin = umo
        self.reply_channel = channel
        self.private = private
        self.panel = panel
        self.audience = audience
        self.sent = []
        self.images = []

        class _Sender:
            user_id = uid

        class _MsgObj:
            sender = _Sender()

        self.message_obj = _MsgObj()

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name

    def get_message_str(self):
        return self._text

    def get_result(self):
        return None

    def plain_result(self, text):
        self.sent.append(text)
        return text

    def image_result(self, path):
        self.images.append(path)
        return path


# ============================ 1. 分层检索 ============================


class TestKnowledgeRetriever(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = knowledge.get_retriever()
        cls.r.ensure_ready()

    def test_index_covers_all_player_docs(self):
        stats = self.r.stats()
        self.assertGreater(stats["chunks"], 500)
        self.assertGreater(stats["items"], 100)
        self.assertIn("新手引导.md", stats["docs"])
        self.assertIn("xiuxian_item_guide.md", stats["docs"])

    def test_core_manual_has_fixed_sections_and_stays_small(self):
        core = self.r.core_manual()
        self.assertTrue(core)
        self.assertLess(len(core), 9000, "核心手册必须能无条件注入，不能膨胀")
        for keyword in ("快速上手", "常见问题"):
            self.assertIn(keyword, core)

    def test_newbie_guide_is_whole_document(self):
        text = self.r.newbie_guide()
        self.assertIn("第一天", text)
        self.assertIn("给 AI 的引导要求", text, "AI 注入版必须保留给 AI 的引导约束")

    def test_search_ranks_relevant_chunk_first(self):
        hits = self.r.search("突破失败会掉境界吗", top_k=5)
        self.assertTrue(hits)
        self.assertTrue(
            any("突破" in h.chunk.title for h in hits[:3]),
            f"前三名应命中突破相关小节，实际：{[h.chunk.title for h in hits[:3]]}",
        )
        scores = [h.score for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_synonym_expansion_helps_recall(self):
        """玩家说「公会」，文档写「宗门」：同义词表必须补上这一跳。"""
        self.assertIn("宗门", self.r.expand_query("公会怎么升级"))
        hits = self.r.search("公会怎么升级", top_k=5)
        self.assertTrue(any("宗门" in h.chunk.title for h in hits))

    def test_item_chunks_index_name_only(self):
        """物品条目正文是模板化描述，进索引只会稀释打分。"""
        item_chunks = [c for c in self.r._chunks if c.kind == "item"]
        self.assertTrue(item_chunks)
        sample = item_chunks[0]
        self.assertTrue(sample.item_name)
        self.assertNotIn("获取途径", "".join(sample.tf.keys()))

    def test_lookup_items_falls_back_to_substring(self):
        hits = self.r.lookup_items(["破境丹"], limit=3)
        self.assertTrue(hits, "精确名对不上时应按子串兜底")
        self.assertTrue(all("破境丹" in h.chunk.item_name for h in hits))

    def test_match_items_recognizes_generic_names(self):
        self.assertTrue(self.r.match_items("洗练石有什么用", limit=3))
        self.assertEqual(self.r.match_items("我为什么突破老是失败", limit=3), [],
                         "非物品问题不应误报物品名")

    def test_admin_sections_hidden_from_players(self):
        admin_hits = self.r.search("管理员命令", top_k=10, include_admin=True)
        player_hits = self.r.search("管理员命令", top_k=10, include_admin=False)
        self.assertTrue(any(h.chunk.admin_only for h in admin_hits) or not admin_hits)
        self.assertFalse(any(h.chunk.admin_only for h in player_hits))

    def test_version_report_flags_stale_docs(self):
        stale = self.r.stale_docs("v8.0.1")
        self.assertIn("物品图鉴", stale, "物品图鉴仍标 v7.9.26，必须被告警出来")
        report = dict((label, ver) for label, ver, _ in self.r.version_report("v8.0.1"))
        self.assertEqual(report.get("新手引导"), "v8.0.1")

    def test_multi_search_rewards_multi_query_hits(self):
        single = self.r.multi_search(["押镖"], top_k=5)
        multi = self.r.multi_search(["押镖", "劫镖", "押镖奖励"], top_k=5)
        self.assertTrue(any(h.matched_queries > 1 for h in multi),
                        "被多个扩展词命中的块应被标记并加权")
        self.assertTrue(single and multi)

    def test_max_chars_budget_respected(self):
        hits = self.r.search("秘境怎么打", top_k=10, max_chars=1500)
        self.assertLessEqual(sum(len(h.chunk.text) for h in hits), 1500 + 1200)

    def test_format_hits_carries_citation(self):
        hits = self.r.search("灵田枯萎", top_k=2)
        text = knowledge.format_hits(hits)
        self.assertIn("[1]", text)
        self.assertIn("《", text)


# ============================ 2. 上下文双缓冲 ============================


class TestChatContextManager(_AsyncRunner):
    def make(self, **kw):
        db = kw.pop("db", _FakeDB())
        return ctx_mod.ChatContextManager(db=db, **kw), db

    def test_personal_and_channel_buffers_are_isolated(self):
        mgr, _ = self.make(persist=False)
        umo = "webroom:group:r1"
        self.run_async(mgr.record_user(umo, "u1", "甲", "我怎么突破", channel="仙界一频道"))
        self.run_async(mgr.record_user(umo, "u2", "乙", "我怎么押镖", channel="仙界一频道"))

        own = self.run_async(mgr.personal_entries(umo, "u1"))
        self.assertEqual([e.text for e in own], ["我怎么突破"], "个人缓冲只能看到自己的话")

        channel = mgr.channel_entries(umo, "仙界一频道")
        self.assertEqual({e.uid for e in channel}, {"u1", "u2"})

    def test_channel_entries_carry_username(self):
        """/chatall 必须区分发言人，否则 AI 分不清谁在问什么。"""
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_user("umo", "u1", "张三", "秘境掉什么", channel="c1"))
        self.run_async(mgr.record_user("umo", "u2", "李四", "押镖怎么接", channel="c1"))
        rendered = mgr.render_channel("umo", "c1")
        self.assertIn("张三", rendered)
        self.assertIn("李四", rendered)

    def test_channels_do_not_leak_across_rooms(self):
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_user("umo", "u1", "甲", "一频道的话", channel="c1"))
        self.run_async(mgr.record_user("umo", "u1", "甲", "二频道的话", channel="c2"))
        self.assertIn("一频道的话", mgr.render_channel("umo", "c1"))
        self.assertNotIn("二频道的话", mgr.render_channel("umo", "c1"))

    def test_private_reply_never_enters_channel_buffer(self):
        """储物戒、银行余额这类「仅自己」结果进公屏缓冲等于泄露他人资产。"""
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_system("umo", "u1", "你的储物戒有 999 灵石", channel="c1", private=True))
        self.assertEqual(mgr.channel_entries("umo", "c1"), [])
        own = self.run_async(mgr.personal_entries("umo", "u1"))
        self.assertEqual(len(own), 1)

    def test_public_reply_enters_both_buffers(self):
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_system("umo", "u1", "签到成功", channel="c1", private=False))
        self.assertEqual(len(mgr.channel_entries("umo", "c1")), 1)
        self.assertEqual(len(self.run_async(mgr.personal_entries("umo", "u1"))), 1)

    def test_clip_keeps_only_first_line_of_battle_report(self):
        report = "\n".join([f"第{i}回合：造成 100 点伤害" for i in range(1, 12)])
        clipped = ctx_mod.clip_text(report, 200)
        self.assertTrue(clipped.startswith("第1回合"))
        self.assertLessEqual(len(clipped), 201)
        self.assertNotIn("第5回合", clipped)

    def test_clip_truncates_single_long_line(self):
        self.assertEqual(len(ctx_mod.clip_text("啊" * 500, 200)), 200)

    def test_ttl_expiry_prunes_old_entries(self):
        mgr, _ = self.make(persist=False, personal_ttl=60, channel_ttl=60)
        self.run_async(mgr.record_user("umo", "u1", "甲", "旧消息", channel="c1"))
        for store in (mgr._personal, mgr._channel):
            for buf in store.values():
                for entry in buf.entries:
                    entry.ts = int(time.time()) - 600
        self.assertEqual(self.run_async(mgr.personal_entries("umo", "u1")), [])
        self.assertEqual(mgr.channel_entries("umo", "c1"), [])

    def test_capacity_cap_keeps_most_recent(self):
        mgr, _ = self.make(persist=False, personal_max=5, channel_max=5)
        for i in range(12):
            self.run_async(mgr.record_user("umo", "u1", "甲", f"消息{i}", channel="c1"))
        own = self.run_async(mgr.personal_entries("umo", "u1"))
        self.assertEqual(len(own), 5)
        self.assertEqual(own[-1].text, "消息11")
        self.assertEqual(own[0].text, "消息7")

    def test_configure_maps_ai_chat_section(self):
        mgr, _ = self.make(persist=False)
        mgr.configure({
            "CONTEXT_MAX": 8, "CONTEXT_TTL_MIN": 5,
            "CHANNEL_CONTEXT_MAX": 12, "CHANNEL_CONTEXT_TTL_MIN": 3,
            "CONTEXT_CLIP": 90,
        })
        self.assertEqual(mgr.personal_max, 8)
        self.assertEqual(mgr.personal_ttl, 300)
        self.assertEqual(mgr.channel_max, 12)
        self.assertEqual(mgr.channel_ttl, 180)
        self.assertEqual(mgr.clip, 90)

    def test_configure_ignores_garbage(self):
        mgr, _ = self.make(persist=False)
        before = (mgr.personal_max, mgr.personal_ttl)
        mgr.configure({"CONTEXT_MAX": "abc", "CONTEXT_TTL_MIN": None})
        self.assertEqual((mgr.personal_max, mgr.personal_ttl), before)

    def test_persistence_roundtrip_via_system_config(self):
        db = _FakeDB()
        mgr, _ = self.make(db=db, persist=True)
        self.run_async(mgr.record_user("umo", "u1", "甲", "第一句"))
        self.run_async(mgr.record_system("umo", "u1", "第一答"))
        self.assertEqual(self.run_async(mgr.flush()), 1)
        self.assertTrue(db.ext.store, "个人上下文应写进 system_config")

        restored, _ = self.make(db=db, persist=True)
        entries = self.run_async(restored.personal_entries("umo", "u1"))
        self.assertEqual([e.text for e in entries], ["第一句", "第一答"])
        self.assertEqual([e.role for e in entries], ["u", "s"])

    def test_persistence_does_not_create_new_tables(self):
        """AI 向导只能复用 system_config，不许新增表或列（v68 来自仙市代理人）。"""
        db = _FakeDB()
        mgr, _ = self.make(db=db, persist=True)
        self.run_async(mgr.record_user("umo", "u1", "甲", "hi"))
        self.run_async(mgr.flush())
        self.assertTrue(all(k.startswith("ai_chat_ctx_") for k in db.ext.store))

    def test_clear_personal_wipes_memory_and_storage(self):
        db = _FakeDB()
        mgr, _ = self.make(db=db, persist=True)
        self.run_async(mgr.record_user("umo", "u1", "甲", "hi"))
        self.run_async(mgr.flush())
        cleared = self.run_async(mgr.clear_personal("umo", "u1"))
        self.assertEqual(cleared, 1)
        self.assertEqual(self.run_async(mgr.personal_entries("umo", "u1")), [])
        self.run_async(mgr.flush())
        self.assertTrue(all(v == "[]" for v in db.ext.store.values()))

    def test_image_only_reply_recorded_as_placeholder(self):
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_image_only("umo", "u1", channel="c1"))
        own = self.run_async(mgr.personal_entries("umo", "u1"))
        self.assertEqual(own[0].text, "[图片]")

    def test_empty_and_uid_less_messages_are_dropped(self):
        mgr, _ = self.make(persist=False)
        self.run_async(mgr.record_user("umo", "", "甲", "没有 uid"))
        self.run_async(mgr.record_user("umo", "u1", "甲", "   "))
        self.assertEqual(self.run_async(mgr.personal_entries("umo", "u1")), [])


# ============================ 3. 输出清洗与拆条 ============================


class TestSanitizeAnswer(unittest.TestCase):
    def test_strips_markdown_noise(self):
        raw = "## 结论\n**先闭关**，再 `/出关`。\n- 第一步\n* 第二步"
        out = chat_mod.sanitize_answer(raw, 400)
        self.assertNotIn("##", out)
        self.assertNotIn("**", out)
        self.assertNotIn("`", out)
        self.assertIn("· 第一步", out)

    def test_strips_code_fence(self):
        out = chat_mod.sanitize_answer("```text\n/签到\n```", 400)
        self.assertNotIn("```", out)
        self.assertIn("/签到", out)

    def test_collapses_blank_lines(self):
        out = chat_mod.sanitize_answer("第一行\n\n\n\n第二行", 400)
        self.assertNotIn("\n\n\n", out)

    def test_truncates_at_sentence_boundary(self):
        raw = "。".join(["这是一段话"] * 200)
        out = chat_mod.sanitize_answer(raw, 200)
        self.assertLessEqual(len(out), 201)
        self.assertTrue(out.endswith("…"))
        self.assertIn("。", out, "应在句末标点处收口，而不是硬切半句")

    def test_short_answer_untouched(self):
        self.assertEqual(chat_mod.sanitize_answer("先发 /签到 即可", 400), "先发 /签到 即可")

    def test_empty_input(self):
        self.assertEqual(chat_mod.sanitize_answer(None, 400), "")
        self.assertEqual(chat_mod.sanitize_answer("   ", 400), "")

    def test_split_answer_respects_limit(self):
        text = "\n\n".join([f"第{i}段说明文字，足够长以便拆条。" * 3 for i in range(6)])
        parts = chat_mod.split_answer(text, 200)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 200)
        self.assertEqual("".join(p.replace("\n\n", "").replace("\n", "") for p in parts).replace(" ", ""),
                         text.replace("\n\n", "").replace("\n", "").replace(" ", ""))

    def test_split_answer_noop_for_short_text(self):
        self.assertEqual(chat_mod.split_answer("短回答", 400), ["短回答"])
        self.assertEqual(chat_mod.split_answer("", 400), [])


# ============================ 4. 问答流程 ============================


class TestAIChatManager(_AsyncRunner):
    def make(self, context=None, player=None, **cfg):
        ctx_mgr = ctx_mod.ChatContextManager(db=_FakeDB(), persist=False)
        mgr = chat_mod.AIChatManager(
            context=context if context is not None else _FakeContext(),
            db=_FakeDB(),
            config_manager=_FakeConfigManager(),
            ctx_mgr=ctx_mgr,
            config={"AI_CHAT": dict(cfg)} if cfg else None,
            retriever=knowledge.get_retriever(),
        )
        return mgr, ctx_mgr

    def test_defaults_applied_without_config(self):
        mgr, _ = self.make()
        self.assertEqual(mgr.cfg["TIMEOUT"], 20)
        self.assertEqual(mgr.cfg["MAX_ANSWER_CHARS"], 400)
        self.assertTrue(mgr.enabled())
        self.assertTrue(mgr.chatall_enabled())

    def test_chatall_switch_is_independent(self):
        mgr, _ = self.make(CHATALL_ENABLED=False)
        self.assertTrue(mgr.enabled())
        self.assertFalse(mgr.chatall_enabled())

    def test_master_switch_disables_both(self):
        mgr, _ = self.make(ENABLED=False)
        self.assertFalse(mgr.enabled())
        self.assertFalse(mgr.chatall_enabled())

    def test_garbage_config_falls_back_to_defaults(self):
        mgr, _ = self.make(TIMEOUT="abc", CONCURRENCY=99, MAX_ANSWER_CHARS=None)
        self.assertEqual(mgr.cfg["TIMEOUT"], 20)
        self.assertLessEqual(mgr.cfg["CONCURRENCY"], 8)
        self.assertEqual(mgr.cfg["MAX_ANSWER_CHARS"], 400)

    # ---------- 限流 ----------

    def test_cooldown_blocks_second_question(self):
        mgr, _ = self.make(COOLDOWN_SEC=30)
        self.assertEqual(mgr.check_rate_limit("u1", "g1", False), "")
        mgr._consume("u1", "g1", False)
        self.assertIn("问得太急", mgr.check_rate_limit("u1", "g1", False))

    def test_cooldown_is_per_user(self):
        mgr, _ = self.make(COOLDOWN_SEC=30)
        mgr._consume("u1", "g1", False)
        self.assertEqual(mgr.check_rate_limit("u2", "g1", False), "")

    def test_daily_quota_blocks(self):
        mgr, _ = self.make(COOLDOWN_SEC=0, DAILY_LIMIT=2)
        mgr._consume("u1", "g1", False)
        mgr._consume("u1", "g1", False)
        self.assertIn("明天再来", mgr.check_rate_limit("u1", "g1", False))

    def test_deep_daily_quota_is_separate(self):
        mgr, _ = self.make(COOLDOWN_SEC=0, DEEP_COOLDOWN_SEC=0, DAILY_LIMIT=30, DEEP_DAILY_LIMIT=1)
        mgr._consume("u1", "g1", True)
        self.assertIn("深度查阅每天限", mgr.check_rate_limit("u1", "g1", True))
        self.assertEqual(mgr.check_rate_limit("u1", "g1", False), "", "普通提问不受深度配额影响")

    def test_group_daily_quota(self):
        mgr, _ = self.make(COOLDOWN_SEC=0, GROUP_DAILY_LIMIT=1)
        mgr._consume("u1", "g1", False)
        self.assertIn("本频道今天", mgr.check_rate_limit("u2", "g1", False))
        self.assertEqual(mgr.check_rate_limit("u2", "g2", False), "")

    def test_zero_limits_mean_unlimited(self):
        mgr, _ = self.make(COOLDOWN_SEC=0, DAILY_LIMIT=0, GROUP_DAILY_LIMIT=0)
        for _ in range(5):
            mgr._consume("u1", "g1", False)
        self.assertEqual(mgr.check_rate_limit("u1", "g1", False), "")

    # ---------- 深度触发 ----------

    def test_wants_deep_by_keyword(self):
        self.assertTrue(chat_mod.AIChatManager.wants_deep("帮我查阅一下押镖机制"))
        self.assertTrue(chat_mod.AIChatManager.wants_deep("解释一下五行克制原理"))
        self.assertFalse(chat_mod.AIChatManager.wants_deep("签到给多少灵石"))

    def test_wants_deep_forced(self):
        self.assertTrue(chat_mod.AIChatManager.wants_deep("签到给多少灵石", force=True))

    # ---------- Provider ----------

    def test_configured_provider_wins(self):
        mgr, _ = self.make(PROVIDER_ID="pinned")
        self.assertEqual(self.run_async(mgr.resolve_provider("umo")), "pinned")

    def test_provider_falls_back_to_session(self):
        mgr, _ = self.make()
        self.assertEqual(self.run_async(mgr.resolve_provider("umo")), "fake-provider")

    def test_missing_provider_yields_fallback(self):
        mgr, _ = self.make(context=_FakeContext(provider=""))
        result = self.run_async(mgr.ask("怎么突破", "umo", "u1"))
        self.assertFalse(result.ok)
        self.assertTrue(result.degraded)
        self.assertIn("Provider", result.messages[0])
        self.assertIn("/修仙帮助", result.messages[0])

    # ---------- 玩家快照 ----------

    def test_snapshot_excludes_assets(self):
        """回答是发在公屏的，快照里绝不能出现灵石与资产。"""
        mgr, _ = self.make()
        snap = mgr.player_snapshot(_Player(gold=888888))
        self.assertIn("剑修", snap)
        self.assertIn("炼气期四层", snap)
        self.assertIn("1,200 / 2,400", snap)
        self.assertNotIn("888888", snap)
        self.assertNotIn("888,888", snap)
        self.assertNotIn("灵石", snap)

    def test_snapshot_can_be_disabled(self):
        mgr, _ = self.make(INCLUDE_PLAYER_STATE=False)
        self.assertEqual(mgr.player_snapshot(_Player()), "")

    def test_snapshot_survives_missing_player(self):
        mgr, _ = self.make()
        self.assertEqual(mgr.player_snapshot(None), "")

    def test_newbie_detection(self):
        mgr, _ = self.make(NEWBIE_MAX_LEVEL_INDEX=5, NEWBIE_MAX_TUTORIAL_STEP=4)
        self.assertTrue(mgr.is_newbie(None), "未创号按新手处理")
        self.assertTrue(mgr.is_newbie(_Player(tutorial_step=1, level_index=20)))
        self.assertTrue(mgr.is_newbie(_Player(tutorial_step=4, level_index=2)))
        self.assertFalse(mgr.is_newbie(_Player(tutorial_step=4, level_index=20)))

    # ---------- 完整问答 ----------

    def test_ask_success_returns_answer_and_sources(self):
        ctx = _FakeContext(answer="先发 /闭关 攒修为，再 /突破。")
        mgr, ctx_mgr = self.make(context=ctx)
        result = self.run_async(mgr.ask("怎么突破境界", "umo", "u1", name="甲", channel="c1"))
        self.assertTrue(result.ok)
        self.assertIn("/突破", result.text)
        self.assertTrue(result.sources, "应给出资料出处，便于玩家自查")
        self.assertGreater(result.elapsed, 0)

    def test_ask_writes_qa_back_to_personal_context(self):
        ctx = _FakeContext(answer="答案在这里")
        mgr, ctx_mgr = self.make(context=ctx)
        self.run_async(mgr.ask("怎么突破", "umo", "u1", name="甲", channel="c1"))
        entries = self.run_async(ctx_mgr.personal_entries("umo", "u1"))
        texts = [e.text for e in entries]
        self.assertTrue(any("/chat 怎么突破" in t for t in texts))
        self.assertTrue(any("答案在这里" in t for t in texts))

    def test_channel_mode_answer_is_public_and_personal_is_not(self):
        """/chatall 的回答别人能看到；/chat 的回答只进个人缓冲。"""
        ctx = _FakeContext(answer="频道答案")
        mgr, ctx_mgr = self.make(context=ctx)
        self.run_async(mgr.ask("问题A", "umo", "u1", name="甲", channel="c1",
                               mode=chat_mod.MODE_CHANNEL))
        self.assertIn("频道答案", ctx_mgr.render_channel("umo", "c1"))

        ctx2 = _FakeContext(answer="私密答案")
        mgr2, ctx_mgr2 = self.make(context=ctx2)
        self.run_async(mgr2.ask("问题B", "umo", "u2", name="乙", channel="c1",
                                mode=chat_mod.MODE_PERSONAL))
        self.assertNotIn("私密答案", ctx_mgr2.render_channel("umo", "c1"))

    def test_prompt_contains_knowledge_and_snapshot(self):
        ctx = _FakeContext(answer="好")
        mgr, _ = self.make(context=ctx)
        self.run_async(mgr.ask("突破失败会掉境界吗", "umo", "u1", player=_Player()))
        prompt = next(c["prompt"] for c in ctx.calls if not c["agent"])
        self.assertIn("【本次问题】", prompt)
        self.assertIn("【玩家状态】", prompt)
        self.assertIn("剑修", prompt)
        self.assertTrue(
            "资料摘录" in prompt or "核心手册" in prompt,
            "提示词必须带检索到的资料，否则等于让模型凭空编",
        )

    def test_system_prompt_forbids_fabrication(self):
        mgr, _ = self.make()
        sp = mgr.build_system_prompt(chat_mod.MODE_PERSONAL, False)
        self.assertIn("禁止编造", sp)
        self.assertIn("以游戏内当前配置为准", sp)
        self.assertIn("400", sp)

    def test_system_prompt_marks_stale_docs(self):
        mgr, _ = self.make()
        sp = mgr.build_system_prompt(chat_mod.MODE_PERSONAL, False, ["物品图鉴"])
        self.assertIn("物品图鉴", sp)
        self.assertIn("落后于当前游戏版本", sp)

    def test_channel_prompt_names_the_asker(self):
        mgr, _ = self.make()
        prompt = mgr.build_prompt("问题", "", "", "", "", chat_mod.MODE_CHANNEL, asker="张三")
        self.assertIn("张三", prompt)

    def test_prompt_budget_drops_history_before_knowledge(self):
        """总闸超限时先砍对话上下文，资料是回答的依据，最后才动。"""
        mgr, _ = self.make(MAX_TOTAL_CHARS=1200)
        prompt = mgr.build_prompt(
            question="问题",
            core="手" * 400,
            hits_text="资" * 400,
            snapshot="",
            history="话" * 4000,
            mode=chat_mod.MODE_PERSONAL,
        )
        self.assertLessEqual(len(prompt), 1400)
        self.assertIn("资", prompt)
        self.assertIn("手", prompt)

    def test_timeout_degrades_gracefully(self):
        ctx = _FakeContext(answer_error=asyncio.TimeoutError())
        mgr, _ = self.make(context=ctx, TIMEOUT=5)
        result = self.run_async(mgr.ask("怎么突破", "umo", "u1"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")
        self.assertIn("超时", result.messages[0])
        self.assertIn("/修仙诊断", result.messages[0])

    def test_llm_error_degrades_gracefully(self):
        ctx = _FakeContext(answer_error=RuntimeError("boom"))
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("怎么突破", "umo", "u1"))
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("llm_error"))
        self.assertIn("/修仙帮助", result.messages[0])

    def test_empty_answer_degrades_with_no_fabrication_notice(self):
        ctx = _FakeContext(answer="   ")
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("怎么突破", "umo", "u1"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_answer")
        self.assertIn("不敢瞎编", result.messages[0])

    def test_empty_question_returns_usage(self):
        mgr, ctx_mgr = self.make()
        result = self.run_async(mgr.ask("   ", "umo", "u1"))
        self.assertFalse(result.ok)
        self.assertIn("/chat", result.messages[0])

    def test_long_question_is_truncated_before_prompt(self):
        ctx = _FakeContext(answer="好")
        mgr, _ = self.make(context=ctx, MAX_QUESTION_CHARS=50)
        self.run_async(mgr.ask("啊" * 500, "umo", "u1"))
        prompt = next(c["prompt"] for c in ctx.calls if not c["agent"])
        self.assertNotIn("啊" * 51, prompt)

    def test_rate_limited_question_never_calls_llm(self):
        ctx = _FakeContext(answer="好")
        mgr, _ = self.make(context=ctx, COOLDOWN_SEC=60)
        self.run_async(mgr.ask("第一问", "umo", "u1"))
        calls_after_first = len(ctx.calls)
        result = self.run_async(mgr.ask("第二问", "umo", "u1"))
        self.assertEqual(len(ctx.calls), calls_after_first, "被限流时不该再打模型")
        self.assertIn("问得太急", result.messages[0])

    def test_failed_call_still_consumes_cooldown(self):
        """模型超时也要计冷却，否则连点就能把额度刷光。"""
        ctx = _FakeContext(answer_error=RuntimeError("boom"))
        mgr, _ = self.make(context=ctx, COOLDOWN_SEC=60)
        self.run_async(mgr.ask("怎么突破", "umo", "u1"))
        self.assertIn("问得太急", mgr.check_rate_limit("u1", "", False))

    def test_deep_keyword_triggers_expansion_agent(self):
        ctx = _FakeContext(answer="深度答案")
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("帮我查阅押镖的完整机制", "umo", "u1"))
        self.assertTrue(result.deep)
        self.assertTrue(any(c["agent"] for c in ctx.calls), "深度模式应先跑一次关键词扩展")

    def test_force_deep_flag(self):
        ctx = _FakeContext(answer="深度答案")
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("签到给什么", "umo", "u1", force_deep=True))
        self.assertTrue(result.deep)

    def test_deep_disabled_ignores_keywords(self):
        ctx = _FakeContext(answer="浅层答案")
        mgr, _ = self.make(context=ctx, DEEP_ENABLED=False)
        result = self.run_async(mgr.ask("帮我查阅押镖机制", "umo", "u1"))
        self.assertFalse(result.deep)
        self.assertFalse(any(c["agent"] for c in ctx.calls))

    def test_low_score_auto_upgrades_to_deep(self):
        ctx = _FakeContext(answer="答案")
        mgr, _ = self.make(context=ctx, DEEP_ON_LOW_SCORE=True, DEEP_SCORE_THRESHOLD=1e9)
        result = self.run_async(mgr.ask("签到给什么", "umo", "u1"))
        self.assertTrue(result.deep, "浅层分数低于阈值时应自动深挖")

    def test_low_score_auto_deep_can_be_turned_off(self):
        ctx = _FakeContext(answer="答案")
        mgr, _ = self.make(context=ctx, DEEP_ON_LOW_SCORE=False, DEEP_SCORE_THRESHOLD=1e9)
        result = self.run_async(mgr.ask("签到给什么", "umo", "u1"))
        self.assertFalse(result.deep)

    def test_agent_failure_falls_back_to_shallow(self):
        """mini agent 挂了不能连累正式回答。"""
        ctx = _FakeContext(answer="浅层答案", agent_error=RuntimeError("agent down"))
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("帮我查阅押镖机制", "umo", "u1"))
        self.assertTrue(result.ok)
        self.assertIn("浅层答案", result.text)

    def test_agent_timeout_falls_back_to_shallow(self):
        ctx = _FakeContext(answer="浅层答案", agent_error=asyncio.TimeoutError())
        mgr, _ = self.make(context=ctx)
        result = self.run_async(mgr.ask("帮我查阅押镖机制", "umo", "u1"))
        self.assertTrue(result.ok)

    def test_agent_kwargs_rejected_then_retried(self):
        """个别 Provider 不认 max_tokens/temperature，去掉参数要能重试成功。"""
        ctx = _FakeContext(answer="答案", accept_kwargs=False)
        mgr, _ = self.make(context=ctx)
        words = self.run_async(mgr.expand_queries("p", "押镖机制", ["押镖"], ""))
        self.assertTrue(words, "带采样参数失败后应去掉参数重试")
        self.assertEqual(len(ctx.calls), 2)
        self.assertEqual(ctx.calls[1]["kwargs"], {})

    def test_expand_queries_parses_strict_lines(self):
        ctx = _FakeContext(expand=["1. 押镖", "- 劫镖", "灵石损失", "", "  护镖  "])
        mgr, _ = self.make(context=ctx)
        words = self.run_async(mgr.expand_queries("p", "押镖", [], ""))
        self.assertEqual(words, ["押镖", "劫镖", "灵石损失", "护镖"])

    def test_expand_queries_caps_at_six(self):
        ctx = _FakeContext(expand=[f"词{i}" for i in range(20)])
        mgr, _ = self.make(context=ctx)
        words = self.run_async(mgr.expand_queries("p", "押镖", [], ""))
        self.assertLessEqual(len(words), 6)

    def test_deep_answer_allows_more_chars_and_splits(self):
        long_answer = "。".join([f"这是第{i}条深度说明" for i in range(80)])
        ctx = _FakeContext(answer=long_answer)
        mgr, _ = self.make(context=ctx, MAX_ANSWER_CHARS=200, DEEP_MAX_ANSWER_CHARS=600)
        result = self.run_async(mgr.ask("帮我查阅押镖机制", "umo", "u1"))
        self.assertTrue(result.deep)
        self.assertLessEqual(len(result.text), 600 + 40)
        self.assertLessEqual(len(result.messages), 2)

    def test_no_knowledge_suffix_when_nothing_matched(self):
        ctx = _FakeContext(answer="只能给通用建议")
        mgr, _ = self.make(context=ctx, KB_TOP_K=1)
        result = self.run_async(mgr.ask("zzz qqq xxx 完全不存在的词", "umo", "u1",
                                        force_deep=False))
        if not result.sources:
            self.assertIn("以游戏内为准", result.text)

    def test_warmup_reports_index_and_stale_docs(self):
        mgr, _ = self.make()
        info = mgr.warmup("v7.12.0")
        self.assertTrue(info["ok"])
        self.assertGreater(info["stats"]["chunks"], 500)
        self.assertIn("物品图鉴", info["stale"])


# ============================ 5. 新手引导 ============================


class TestNewbieGuide(unittest.TestCase):
    def test_sections_parsed_from_document(self):
        sections = newbie_guide.newbie_sections()
        self.assertGreaterEqual(len(sections), 6)
        names = [s["name"] for s in sections]
        self.assertTrue(any("第一天" in n for n in names))
        self.assertTrue(any("玩法体系" in n for n in names))

    def test_ai_only_section_hidden_from_players(self):
        """「给 AI 的引导要求」是提示词素材，不该出现在玩家看到的指南里。"""
        sections = newbie_guide.newbie_sections()
        self.assertFalse(any("给 AI" in s["name"] for s in sections))
        for sec in sections:
            for line in sec["lines"]:
                self.assertNotIn("给 AI 的引导要求", line)

    def test_markdown_markers_stripped(self):
        text = newbie_guide.newbie_text()
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertNotIn("| ---", text)

    def test_tables_become_readable_lines(self):
        text = newbie_guide.newbie_text()
        self.assertIn("剑修", text)
        self.assertIn("｜", text, "表格应转成竖线分隔的单行")

    def test_preamble_before_first_heading_excluded(self):
        sections = newbie_guide.newbie_sections()
        self.assertNotEqual(sections[0]["name"], "开始之前")

    def test_commands_are_copy_pasteable(self):
        text = newbie_guide.newbie_text()
        for cmd in ("/我要修仙", "/签到", "/闭关", "/出关", "/突破信息", "/修仙帮助"):
            self.assertIn(cmd, text)

    def test_text_respects_max_chars(self):
        self.assertLessEqual(len(newbie_guide.newbie_text(max_chars=500)), 500)

    def test_register_page_builds_help_columns_spec(self):
        self.assertTrue(newbie_guide.register_newbie_page())
        pages = import_module(f"{PKG}.utils.static_image_generator").PAGES
        spec = pages.get("newbie")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["renderer"], "help_columns")
        self.assertTrue(spec["sections"])
        self.assertTrue(spec["title"])


# ============================ 6. 指令入口 ============================


class TestAiChatHandler(_AsyncRunner):
    def make(self, context=None, cfg=None, players=None):
        db = _FakeDB(players or {})
        ctx_mgr = ctx_mod.ChatContextManager(db=db, persist=False)
        mgr = chat_mod.AIChatManager(
            context=context if context is not None else _FakeContext(answer="答案"),
            db=db,
            config_manager=_FakeConfigManager(),
            ctx_mgr=ctx_mgr,
            config={"AI_CHAT": cfg or {}, "IMAGE_CARDS": {"ENABLED": False}},
            retriever=knowledge.get_retriever(),
        )
        handler = handler_mod.AiChatHandler(db, {"AI_CHAT": cfg or {}, "IMAGE_CARDS": {"ENABLED": False}},
                                            ai_chat_mgr=mgr, ctx_mgr=ctx_mgr)
        return handler, mgr, ctx_mgr, db

    def drain(self, gen):
        async def _run():
            return [r async for r in gen]
        return self.run_async(_run())

    def test_origin_extracts_webroom_channel(self):
        event = _FakeEvent(umo="webroom:group:r1", channel="仙界三频道", uid="u9", name="阿九")
        umo, uid, name, channel = handler_mod.AiChatHandler.origin(event)
        self.assertEqual((umo, uid, name, channel), ("webroom:group:r1", "u9", "阿九", "仙界三频道"))

    def test_origin_defaults_for_qq(self):
        event = _FakeEvent()
        umo, uid, name, channel = handler_mod.AiChatHandler.origin(event)
        self.assertEqual(channel, "")
        self.assertTrue(umo)

    def test_private_reply_detection(self):
        self.assertTrue(handler_mod.AiChatHandler.is_private_reply(_FakeEvent(private=True)))
        self.assertTrue(handler_mod.AiChatHandler.is_private_reply(_FakeEvent(panel=True)))
        self.assertTrue(handler_mod.AiChatHandler.is_private_reply(_FakeEvent(audience="self")))
        self.assertFalse(handler_mod.AiChatHandler.is_private_reply(_FakeEvent()))

    def test_empty_question_shows_usage(self):
        handler, _, _, _ = self.make()
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chat"), ""))
        self.assertEqual(len(out), 1)
        self.assertIn("AI 向导用法", out[0])
        self.assertIn("/chatall", out[0])
        self.assertIn("/新手指南", out[0])

    def test_help_word_shows_usage(self):
        handler, _, _, _ = self.make()
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chat 帮助"), "帮助"))
        self.assertIn("AI 向导用法", out[0])

    def test_disabled_switch_short_circuits(self):
        handler, _, _, _ = self.make(cfg={"ENABLED": False})
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chat 问题"), "问题"))
        self.assertIn("未启用", out[0])

    def test_chatall_disabled_independently(self):
        handler, _, _, _ = self.make(cfg={"CHATALL_ENABLED": False})
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chatall 问题"), "问题", channel_mode=True))
        self.assertIn("未启用", out[0])

    def test_question_reaches_manager(self):
        ctx = _FakeContext(answer="先发 /闭关 再 /出关")
        handler, _, _, _ = self.make(context=ctx)
        out = self.drain(handler.handle_chat(
            _FakeEvent(text="/chat 怎么攒修为"), "怎么攒修为"))
        self.assertIn("先发 /闭关 再 /出关", out[-1])
        # 「怎么攒修为」这类泛问法可能触发低分自动深挖，两种前缀都合法
        self.assertTrue(out[0].startswith("🤖") or out[0].startswith("🔍"))

    def test_greedy_fallback_reads_full_message(self):
        """GreedyStr 在部分平台退化成只给第一个分段，必须用原始消息兜底。"""
        ctx = _FakeContext(answer="好")
        handler, _, _, _ = self.make(context=ctx)
        event = _FakeEvent(text="/chat 突破 失败 会 掉 境界 吗")
        self.drain(handler.handle_chat(event, "突破"))
        prompt = next(c["prompt"] for c in ctx.calls if not c["agent"])
        self.assertIn("突破 失败 会 掉 境界 吗", prompt)

    def test_clear_command_wipes_context(self):
        handler, _, ctx_mgr, _ = self.make()
        self.run_async(ctx_mgr.record_user("aiocqhttp:GroupMessage:1001", "u1", "甲", "旧对话"))
        out = self.drain(handler.handle_chat(
            _FakeEvent(text="/chat 清空"), "清空"))
        self.assertIn("已忘掉", out[0])
        self.assertEqual(self.run_async(ctx_mgr.personal_entries(
            "aiocqhttp:GroupMessage:1001", "u1")), [])

    def test_deep_prefix_forces_deep_and_is_stripped(self):
        ctx = _FakeContext(answer="深度答案")
        handler, _, _, _ = self.make(context=ctx)
        out = self.drain(handler.handle_chat(
            _FakeEvent(text="/chat 深度 押镖怎么玩"), "深度 押镖怎么玩"))
        self.assertIn("深度查阅", out[0])
        prompt = next(c["prompt"] for c in ctx.calls if not c["agent"])
        self.assertIn("押镖怎么玩", prompt)
        self.assertNotIn("深度 押镖怎么玩", prompt)

    def test_deep_sources_appended(self):
        ctx = _FakeContext(answer="深度答案")
        handler, _, _, _ = self.make(context=ctx)
        out = self.drain(handler.handle_chat(
            _FakeEvent(text="/chat 深度 押镖怎么玩"), "深度 押镖怎么玩"))
        self.assertTrue(any("📚 依据" in m for m in out), "深度回答应附资料出处")

    def test_unregistered_player_blocked_when_configured(self):
        handler, _, _, _ = self.make(cfg={"ALLOW_UNREGISTERED": False})
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chat 问题"), "问题"))
        self.assertIn("尚未踏入仙途", out[0])

    def test_unregistered_player_allowed_by_default(self):
        ctx = _FakeContext(answer="欢迎新道友")
        handler, _, _, _ = self.make(context=ctx)
        out = self.drain(handler.handle_chat(_FakeEvent(text="/chat 问题"), "问题"))
        self.assertIn("欢迎新道友", out[-1])

    def test_newbie_guide_falls_back_to_text(self):
        handler, _, _, _ = self.make()
        image_generator = import_module(f"{PKG}.utils.image_generator")
        image_generator.set_card_flags({"__enabled__": False})
        try:
            event = _FakeEvent(text="/新手指南")
            out = self.drain(handler.handle_newbie_guide(event))
            self.assertEqual(event.images, [], "卡片总开关关闭时不应出图")
        finally:
            image_generator.set_card_flags({"__enabled__": True})
        self.assertIn("新手引导", out[0])
        self.assertIn("/我要修仙", out[0])

    def test_channel_mode_uses_channel_context(self):
        ctx = _FakeContext(answer="频道答案")
        handler, _, ctx_mgr, _ = self.make(context=ctx)
        self.run_async(ctx_mgr.record_user("webroom:group:r1", "u2", "李四", "刚才那个秘境", channel="c1"))
        self.drain(handler.handle_chat(
            _FakeEvent(text="/chatall 掉什么", uid="u1", name="张三",
                       umo="webroom:group:r1", channel="c1"),
            "掉什么", channel_mode=True))
        prompt = next(c["prompt"] for c in ctx.calls if not c["agent"])
        self.assertIn("李四", prompt, "/chatall 必须带上别人的发言并标注用户名")


# ============================ 7. 文档与配置一致性 ============================


class TestWiringConsistency(unittest.TestCase):
    def test_conf_schema_declares_ai_chat(self):
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("AI_CHAT", schema)
        items = schema["AI_CHAT"]["items"]
        for key in ("ENABLED", "PROVIDER_ID", "TIMEOUT", "COOLDOWN_SEC", "DAILY_LIMIT",
                    "DEEP_ENABLED", "DEEP_ON_LOW_SCORE", "CHATALL_ENABLED",
                    "CONTEXT_MAX", "CHANNEL_CONTEXT_MAX", "MAX_TOTAL_CHARS",
                    "NEWBIE_HINT", "MAX_ANSWER_CHARS"):
            self.assertIn(key, items, f"配置项 {key} 缺少 schema 声明")
        self.assertIn("CARD_NEWBIE", schema["IMAGE_CARDS"]["items"])

    def test_schema_defaults_match_code_defaults(self):
        """网页面板显示的默认值必须和代码里的一致，否则改配置会一头雾水。"""
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["AI_CHAT"]["items"]
        code_defaults = chat_mod.DEFAULT_AI_CHAT_CONFIG
        mismatched = []
        for key, spec in items.items():
            if key not in code_defaults:
                continue  # CONTEXT_* 等由 ChatContextManager 消费
            if spec["default"] != code_defaults[key]:
                mismatched.append(f"{key}: schema={spec['default']} code={code_defaults[key]}")
        self.assertEqual(mismatched, [])

    def test_context_schema_defaults_match_code(self):
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["AI_CHAT"]["items"]
        expected = {
            "CONTEXT_MAX": ctx_mod.DEFAULT_PERSONAL_MAX,
            "CONTEXT_TTL_MIN": ctx_mod.DEFAULT_PERSONAL_TTL // 60,
            "CHANNEL_CONTEXT_MAX": ctx_mod.DEFAULT_CHANNEL_MAX,
            "CHANNEL_CONTEXT_TTL_MIN": ctx_mod.DEFAULT_CHANNEL_TTL // 60,
            "CONTEXT_CLIP": ctx_mod.DEFAULT_CLIP,
        }
        for key, value in expected.items():
            self.assertEqual(items[key]["default"], value, f"{key} 默认值不一致")

    def test_main_registers_all_ai_commands(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('CMD_AI_CHAT = "chat"', source)
        self.assertIn('CMD_AI_CHAT_ALL = "chatall"', source)
        self.assertIn('CMD_NEWBIE_GUIDE = "新手指南"', source)
        self.assertIn('@filter.command("问道"', source)
        self.assertIn('@filter.command("群聊问答"', source)
        self.assertIn("@filter.on_decorating_result()", source)
        self.assertIn("@filter.event_message_type(EventMessageType.ALL)", source)

    def test_help_page_lists_new_commands(self):
        pages = import_module(f"{PKG}.utils.static_pages")
        self.assertEqual(pages.HELP_VERSION, "v8.0.1")
        text = pages.build_help_text()
        for cmd in ("/chat", "/chatall", "/新手指南", "/问道", "/群聊问答"):
            self.assertIn(cmd, text)

    def test_webchat_grid_and_aliases_cover_new_commands(self):
        commands = import_module(f"{PKG}.webchat_room.commands")
        grid = {item["cmd"].lstrip("/") for cat in commands.CATEGORIES for item in cat["items"]}
        for cmd in ("chat", "chatall", "新手指南"):
            self.assertIn(cmd, grid)
        self.assertEqual(commands.ALIAS_MAP["问道"], "/chat")
        self.assertEqual(commands.ALIAS_MAP["群聊问答"], "/chatall")
        self.assertIn("chat", commands.ARG_SPEC)
        self.assertIn("chatall", commands.ARG_SPEC)
        ai_cat = next(c for c in commands.CATEGORIES if c["id"] == "ai")
        self.assertEqual(ai_cat["name"], "AI 向导")

    def test_design_doc_and_newbie_doc_exist(self):
        self.assertTrue((ROOT / "docs" / "design" / "AI向导与知识库检索设计方案.md").exists())
        self.assertTrue((ROOT / "docs" / "player" / "新手引导.md").exists())

    def test_newbie_doc_declares_current_game_version(self):
        raw = (ROOT / "docs" / "player" / "新手引导.md").read_text(encoding="utf-8")
        pages = import_module(f"{PKG}.utils.static_pages")
        self.assertIn(f"game_version: {pages.HELP_VERSION}", raw)

    def test_database_version_unchanged(self):
        """AI 向导个人上下文复用 system_config；v71 来自仙域争霸 P1~P3，不是 AI 向导。"""
        migration = (ROOT / "data" / "migration.py").read_text(encoding="utf-8")
        self.assertIn("LATEST_DB_VERSION = 71", migration, "本次发布数据库版本为 v71")
        self.assertNotIn("ai_chat", migration.lower(), "AI 向导不得新增迁移步骤")


if __name__ == "__main__":
    unittest.main()
