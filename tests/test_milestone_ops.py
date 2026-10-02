from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from milestone_ops.api import JsonApplication
from milestone_ops.clock import FrozenClock
from milestone_ops.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from milestone_ops.rules import evaluate_evidence, next_action, partial_release
from milestone_ops.service import TranslationMilestoneService
from milestone_ops.storage import connect


def make_clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc))


def boot(service: TranslationMilestoneService) -> None:
    for user_id, role in (
        ("research", "research_lead"),
        ("advisor", "advisor"),
        ("fund", "fund_manager"),
        ("fund2", "fund_manager"),
        ("gov", "governance"),
        ("secretary", "secretary"),
    ):
        service.create_user(user_id, user_id, role)
    service.create_rule_set(
        "gov",
        {
            "rule_set_id": "rules-1",
            "name": "转化评审规则 v1",
            "required_evidence_kinds": ["experiment_result", "clinical_assessment"],
            "min_evidence_items": 2,
            "partial_acceptance": {"allowed": True, "min_percent": "25", "max_percent": "75"},
        },
    )
    service.activate_rule_set("gov", "rules-1")
    service.create_funding_source("fund", {"source_id": "src-1", "name": "转化基金一期", "total_amount_cny": "5000000"})
    service.create_project("gov", {"project_id": "proj-1", "name": "口服 GLP-1 前体", "research_lead_id": "research"})
    service.sign_milestone(
        "gov",
        {
            "milestone_id": "ms-1",
            "project_id": "proj-1",
            "sequence": 1,
            "title": "体外活性与选择性验证",
            "experiment_plan": "双靶点细胞实验三批重复",
            "clinical_hypothesis": "2 型糖尿病肥胖亚组口服需求",
            "commercial_path": "国内自持，海外授权",
            "funding_source_id": "src-1",
            "planned_amount_cny": "1000000",
        },
    )


def submit_two_evidence(service: TranslationMilestoneService, milestone_id: str = "ms-1") -> None:
    service.submit_evidence(
        "research",
        milestone_id,
        {"evidence_id": f"{milestone_id}-e-1", "kind": "experiment_result", "summary": "IC50 达标", "content_sha256": "a" * 64},
    )
    service.submit_evidence(
        "research",
        milestone_id,
        {"evidence_id": f"{milestone_id}-e-2", "kind": "clinical_assessment", "summary": "未满足需求明确", "content_sha256": "b" * 64},
    )


def schedule(service: TranslationMilestoneService, disbursement_id: str = "d-1", amount: str = "400000", milestone_id: str = "ms-1") -> dict[str, object]:
    return service.schedule_disbursement(
        "fund",
        milestone_id,
        {"disbursement_id": disbursement_id, "amount_cny": amount, "idempotency_key": f"{disbursement_id}-key"},
    )


def propose(service: TranslationMilestoneService, action: str = "accept", decision_id: str = "dec-1", milestone_id: str = "ms-1", **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_id": decision_id,
        "action": action,
        "note": "季度会议结论",
        "idempotency_key": f"{decision_id}-key",
    }
    payload.update(extra)
    return service.propose_decision("gov", milestone_id, payload)


class RulesTests(unittest.TestCase):
    def test_evaluate_evidence_lists_missing_conditions(self) -> None:
        rule = {"required_evidence_kinds": ["experiment_result", "clinical_assessment"], "min_evidence_items": 2}
        result = evaluate_evidence(rule, [{"kind": "experiment_result"}])
        self.assertFalse(result["satisfied"])
        self.assertEqual(result["missing_conditions"], ["缺少证据类型 clinical_assessment", "证据数量不足：需要 2 条，当前 1 条"])
        satisfied = evaluate_evidence(rule, [{"kind": "experiment_result"}, {"kind": "clinical_assessment"}])
        self.assertTrue(satisfied["satisfied"])

    def test_partial_release_splits_amount(self) -> None:
        released, retained = partial_release(Decimal("400000"), Decimal("50"))
        self.assertEqual(released, Decimal("200000.00"))
        self.assertEqual(retained, Decimal("200000.00"))
        with self.assertRaises(ValueError):
            partial_release(Decimal("100"), Decimal("100"))

    def test_next_action_tracks_state_machine(self) -> None:
        self.assertEqual(next_action(milestone_state="signed", pending_decision=None, open_disputes=0), "submit_evidence")
        self.assertEqual(next_action(milestone_state="submitted", pending_decision=None, open_disputes=0), "propose_decision")
        pending = {"science_signed_by": None, "finance_signed_by": None}
        self.assertEqual(next_action(milestone_state="submitted", pending_decision=pending, open_disputes=0), "science_signoff")
        self.assertEqual(next_action(milestone_state="submitted", pending_decision=pending, open_disputes=1), "resolve_dispute")
        self.assertEqual(next_action(milestone_state="suspended", pending_decision=None, open_disputes=0), "resume_milestone")
        self.assertEqual(next_action(milestone_state="accepted", pending_decision=None, open_disputes=0), "none")


class MilestoneServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = make_clock()
        self.service = TranslationMilestoneService(self.connection, self.clock)
        boot(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_signing_pins_active_rule_version(self) -> None:
        milestone = self.connection.execute("SELECT * FROM milestones WHERE milestone_id='ms-1'").fetchone()
        self.assertEqual(milestone["rule_version"], 1)
        self.service.create_rule_set(
            "gov",
            {
                "rule_set_id": "rules-2",
                "name": "转化评审规则 v2",
                "required_evidence_kinds": ["experiment_result"],
                "min_evidence_items": 1,
                "partial_acceptance": {"allowed": False},
            },
        )
        self.service.activate_rule_set("gov", "rules-2")
        signed = self.service.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验",
                "experiment_plan": "28 天重复给药",
                "clinical_hypothesis": "安全性窗口",
                "commercial_path": "专利先行",
                "funding_source_id": "src-1",
                "planned_amount_cny": "500000",
            },
        )
        self.assertEqual(signed["rule_version"], 2)
        again = self.connection.execute("SELECT rule_version FROM milestones WHERE milestone_id='ms-1'").fetchone()
        self.assertEqual(again[0], 1)

    def test_signing_requires_active_rule_set(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        service = TranslationMilestoneService(connection, make_clock())
        service.create_user("gov", "gov", "governance")
        service.create_user("research", "research", "research_lead")
        service.create_user("fund", "fund", "fund_manager")
        service.create_funding_source("fund", {"source_id": "src-x", "name": "基金", "total_amount_cny": "100"})
        service.create_project("gov", {"project_id": "p-x", "name": "项目", "research_lead_id": "research"})
        with self.assertRaises(InvalidState):
            service.sign_milestone(
                "gov",
                {
                    "milestone_id": "ms-x",
                    "project_id": "p-x",
                    "sequence": 1,
                    "title": "节点",
                    "experiment_plan": "方案",
                    "clinical_hypothesis": "假设",
                    "commercial_path": "路径",
                    "funding_source_id": "src-x",
                    "planned_amount_cny": "100",
                },
            )
        connection.close()

    def test_full_acceptance_releases_disbursement(self) -> None:
        schedule(self.service)
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        science = self.service.sign_decision_science("advisor", "dec-1")
        self.assertEqual(science["state"], "pending")
        confirmed = self.service.sign_decision_finance("fund", "dec-1")
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["milestone_state"], "accepted")
        self.assertEqual(confirmed["released_total_cny"], "400000.00")
        disbursement = self.connection.execute("SELECT * FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(disbursement["state"], "released")
        self.assertEqual(disbursement["released_amount_cny"], "400000")
        self.assertEqual(disbursement["decision_id"], "dec-1")
        source = self.service.funding_source("src-1")
        self.assertEqual(source["available_amount_cny"], "4600000.00")
        self.assertEqual(source["released_amount_cny"], "400000.00")

    def test_partial_accept_releases_proportion(self) -> None:
        schedule(self.service)
        submit_two_evidence(self.service)
        propose(self.service, "partial_accept", accept_percent="50")
        self.service.sign_decision_science("advisor", "dec-1")
        confirmed = self.service.sign_decision_finance("fund", "dec-1")
        self.assertEqual(confirmed["milestone_state"], "partially_accepted")
        self.assertEqual(confirmed["released_total_cny"], "200000.00")
        disbursement = self.connection.execute("SELECT * FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(disbursement["state"], "partially_released")
        self.assertEqual(disbursement["released_amount_cny"], "200000.00")

    def test_partial_accept_percent_checked_against_signed_rule_version(self) -> None:
        submit_two_evidence(self.service)
        with self.assertRaises(ValidationFailed):
            propose(self.service, "partial_accept", accept_percent="80")
        self.service.create_rule_set(
            "gov",
            {
                "rule_set_id": "rules-2",
                "name": "转化评审规则 v2",
                "required_evidence_kinds": ["experiment_result"],
                "min_evidence_items": 1,
                "partial_acceptance": {"allowed": True, "min_percent": "1", "max_percent": "99"},
            },
        )
        self.service.activate_rule_set("gov", "rules-2")
        # ms-1 钉住 v1，80% 仍超出其 25-75 的范围
        with self.assertRaises(ValidationFailed):
            propose(self.service, "partial_accept", accept_percent="80")

    def test_return_for_evidence_keeps_disbursement_and_allows_resubmission(self) -> None:
        schedule(self.service)
        submit_two_evidence(self.service)
        propose(self.service, "return_for_evidence")
        self.service.sign_decision_science("advisor", "dec-1")
        confirmed = self.service.sign_decision_finance("fund", "dec-1")
        self.assertEqual(confirmed["milestone_state"], "returned")
        self.assertEqual(confirmed["released_total_cny"], "0.00")
        disbursement = self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(disbursement[0], "scheduled")
        self.service.submit_evidence(
            "research",
            "ms-1",
            {"evidence_id": "e-3", "kind": "commercial_analysis", "summary": "海外意向书", "content_sha256": "c" * 64},
        )
        milestone = self.connection.execute("SELECT state FROM milestones WHERE milestone_id='ms-1'").fetchone()
        self.assertEqual(milestone[0], "submitted")
        propose(self.service, "accept", decision_id="dec-2")
        self.service.sign_decision_science("advisor", "dec-2")
        final = self.service.sign_decision_finance("fund", "dec-2")
        self.assertEqual(final["milestone_state"], "accepted")

    def test_suspend_holds_disbursement_and_resume_restores(self) -> None:
        schedule(self.service)
        submit_two_evidence(self.service)
        propose(self.service, "suspend")
        self.service.sign_decision_science("advisor", "dec-1")
        confirmed = self.service.sign_decision_finance("fund", "dec-1")
        self.assertEqual(confirmed["milestone_state"], "suspended")
        disbursement = self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(disbursement[0], "held")
        with self.assertRaises(InvalidState):
            self.service.submit_evidence(
                "research",
                "ms-1",
                {"evidence_id": "e-3", "kind": "experiment_result", "summary": "补充", "content_sha256": "c" * 64},
            )
        resumed = self.service.resume_milestone("gov", "ms-1")
        self.assertEqual(resumed["state"], "signed")
        disbursement = self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(disbursement[0], "scheduled")

    def test_science_and_finance_signoff_require_distinct_actors(self) -> None:
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        with self.assertRaises(Forbidden):
            self.service.sign_decision_finance("advisor", "dec-1")
        with self.assertRaises(Forbidden):
            self.service.sign_decision_science("fund", "dec-1")
        self.service.sign_decision_science("advisor", "dec-1")
        confirmed = self.service.sign_decision_finance("fund", "dec-1")
        self.assertEqual(confirmed["state"], "confirmed")
        with self.assertRaises(InvalidState):
            self.service.sign_decision_science("advisor", "dec-1")

    def test_database_enforces_distinct_signers(self) -> None:
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        self.service.sign_decision_science("advisor", "dec-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE milestone_decisions SET finance_signed_by='advisor' WHERE decision_id='dec-1'"
            )

    def test_double_signoff_same_side_rejected(self) -> None:
        submit_two_evidence(self.service)
        propose(self.service, "return_for_evidence")
        self.service.sign_decision_science("advisor", "dec-1")
        with self.assertRaises(Conflict):
            self.service.sign_decision_science("advisor", "dec-1")
        self.service.sign_decision_finance("fund", "dec-1")
        self.service.submit_evidence(
            "research",
            "ms-1",
            {"evidence_id": "e-3", "kind": "commercial_analysis", "summary": "补充", "content_sha256": "c" * 64},
        )
        propose(self.service, "suspend", decision_id="dec-2")
        self.service.sign_decision_finance("fund", "dec-2")
        with self.assertRaises(Conflict):
            self.service.sign_decision_finance("fund2", "dec-2")

    def test_dispute_freezes_only_related_disbursements(self) -> None:
        schedule(self.service, "d-1", "400000")
        self.service.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验",
                "experiment_plan": "28 天重复给药",
                "clinical_hypothesis": "安全性窗口",
                "commercial_path": "专利先行",
                "funding_source_id": "src-1",
                "planned_amount_cny": "500000",
            },
        )
        schedule(self.service, "d-2", "300000", milestone_id="ms-2")
        dispute = self.service.raise_dispute("fund", "ms-1", {"dispute_id": "disp-1", "reason": "原始数据归属待核"})
        self.assertEqual(dispute["frozen_disbursements"], ["d-1"])
        states = {
            row[0]: row[1]
            for row in self.connection.execute("SELECT disbursement_id,state FROM disbursements").fetchall()
        }
        self.assertEqual(states, {"d-1": "frozen", "d-2": "scheduled"})
        submit_two_evidence(self.service)
        with self.assertRaises(InvalidState):
            propose(self.service, "accept")
        resolved = self.service.resolve_dispute("gov", "disp-1", "数据归属确认无误")
        self.assertEqual(resolved["unfrozen_disbursements"], ["d-1"])
        states = {
            row[0]: row[1]
            for row in self.connection.execute("SELECT disbursement_id,state FROM disbursements").fetchall()
        }
        self.assertEqual(states, {"d-1": "scheduled", "d-2": "scheduled"})

    def test_dispute_can_target_single_disbursement(self) -> None:
        schedule(self.service, "d-1", "200000")
        schedule(self.service, "d-2", "200000")
        dispute = self.service.raise_dispute(
            "advisor", "ms-1", {"dispute_id": "disp-1", "reason": "单笔拨付依据不足", "disbursement_id": "d-1"}
        )
        self.assertEqual(dispute["frozen_disbursements"], ["d-1"])
        states = {
            row[0]: row[1]
            for row in self.connection.execute("SELECT disbursement_id,state FROM disbursements").fetchall()
        }
        self.assertEqual(states, {"d-1": "frozen", "d-2": "scheduled"})

    def test_second_open_dispute_keeps_freeze_until_all_resolved(self) -> None:
        schedule(self.service, "d-1", "200000")
        self.service.raise_dispute("fund", "ms-1", {"dispute_id": "disp-1", "reason": "问题一"})
        self.service.raise_dispute("research", "ms-1", {"dispute_id": "disp-2", "reason": "问题二"})
        resolved = self.service.resolve_dispute("gov", "disp-1", "问题一排除")
        self.assertEqual(resolved["unfrozen_disbursements"], [])
        state = self.connection.execute("SELECT state FROM disbursements WHERE disbursement_id='d-1'").fetchone()
        self.assertEqual(state[0], "frozen")
        resolved = self.service.resolve_dispute("gov", "disp-2", "问题二排除")
        self.assertEqual(resolved["unfrozen_disbursements"], ["d-1"])

    def test_rule_revision_does_not_rewrite_confirmed_history(self) -> None:
        schedule(self.service)
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        self.service.sign_decision_science("advisor", "dec-1")
        self.service.sign_decision_finance("fund", "dec-1")
        before = self.connection.execute("SELECT * FROM milestone_decisions WHERE decision_id='dec-1'").fetchone()
        self.service.create_rule_set(
            "gov",
            {
                "rule_set_id": "rules-2",
                "name": "转化评审规则 v2",
                "required_evidence_kinds": ["experiment_result", "clinical_assessment", "commercial_analysis"],
                "min_evidence_items": 3,
                "partial_acceptance": {"allowed": False},
            },
        )
        self.service.activate_rule_set("gov", "rules-2")
        after = self.connection.execute("SELECT * FROM milestone_decisions WHERE decision_id='dec-1'").fetchone()
        self.assertEqual(dict(before), dict(after))
        ledger = self.service.project_ledger("secretary", "proj-1")
        milestone = ledger["milestones"][0]
        self.assertEqual(milestone["rule_version"], 1)
        self.assertEqual(milestone["evaluation"]["missing_conditions"], [])
        self.service.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验",
                "experiment_plan": "28 天重复给药",
                "clinical_hypothesis": "安全性窗口",
                "commercial_path": "专利先行",
                "funding_source_id": "src-1",
                "planned_amount_cny": "500000",
            },
        )
        ledger = self.service.project_ledger("secretary", "proj-1")
        newer = ledger["milestones"][1]
        self.assertEqual(newer["rule_version"], 2)
        self.assertIn("缺少证据类型 commercial_analysis", newer["evaluation"]["missing_conditions"])

    def test_secretary_ledger_tracks_evidence_missing_and_next_action(self) -> None:
        schedule(self.service)
        ledger = self.service.project_ledger("secretary", "proj-1")
        entry = ledger["milestones"][0]["disbursements"][0]
        self.assertEqual(entry["next_action"], "submit_evidence")
        self.assertEqual(entry["evidence_source"], "current_submissions")
        self.assertEqual(entry["evidence_used"], [])
        self.assertEqual(len(entry["missing_conditions"]), 3)
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        ledger = self.service.project_ledger("secretary", "proj-1")
        entry = ledger["milestones"][0]["disbursements"][0]
        self.assertEqual(entry["next_action"], "science_signoff")
        self.assertEqual(len(entry["evidence_used"]), 2)
        self.service.sign_decision_science("advisor", "dec-1")
        ledger = self.service.project_ledger("secretary", "proj-1")
        self.assertEqual(ledger["milestones"][0]["disbursements"][0]["next_action"], "finance_signoff")
        self.service.sign_decision_finance("fund", "dec-1")
        ledger = self.service.project_ledger("secretary", "proj-1")
        entry = ledger["milestones"][0]["disbursements"][0]
        self.assertEqual(entry["state"], "released")
        self.assertEqual(entry["evidence_source"], "decision_snapshot")
        self.assertEqual([item["evidence_id"] for item in entry["evidence_used"]], ["ms-1-e-1", "ms-1-e-2"])
        self.assertEqual(entry["missing_conditions"], [])
        self.assertEqual(entry["next_action"], "none")

    def test_idempotent_scheduling_and_decision_replay(self) -> None:
        payload = {"disbursement_id": "d-1", "amount_cny": "400000", "idempotency_key": "d-key-1"}
        first = self.service.schedule_disbursement("fund", "ms-1", payload)
        self.assertEqual(first, self.service.schedule_disbursement("fund", "ms-1", payload))
        with self.assertRaises(Conflict):
            self.service.schedule_disbursement("fund", "ms-1", {**payload, "amount_cny": "410000"})
        submit_two_evidence(self.service)
        decision = {"decision_id": "dec-1", "action": "accept", "note": "结论", "idempotency_key": "dec-key-1"}
        first = self.service.propose_decision("gov", "ms-1", decision)
        self.assertEqual(first, self.service.propose_decision("gov", "ms-1", decision))
        with self.assertRaises(Conflict):
            self.service.propose_decision("gov", "ms-1", {**decision, "note": "另一结论"})

    def test_schedule_capped_by_milestone_budget(self) -> None:
        schedule(self.service, "d-1", "700000")
        with self.assertRaises(Conflict):
            schedule(self.service, "d-2", "300001")
        schedule(self.service, "d-2", "300000")

    def test_release_succeeds_when_source_exactly_covers(self) -> None:
        self.service.create_funding_source("fund", {"source_id": "src-tight", "name": "专项基金", "total_amount_cny": "300000"})
        self.service.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验",
                "experiment_plan": "28 天重复给药",
                "clinical_hypothesis": "安全性窗口",
                "commercial_path": "专利先行",
                "funding_source_id": "src-tight",
                "planned_amount_cny": "300000",
            },
        )
        schedule(self.service, "d-9", "300000", milestone_id="ms-2")
        submit_two_evidence(self.service, milestone_id="ms-2")
        propose(self.service, "accept", decision_id="dec-9", milestone_id="ms-2")
        self.service.sign_decision_science("advisor", "dec-9")
        self.service.adjust_funding_source("fund", "src-tight", "0.01")
        confirmed = self.service.sign_decision_finance("fund", "dec-9")
        self.assertEqual(confirmed["released_total_cny"], "300000.00")
        source = self.service.funding_source("src-tight")
        self.assertEqual(source["available_amount_cny"], "0.01")

    def test_release_blocked_when_source_insufficient(self) -> None:
        self.service.create_funding_source("fund", {"source_id": "src-tight", "name": "专项基金", "total_amount_cny": "300000"})
        self.service.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验",
                "experiment_plan": "28 天重复给药",
                "clinical_hypothesis": "安全性窗口",
                "commercial_path": "专利先行",
                "funding_source_id": "src-tight",
                "planned_amount_cny": "300000",
            },
        )
        schedule(self.service, "d-9", "300000", milestone_id="ms-2")
        submit_two_evidence(self.service, milestone_id="ms-2")
        propose(self.service, "accept", decision_id="dec-9", milestone_id="ms-2")
        self.service.sign_decision_science("advisor", "dec-9")
        self.service.adjust_funding_source("fund", "src-tight", "0.01")
        # 先手工占用余额，模拟基金管理人超配
        self.connection.execute("UPDATE funding_sources SET available_amount_cny='100' WHERE source_id='src-tight'")
        with self.assertRaises(Conflict):
            self.service.sign_decision_finance("fund", "dec-9")
        # 确认失败不留下半个签署：财务复核被回滚，决定仍未结
        pending = self.service.pending_decisions("secretary")["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["science_signed_by"], "advisor")
        self.assertIsNone(pending[0]["finance_signed_by"])
        self.service.adjust_funding_source("fund", "src-tight", "500000")
        confirmed = self.service.sign_decision_finance("fund", "dec-9")
        self.assertEqual(confirmed["state"], "confirmed")

    def test_pending_decision_blocks_second_proposal(self) -> None:
        submit_two_evidence(self.service)
        propose(self.service, "accept")
        with self.assertRaises(Conflict):
            propose(self.service, "suspend", decision_id="dec-2")

    def test_role_permissions_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.schedule_disbursement(
                "research", "ms-1", {"disbursement_id": "d-x", "amount_cny": "1", "idempotency_key": "d-x-key"}
            )
        with self.assertRaises(Forbidden):
            self.service.submit_evidence(
                "secretary",
                "ms-1",
                {"evidence_id": "e-x", "kind": "experiment_result", "summary": "x", "content_sha256": "a" * 64},
            )
        with self.assertRaises(Forbidden):
            self.service.project_ledger("research", "proj-1")
        with self.assertRaises(Forbidden):
            self.service.raise_dispute("secretary", "ms-1", {"dispute_id": "disp-x", "reason": "无权"})

    def test_audit_chain_detects_tampering(self) -> None:
        schedule(self.service)
        self.assertTrue(self.service.audit_chain("secretary")["valid"])
        self.connection.execute("UPDATE milestone_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("secretary")["valid"])

    def test_api_smoke(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/decisions/pending")
        self.assertEqual(missing_actor.status, 422)
        created = app.handle(
            "POST",
            "/milestones/ms-1/evidence",
            {"X-Actor-Id": "research"},
            b'{"evidence_id":"e-api","kind":"experiment_result","summary":"API","content_sha256":"' + b"a" * 64 + b'"}',
        )
        self.assertEqual(created.status, 201)
        ledger = app.handle("GET", "/projects/proj-1/ledger", {"X-Actor-Id": "secretary"})
        self.assertEqual(ledger.status, 200)
        unknown = app.handle("GET", "/nope", {"X-Actor-Id": "secretary"})
        self.assertEqual(unknown.status, 404)


class RestartRecoveryTests(unittest.TestCase):
    def test_pending_decision_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "milestone.sqlite3"
            service = TranslationMilestoneService(connect(database), make_clock())
            boot(service)
            schedule(service)
            submit_two_evidence(service)
            propose(service, "partial_accept", accept_percent="50")
            service.sign_decision_science("advisor", "dec-1")
            service.connection.close()

            restarted = TranslationMilestoneService(connect(database), make_clock())
            pending = restarted.pending_decisions("secretary")["pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["decision_id"], "dec-1")
            self.assertEqual(pending[0]["science_signed_by"], "advisor")
            self.assertIsNone(pending[0]["finance_signed_by"])
            confirmed = restarted.sign_decision_finance("fund", "dec-1")
            self.assertEqual(confirmed["state"], "confirmed")
            self.assertEqual(confirmed["released_total_cny"], "200000.00")
            self.assertEqual(restarted.pending_decisions("secretary")["pending"], [])
            restarted.connection.close()

            again = TranslationMilestoneService(connect(database), make_clock())
            ledger = again.project_ledger("secretary", "proj-1")
            entry = ledger["milestones"][0]["disbursements"][0]
            self.assertEqual(entry["state"], "partially_released")
            self.assertEqual(entry["evidence_source"], "decision_snapshot")
            again.connection.close()


if __name__ == "__main__":
    unittest.main()
