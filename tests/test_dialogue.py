"""离线单测：不联网、不调模型、不碰中台。

覆盖三块最容易出问题的地方：
1. 流程配置的引用完整性（写错要在加载期炸）
2. 状态机的挂起/恢复/取消与槽位隔离
3. 引擎在"模型乱说"时的兜底行为
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.conf.settings import settings
from app.core.jsonx import extract_json
from app.core.text import eval_condition, find_tracking_number, normalize, render
from app.dialogue.planner import RulePlanner, TurnPlan
from app.dialogue.state import DialogueState, TaskInstance
from app.dialogue.validator import validate
from app.flows.loader import load
from app.flows.schema import FlowConfigError
from app.infra.commerce_client import CallResult
from app.infra.repo import StateRepo
from app.tracks import knowledge

FLOW_DIR = Path(__file__).resolve().parents[1] / "config" / "flows"
KB_DIR = Path(__file__).resolve().parents[1] / "config" / "kb"


class FakeCommerce:
    """替身中台：行为确定，便于断言"失败了会怎么说"。"""

    def __init__(self) -> None:
        self.shipments = {
            "YD20260410001": ("干线运输中", "已离开华北1仓。", "3C 数码配件", "顺达速运", "次日 18:00 前"),
            "YD20260408002": ("派送中", "快递员已取件。", "办公用品", "顺达速运", "当日 20:00 前"),
            "YD20260403004": ("已妥投", "本人签收。", "服饰箱包", "安捷物流", "隔日 18:00 前"),
        }
        self.claimed = {"YD20260403004"}     # 预置一笔进行中的理赔，用于测 409
        self.calls: list[tuple[str, str]] = []

    async def shipment_detail(self, tracking_number: str) -> CallResult:
        self.calls.append(("detail", tracking_number))
        if tracking_number not in self.shipments:
            return CallResult(False, 404, error=f"没查到运单 {tracking_number}，麻烦确认一下运单号。")
        status, desc, goods, carrier, promise = self.shipments[tracking_number]
        return CallResult(
            True, 200,
            data={
                "tracking_number": tracking_number, "status": status, "status_desc": desc,
                "carrier": carrier, "origin_warehouse": "华北1仓", "destination": "北京市朝阳区",
                "goods_desc": goods, "weight_kg": "2.40", "insured_amount": "5000.00",
                "freight": "18.00", "promise_time": promise, "created_at": "2026-04-10T10:00:00",
                "receiver_name": "李然", "receiver_phone_masked": "138****1234",
                "receiver_address": "北京市朝阳区望京 SOHO",
            },
        )

    async def shipment_status(self, tracking_number: str) -> CallResult:
        return await self.shipment_detail(tracking_number)

    async def shipment_tracking(self, tracking_number: str) -> CallResult:
        self.calls.append(("tracking", tracking_number))
        if tracking_number != "YD20260408002":
            return CallResult(False, 404, error="这张运单还没有轨迹，仓库可能还没出库。")
        return CallResult(
            True, 200,
            data={
                "tracking_number": tracking_number, "carrier": "顺达速运",
                "status": "派送中", "status_desc": "正在派送",
                "traces": [{"time": "2026-04-10T08:20:00", "node": "中关村网点", "desc": "快递员正在派送"}],
            },
        )

    async def submit_claim(
        self, tracking_number: str, claim_type: str, reason: str, operator: str = ""
    ) -> CallResult:
        self.calls.append(("claim", tracking_number))
        if tracking_number in self.claimed:
            return CallResult(False, 409, error="已存在进行中的理赔申请。")
        return CallResult(True, 200,
                          data={"request_id": "CLM-1", "tracking_number": tracking_number,
                                "status": "submitted"})

    async def change_address(
        self, tracking_number: str, new_address: str, operator: str = ""
    ) -> CallResult:
        self.calls.append(("address", tracking_number))
        return CallResult(True, 200,
                          data={"request_id": "CHG-1", "tracking_number": tracking_number,
                                "status": "submitted"})

    async def urge_delivery(
        self, tracking_number: str, reason: str = "", operator: str = ""
    ) -> CallResult:
        self.calls.append(("urge", tracking_number))
        return CallResult(True, 200,
                          data={"request_id": "URG-1", "tracking_number": tracking_number,
                                "status": "submitted"})

    async def customer_shipments(self, customer_id: str) -> CallResult:
        return CallResult(True, 200, data={"shipments": []})


class TextToolsTest(unittest.TestCase):
    def test_render_replaces_placeholders(self):
        self.assertEqual(render("运单{{ slots.no }}到了", {"no": "A1"}), "运单A1到了")

    def test_render_missing_slot_becomes_empty(self):
        self.assertEqual(render("运单{{ slots.no }}到了", {}), "运单到了")

    def test_eval_condition_truthy(self):
        self.assertTrue(eval_condition("slots.get('tracking_number')", {"tracking_number": "SKU1"}, {}))
        self.assertFalse(eval_condition("slots.get('tracking_number')", {}, {}))

    def test_eval_condition_equals(self):
        self.assertTrue(eval_condition("context.get('reason') == 'not_supported'", {}, {"reason": "not_supported"}))

    def test_eval_condition_rejects_arbitrary_code(self):
        # 配置里塞表达式不能被执行，这是安全底线
        self.assertFalse(eval_condition("__import__('os').system('echo hi')", {}, {}))

    def test_find_tracking_number(self):
        self.assertEqual(find_tracking_number("麻烦看下 YD20260410001"), "YD20260410001")
        self.assertEqual(find_tracking_number("运单 202604100011"), "202604100011")
        self.assertEqual(find_tracking_number("没有运单号"), "")

    def test_normalize_fullwidth(self):
        self.assertEqual(normalize("运单号：Ａ２０２６０４１０００１"), "运单号:A20260410001")

    def test_extract_json_ignores_trailing_text(self):
        text = '{"action": "start", "flow": "claim_request"} 这句是解释，还有 {括号}'
        self.assertEqual(extract_json(text)["flow"], "claim_request")

    def test_extract_json_handles_fence(self):
        text = '```json\n{"action": "cancel"}\n```'
        self.assertEqual(extract_json(text)["action"], "cancel")


class FlowLoaderTest(unittest.TestCase):
    def test_loads_all_flows(self):
        registry = load(FLOW_DIR)
        self.assertIn("claim_request", registry.flows)
        self.assertIn("system_cannot_handle", registry.system_flows)
        self.assertEqual(len(registry.flows), 7)

    def test_claim_flow_requires_reason(self):
        registry = load(FLOW_DIR)
        self.assertIn("claim_reason", registry.flows["claim_request"].required_slots)

    def test_catalogue_only_exposes_business_flows(self):
        registry = load(FLOW_DIR)
        ids = {item["id"] for item in registry.catalogue()}
        self.assertIn("tracking_query", ids)
        self.assertNotIn("system_cannot_handle", ids)

    def test_bad_next_target_fails_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_flows.yml"
            path.write_text(
                "slots:\n  no:\n    type: text\n    label: 号\n"
                "flows:\n  f:\n    steps:\n      - id: start\n        type: start\n        next: nowhere\n"
                "      - id: end\n        type: end\n        next: []\n",
                encoding="utf-8",
            )
            with self.assertRaises(FlowConfigError):
                load(Path(tmp))

    def test_undefined_slot_fails_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_flows.yml"
            path.write_text(
                "slots: {}\n"
                "flows:\n  f:\n    steps:\n"
                "      - id: start\n        type: start\n        next: ask\n"
                "      - id: ask\n        type: collect\n        slot_name: ghost\n        next: end\n"
                "      - id: end\n        type: end\n        next: []\n",
                encoding="utf-8",
            )
            with self.assertRaises(FlowConfigError):
                load(Path(tmp))

    def test_unknown_action_fails_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_flows.yml"
            path.write_text(
                "slots: {}\n"
                "flows:\n  f:\n    steps:\n"
                "      - id: start\n        type: start\n        next: do\n"
                "      - id: do\n        type: action\n        action: action_magic\n        next: end\n"
                "      - id: end\n        type: end\n        next: []\n",
                encoding="utf-8",
            )
            with self.assertRaises(FlowConfigError):
                load(Path(tmp))


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.state = DialogueState(sender_id="u1")

    def test_pause_keeps_step_and_slots(self):
        self.state.active_task = TaskInstance("claim_request", "ask_reason", {"tracking_number": "A1"})
        paused = self.state.pause_active(max_paused=3)
        self.assertIs(self.state.active_task, None)
        self.assertEqual(paused.step_id, "ask_reason")
        self.assertEqual(self.state.paused_tasks[0].slots["tracking_number"], "A1")

    def test_resume_from_breakpoint(self):
        self.state.active_task = TaskInstance("claim_request", "ask_reason", {"tracking_number": "A1"})
        self.state.pause_active(max_paused=3)
        revived = self.state.pop_paused("claim_request")
        self.assertEqual(revived.step_id, "ask_reason")
        self.assertEqual(len(self.state.paused_tasks), 0)

    def test_paused_list_is_bounded(self):
        for i in range(5):
            self.state.active_task = TaskInstance(f"flow{i}", "start")
            self.state.pause_active(max_paused=2)
        self.assertEqual(len(self.state.paused_tasks), 2)

    def test_pausing_same_flow_does_not_duplicate(self):
        self.state.active_task = TaskInstance("claim_request", "ask_reason")
        self.state.pause_active(3)
        self.state.active_task = TaskInstance("claim_request", "ask_reason")
        self.state.pause_active(3)
        self.assertEqual([t.flow_id for t in self.state.paused_tasks], ["claim_request"])

    def test_focused_object_fills_tracking_slot(self):
        self.state.focused_object = {"type": "shipment", "tracking_number": "YD20260410001"}
        self.assertEqual(self.state.known_slots()["tracking_number"], "YD20260410001")

    def test_task_scoped_slot_not_leaking(self):
        # 上一单的理赔原因不能自动套到新任务上
        self.state.slots["claim_reason"] = "旧原因"
        self.assertNotIn("claim_reason", self.state.known_slots())

    def test_serialization_roundtrip(self):
        self.state.active_task = TaskInstance("claim_request", "ask_reason", {"tracking_number": "A1"})
        self.state.focused_object = {"type": "shipment", "tracking_number": "A1"}
        restored = DialogueState.from_dict(self.state.to_dict())
        self.assertEqual(restored.active_task.step_id, "ask_reason")
        self.assertEqual(restored.focused_object["tracking_number"], "A1")

    def test_history_is_bounded(self):
        for i in range(250):
            self.state.push_message("user", {"text": str(i)})
        self.assertEqual(len(self.state.messages), 200)


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        self.registry = load(FLOW_DIR)
        self.state = DialogueState(sender_id="u1")

    def _state_at(self, flow_id: str, step_id: str, **slots) -> DialogueState:
        """造一个"流程正停在某一步追问"的状态——槽位校验判的是追问上下文。"""
        state = DialogueState(sender_id="u1")
        state.slots.update(slots)
        state.active_task = TaskInstance(flow_id=flow_id, step_id=step_id, turn_started=1)
        return state

    def test_unknown_flow_becomes_clarify(self):
        plan, notes = validate(
            TurnPlan("start", "flow_that_does_not_exist", confidence=0.9), self.state, self.registry
        )
        self.assertEqual(plan.action, "clarify")
        self.assertTrue(notes)

    def test_flow_in_action_field_is_repaired(self):
        # 模型把 flow 名填进 action，是很常见的串字段
        plan, notes = validate(
            TurnPlan("tracking_query", confidence=0.9), self.state, self.registry
        )
        self.assertEqual((plan.action, plan.flow_id), ("start", "tracking_query"))
        self.assertTrue(any("修正" in n for n in notes))

    def test_unknown_slot_is_dropped(self):
        plan, notes = validate(
            TurnPlan("start", "claim_request", {"ghost": "x", "tracking_number": "A1"}, 0.9),
            self.state, self.registry,
        )
        self.assertNotIn("ghost", plan.slots)
        self.assertIn("tracking_number", plan.slots)
        self.assertTrue(any("ghost" in n for n in notes))

    def test_resume_without_paused_restarts_named_flow(self):
        # 取消掉的任务不在挂起列表里，"继续刚才的理赔"只能重新发起；
        # 用户意图是清楚的，反问"你什么意思"是浪费一轮。
        plan, notes = validate(
            TurnPlan("resume", "claim_request", confidence=0.9), self.state, self.registry
        )
        self.assertEqual((plan.action, plan.flow_id), ("start", "claim_request"))
        self.assertTrue(any("重新开始" in n for n in notes))

    def test_resume_without_paused_and_no_flow_becomes_clarify(self):
        plan, _ = validate(TurnPlan("resume", confidence=0.9), self.state, self.registry)
        self.assertEqual(plan.action, "clarify")

    def test_resume_wrong_flow_becomes_clarify(self):
        self.state.paused_tasks.append(TaskInstance("tracking_query", "ask_tracking_number"))
        plan, _ = validate(TurnPlan("resume", "claim_request", confidence=0.9), self.state, self.registry)
        self.assertEqual(plan.action, "clarify")

    def test_cancel_without_task_becomes_chitchat(self):
        plan, _ = validate(TurnPlan("cancel", confidence=0.9), self.state, self.registry)
        self.assertEqual(plan.action, "chitchat")

    def test_low_confidence_becomes_clarify(self):
        plan, _ = validate(
            TurnPlan("start", "claim_request", confidence=0.2), self.state, self.registry
        )
        self.assertEqual(plan.action, "clarify")

    # ---- 运单号一致性：模型的抄写错误不能落到中台去 ----
    def test_model_typo_tracking_number_is_corrected_to_known(self):
        # 真实踩过：模型把 YD20260410001 抄成 YD2026041001，打到中台直接 404
        self.state.slots["tracking_number"] = "YD20260410001"
        plan, notes = validate(
            TurnPlan("start", "claim_request", {"tracking_number": "YD2026041001"}, 0.95),
            self.state, self.registry, "不想要了",
        )
        self.assertEqual(plan.slots["tracking_number"], "YD20260410001")
        self.assertTrue(any("纠正" in n for n in notes))

    def test_user_text_tracking_number_wins(self):
        self.state.slots["tracking_number"] = "YD20260410001"
        plan, _ = validate(
            TurnPlan("start", "claim_request", {"tracking_number": "YD20260408002"}, 0.95),
            self.state, self.registry, "改成退 YD20260408002",
        )
        self.assertEqual(plan.slots["tracking_number"], "YD20260408002")

    # ---- 问的是当前这单的物流字段，就别去知识库里瞎搜 ----
    def test_logistics_attribute_question_rerouted(self):
        self.state.slots["tracking_number"] = "YD20260408002"
        plan, notes = validate(
            TurnPlan("answer", confidence=0.95), self.state, self.registry, "运单号是多少"
        )
        self.assertEqual((plan.action, plan.flow_id), ("start", "tracking_query"))
        self.assertTrue(any("物流" in n for n in notes))

    def test_claim_id_question_not_rerouted_to_logistics(self):
        # "理赔单号是多少"里也有"单号"，但它问的是理赔，不能被改派到轨迹
        self.state.slots["tracking_number"] = "YD20260408002"
        plan, _ = validate(
            TurnPlan("answer", confidence=0.95), self.state, self.registry, "理赔单号是多少"
        )
        self.assertEqual(plan.action, "answer")

    # ---- 可行性问句 ≠ 办事请求 ----
    def test_feasibility_question_not_treated_as_claim(self):
        # 真实踩过：模型把"那这单能理赔吗"判成 start/claim_request，
        # 于是下一句就问"异常情况是什么"，用户还没说要报案呢
        plan, notes = validate(
            TurnPlan("start", "claim_request", confidence=0.95), self.state, self.registry,
            "那这单能理赔吗",
        )
        self.assertEqual(plan.action, "answer")
        self.assertTrue(any("能不能办" in n for n in notes))

    def test_explicit_claim_request_still_starts_flow(self):
        for text in ("直接帮我申请理赔", "我要理赔", "能不能帮我理赔"):
            with self.subTest(text=text):
                plan, _ = validate(
                    TurnPlan("start", "claim_request", confidence=0.95),
                    self.state, self.registry, text,
                )
                self.assertEqual((plan.action, plan.flow_id), ("start", "claim_request"))

    # ---- 意图句不能当理赔原因 ----
    def test_intent_sentence_not_used_as_claim_reason(self):
        plan, notes = validate(
            TurnPlan("start", "claim_request",
                     {"claim_reason": "直接帮我申请理赔"}, 0.95),
            self.state, self.registry, "直接帮我申请理赔",
        )
        self.assertNotIn("claim_reason", plan.slots)
        self.assertTrue(any("不是情况说明" in n for n in notes))

    def test_real_reason_is_kept(self):
        # 真实场景：流程正停在 ask_reason 追问，用户这句话就是在回答它
        for text in ("不想要了", "拍错了", "质量不好", "运单与描述不符"):
            with self.subTest(text=text):
                state = self._state_at("claim_request", "ask_reason")
                plan, _ = validate(
                    TurnPlan("start", "claim_request", {"claim_reason": text}, 0.95),
                    state, self.registry, text,
                )
                self.assertEqual(plan.slots.get("claim_reason"), text)

    def test_reason_dropped_before_tracking_number_is_known(self):
        # 用户只报了意图、还没给运单号，模型却把触发词"破损"填成了原因。
        # 不丢掉的话，下一轮刚拿到单号就会跳过 ask_reason 直接提交，
        # 理赔单上写的是模型替用户编的原因。
        plan, notes = validate(
            TurnPlan("start", "claim_request", {"claim_reason": "破损"}, 0.95),
            self.state, self.registry, "包裹破损了，我要报异常",
        )
        self.assertNotIn("claim_reason", plan.slots)
        self.assertTrue(any("回答追问" in n for n in notes))

    def test_reason_kept_when_user_says_it_with_tracking_number(self):
        # 一句话把单号和原因都说完是合法的一轮办完，不该逼用户再说一遍
        plan, _ = validate(
            TurnPlan("start", "claim_request",
                     {"tracking_number": "YD20260405003", "claim_reason": "箱子压坏了"}, 0.95),
            self.state, self.registry, "YD20260405003 箱子压坏了",
        )
        self.assertEqual(plan.slots.get("claim_reason"), "箱子压坏了")

    def test_new_address_follows_the_same_rule(self):
        plan, _ = validate(
            TurnPlan("start", "address_change", {"new_address": "北京市朝阳区建国路 88 号"}, 0.95),
            self.state, self.registry, "我要改派送地址",
        )
        self.assertNotIn("new_address", plan.slots)

    def test_logistics_policy_without_data_stays_answer(self):
        # 状态里没有任何物流数据时，"物流一般几天到"还是政策问题
        plan, _ = validate(
            TurnPlan("answer", confidence=0.95), self.state, self.registry, "物流一般几天到"
        )
        self.assertEqual(plan.action, "answer")


class RulePlannerTest(unittest.TestCase):
    """规则规划器是"没有模型时的下限"，它的每条启发式都要能说清理由。"""

    def setUp(self):
        self.registry = load(FLOW_DIR)
        self.planner = RulePlanner()
        self.state = DialogueState(sender_id="u1")

    def plan(self, text: str):
        return self.planner.plan(text, self.state, self.registry)

    def test_policy_question_goes_to_answer(self):
        for text in ("破损件怎么赔", "延误了能赔多少", "哪些东西是禁运品吗", "派送时效怎么算"):
            with self.subTest(text=text):
                self.assertEqual(self.plan(text).action, "answer")

    def test_action_request_still_starts_flow(self):
        # "能不能帮我理赔"带着请求动作，不能当政策问
        self.assertEqual(self.plan("能不能帮我理赔").flow_id, "claim_request")
        self.assertEqual(self.plan("我要申请理赔").flow_id, "claim_request")

    def test_bare_tracking_number_defaults_to_status_query(self):
        self.assertEqual(self.plan("查一下 YD20260408002").flow_id, "shipment_status_query")

    def test_tracking_number_while_collecting_is_treated_as_slot_value(self):
        # 理赔流程正在问运单号时，用户只回一个单号，不能被当成"查运单"
        self.state.active_task = TaskInstance("claim_request", "ask_tracking_number")
        plan = self.plan("YD20260410001")
        self.assertEqual(plan.flow_id, "claim_request")
        self.assertEqual(plan.slots.get("tracking_number"), "YD20260410001")

    def test_tracking_number_with_explicit_intent_keeps_intent(self):
        self.assertEqual(self.plan("帮我查下 YD20260408002 的物流").flow_id, "tracking_query")


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.passages = knowledge.load_passages(KB_DIR)

    def test_loads_sections(self):
        self.assertGreaterEqual(len(self.passages), 8)
        titles = [p.title for p in self.passages]
        self.assertIn("破损件的赔付标准", titles)
        self.assertIn("修改派送地址", titles)

    def test_retrieve_hits_by_keyword(self):
        hits = knowledge.retrieve("包裹破损了怎么赔的", self.passages)
        self.assertEqual(hits[0].title, "破损件的赔付标准")

    def test_retrieve_returns_empty_for_unrelated(self):
        hits = knowledge.retrieve("明天天气怎么样", self.passages)
        self.assertTrue(not hits or hits[0].score < knowledge.MIN_SCORE)

    def test_expand_adds_synonyms(self):
        self.assertIn("赔付", knowledge.expand("包裹坏了怎么赔"))

    def test_context_enables_pronoun_question(self):
        # 只问"能赔吗"检索不到；补上扩展词后才可能命中
        weak = knowledge.retrieve("那这单能赔吗", self.passages)
        strong = knowledge.retrieve(knowledge.expand("那这单能赔吗"), self.passages)
        self.assertLess(weak[0].score if weak else 0, strong[0].score if strong else 0)


class EngineTest(unittest.TestCase):
    """引擎测试：关掉模型，用替身中台，验证状态流转。"""

    def setUp(self):
        import app.actions.business as business

        self._orig_client = business.commerce_client
        self.fake = FakeCommerce()
        business.commerce_client = self.fake

        self._orig_key = settings.llm.api_key
        settings.llm.api_key = ""  # 强制走规则规划器，结果才可复现

        self.tmp = tempfile.TemporaryDirectory()
        from app.dialogue.engine import DialogueEngine

        self.engine = DialogueEngine(
            load(FLOW_DIR), StateRepo(Path(self.tmp.name) / "s.db"), knowledge.load_passages(KB_DIR)
        )

    def tearDown(self):
        import app.actions.business as business

        business.commerce_client = self._orig_client
        settings.llm.api_key = self._orig_key
        self.tmp.cleanup()

    def say(self, text: str, obj: dict | None = None):
        return asyncio.run(self.engine.handle("u1001", text, obj))

    def test_interrupt_does_not_leak_slot_into_paused_task(self):
        """理赔办到一半去查物流，回来接着办时，退的必须还是原来那一单。

        真实踩过：槽位先写进"当时的 active_task"再挂起，于是物流的运单号
        落到了挂起的理赔任务上，恢复后把另一单给退了。
        """
        self.say("我要理赔")
        self.say("YD20260410001")
        self.say("查一下 YD20260408002 的物流")
        resumed = self.say("继续刚才的理赔")
        self.assertTrue(any("paused->active" in t for t in resumed.transitions))
        final = self.say("拍错了")

        claim_calls = [c for c in self.fake.calls if c[0] == "claim"]
        self.assertEqual(claim_calls, [("claim", "YD20260410001")])
        self.assertIn("CLM-1", " ".join(m.get("text", "") for m in final.messages))

    def test_paused_task_keeps_its_own_slots(self):
        self.say("我要理赔")
        self.say("YD20260410001")
        self.say("查一下 YD20260408002 的物流")
        state = self.engine.repo.get("u1001")
        paused = state.paused_tasks[0]
        self.assertEqual(paused.flow_id, "claim_request")
        self.assertEqual(paused.slots.get("tracking_number"), "YD20260410001")
        # 全局槽位跟着最新上下文走
        self.assertEqual(state.slots.get("tracking_number"), "YD20260408002")

    def test_query_order_returns_card_and_status(self):
        result = self.say("帮我查运单 YD20260408002")
        self.assertEqual(result.plan.flow_id, "shipment_status_query")
        card = next((m["object"] for m in result.messages if m.get("object")), None)
        self.assertIsNotNone(card, "应该返回一张运单卡片")
        self.assertEqual(card["status"], "派送中")

    def test_logistics_query_mentions_company(self):
        result = self.say("帮我看下物流 YD20260408002")
        self.assertEqual(result.plan.flow_id, "tracking_query")
        text = " ".join(m.get("text", "") for m in result.messages)
        self.assertIn("顺达速运", text)

    def test_collect_asks_when_slot_missing(self):
        result = self.say("我要理赔")
        self.assertIn("运单号", result.messages[-1]["text"])
        state = self.engine.repo.get("u1001")
        self.assertEqual(state.active_task.step_id, "ask_tracking_number")

    def test_interruption_pauses_previous_task(self):
        self.say("我要理赔")
        self.say("先查物流 YD20260408002")
        state = self.engine.repo.get("u1001")
        # 物流查完即结束，所以此刻没有进行中的任务；重点是理赔被挂起了
        self.assertIsNone(state.active_task)
        self.assertIn("claim_request", [t.flow_id for t in state.paused_tasks])

    def test_interruption_speaks_acknowledgement(self):
        self.say("我要理赔")
        result = self.say("先查物流 YD20260408002")
        first = result.messages[0].get("text", "")
        self.assertIn("放一放", first)

    def test_resume_continues_from_breakpoint(self):
        self.say("我要理赔")
        self.say("先查物流 YD20260408002")
        self.say("继续刚才的理赔")
        state = self.engine.repo.get("u1001")
        self.assertEqual(state.active_task.flow_id, "claim_request")
        # 运单号已在上文出现过，恢复后应直接问到原因，而不是再问运单号
        self.assertEqual(state.active_task.step_id, "ask_reason")

    def test_cancel_clears_active_task(self):
        self.say("我要理赔")
        result = self.say("取消了")
        self.assertIn("不办了", result.messages[-1]["text"])
        self.assertIsNone(self.engine.repo.get("u1001").active_task)

    def test_claim_success_returns_request_id(self):
        result = self.say("给 YD20260410001 理赔，原因不想要了")
        text = " ".join(m.get("text", "") for m in result.messages)
        self.assertIn("CLM-1", text)

    def test_claim_conflict_isexplained_in_plain_words(self):
        result = self.say("给 YD20260403004 理赔，原因不想要了")
        text = " ".join(m.get("text", "") for m in result.messages)
        self.assertIn("已经有一笔理赔", text)

    def test_failed_action_does_not_print_success_template(self):
        # 这是最致命的一类错误：理赔没成功却告诉用户已提交
        result = self.say("给 YD20260403004 理赔，原因不想要了")
        text = " ".join(m.get("text", "") for m in result.messages)
        self.assertNotIn("审核结果会通过短信通知你", text)

    def test_object_message_skips_planner(self):
        result = self.say("", {"type": "shipment", "tracking_number": "YD20260408002"})
        self.assertEqual(result.plan.source, "object")
        self.assertEqual(result.plan.flow_id, "shipment_status_query")

    def test_state_persisted_between_turns(self):
        self.say("我要理赔")
        self.say("YD20260410001")
        state = self.engine.repo.get("u1001")
        self.assertEqual(state.slots["tracking_number"], "YD20260410001")


if __name__ == "__main__":
    unittest.main()
