"""转化里程碑、双签决定、分期拨付与争议冻结的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    DecisionDraft,
    DisbursementDraft,
    DisputeDraft,
    EvidenceDraft,
    FundingSourceDraft,
    MilestoneContract,
    ProjectDraft,
    RuleSetDraft,
    decimal_value,
)
from .rules import (
    canonical_json,
    decimal_text,
    digest,
    evaluate_evidence,
    next_action,
    partial_release,
    quantize_money,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "research_lead": {"evidence.submit", "dispute.raise"},
    "advisor": {"decision.science", "dispute.raise"},
    "fund_manager": {"decision.finance", "fund.write", "disbursement.schedule", "dispute.raise"},
    "governance": {
        "rules.write",
        "rules.activate",
        "project.write",
        "milestone.sign",
        "milestone.resume",
        "decision.propose",
        "dispute.resolve",
        "ledger.read",
    },
    "secretary": {"ledger.read", "audit.read"},
}

# 允许补交证据并进入评审的节点状态
EVIDENCE_SUBMITTABLE = ("signed", "returned", "partially_accepted")
# 允许安排新拨付的节点状态
DISBURSEMENT_SCHEDULABLE = ("signed", "submitted", "returned", "partially_accepted")
# 争议可冻结的拨付状态
FREEZABLE_STATES = ("scheduled", "held")


class TranslationMilestoneService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM milestone_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM milestone_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO milestone_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM milestone_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(row["response_json"])

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO milestone_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ---- 规则版本 ----

    def create_rule_set(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rules.write")
        draft = RuleSetDraft.from_dict(raw)
        definition = canonical_json(draft.content())
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                latest = self.connection.execute("SELECT max(version) AS v FROM rule_sets").fetchone()["v"]
                version = 1 if latest is None else int(latest) + 1
                self.connection.execute(
                    "INSERT INTO rule_sets(rule_set_id,version,name,content_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (draft.rule_set_id, version, draft.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit(
                    "rule_set",
                    draft.rule_set_id,
                    "rules.created",
                    actor_id,
                    {"version": version, "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则集编号已经存在") from exc
        return {"rule_set_id": draft.rule_set_id, "version": version, "state": "draft", "sha256": content_sha256}

    def activate_rule_set(self, actor_id: str, rule_set_id: str) -> dict[str, Any]:
        self._require(actor_id, "rules.activate")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM rule_sets WHERE rule_set_id=?", (rule_set_id,)
            ).fetchone()
            if row is None:
                raise NotFound("规则集不存在")
            if row["state"] != "draft":
                raise InvalidState("只有草稿规则集可以生效")
            # 旧版本仅被取代，已签署节点仍钉住各自版本，历史决定不可改写
            self.connection.execute(
                "UPDATE rule_sets SET state='superseded',revision=revision+1 WHERE state='active'"
            )
            self.connection.execute(
                "UPDATE rule_sets SET state='active',activated_at=?,revision=revision+1 WHERE rule_set_id=?",
                (self._now(), rule_set_id),
            )
            self._audit("rule_set", rule_set_id, "rules.activated", actor_id, {"version": row["version"]})
        return {"rule_set_id": rule_set_id, "version": row["version"], "state": "active"}

    def active_rule_set(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM rule_sets WHERE state='active'").fetchone()
        return None if row is None else dict(row)

    def _rule_set_row(self, rule_set_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rule_sets WHERE rule_set_id=?", (rule_set_id,)
        ).fetchone()
        if row is None:
            raise NotFound("规则集不存在")
        return row

    # ---- 资金来源与孵化项目 ----

    def create_funding_source(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "fund.write")
        draft = FundingSourceDraft.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO funding_sources(source_id,name,total_amount_cny,available_amount_cny,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        draft.source_id,
                        draft.name,
                        decimal_text(draft.total_amount_cny),
                        decimal_text(draft.total_amount_cny),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("funding_source", draft.source_id, "fund.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("资金来源编号已经存在") from exc
        return self.funding_source(draft.source_id)

    def adjust_funding_source(self, actor_id: str, source_id: str, additional_amount_cny: object) -> dict[str, Any]:
        self._require(actor_id, "fund.write")
        additional = quantize_money(decimal_value(additional_amount_cny, "additional_amount_cny", minimum=Decimal("0.01")))
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM funding_sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is None:
                raise NotFound("资金来源不存在")
            total = quantize_money(Decimal(row["total_amount_cny"]) + additional)
            available = quantize_money(Decimal(row["available_amount_cny"]) + additional)
            cursor = self.connection.execute(
                "UPDATE funding_sources SET total_amount_cny=?,available_amount_cny=?,revision=revision+1 "
                "WHERE source_id=? AND revision=?",
                (decimal_text(total), decimal_text(available), source_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise Conflict("资金来源版本冲突")
            self._audit(
                "funding_source",
                source_id,
                "fund.adjusted",
                actor_id,
                {"additional_amount_cny": decimal_text(additional)},
            )
        return self.funding_source(source_id)

    def funding_source(self, source_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM funding_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资金来源不存在")
        return dict(row)

    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        draft = ProjectDraft.from_dict(raw)
        lead = self._user(draft.research_lead_id)
        if lead["role"] != "research_lead":
            raise ValidationFailed("research_lead_id 必须指向科研团队负责人")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO incubation_projects(project_id,name,research_lead_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (draft.project_id, draft.name, draft.research_lead_id, actor_id, self._now()),
                )
                self._audit("project", draft.project_id, "project.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号已经存在") from exc
        return {"project_id": draft.project_id, "state": "incubating"}

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM incubation_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("孵化项目不存在")
        return row

    # ---- 里程碑签署：五要素绑定签署时规则版本 ----

    def sign_milestone(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "milestone.sign")
        contract = MilestoneContract.from_dict(raw)
        project = self._project_row(contract.project_id)
        if project["state"] != "incubating":
            raise InvalidState("项目不在孵化状态，不能签署新节点")
        self.funding_source(contract.funding_source_id)
        rule = self.active_rule_set()
        if rule is None:
            raise InvalidState("没有已生效的规则版本，不能签署里程碑")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO milestones(milestone_id,project_id,sequence,title,experiment_plan,"
                    "clinical_hypothesis,commercial_path,funding_source_id,planned_amount_cny,"
                    "rule_set_id,rule_version,signed_by,signed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        contract.milestone_id,
                        contract.project_id,
                        contract.sequence,
                        contract.title,
                        contract.experiment_plan,
                        contract.clinical_hypothesis,
                        contract.commercial_path,
                        contract.funding_source_id,
                        decimal_text(contract.planned_amount_cny),
                        rule["rule_set_id"],
                        rule["version"],
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "milestone",
                    contract.milestone_id,
                    "milestone.signed",
                    actor_id,
                    {
                        "project_id": contract.project_id,
                        "rule_set_id": rule["rule_set_id"],
                        "rule_version": rule["version"],
                        "planned_amount_cny": decimal_text(contract.planned_amount_cny),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("里程碑编号或项目内序号冲突") from exc
        return {
            "milestone_id": contract.milestone_id,
            "project_id": contract.project_id,
            "rule_set_id": rule["rule_set_id"],
            "rule_version": rule["version"],
            "state": "signed",
            "revision": 1,
        }

    def _milestone_row(self, milestone_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,)
        ).fetchone()
        if row is None:
            raise NotFound("里程碑不存在")
        return row

    # ---- 证据提交 ----

    def submit_evidence(self, actor_id: str, milestone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.submit")
        milestone = self._milestone_row(milestone_id)
        if milestone["state"] not in EVIDENCE_SUBMITTABLE + ("submitted",):
            raise InvalidState("当前节点状态不能提交证据")
        draft = EvidenceDraft.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_items(evidence_id,milestone_id,kind,summary,content_sha256,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        draft.evidence_id,
                        milestone_id,
                        draft.kind,
                        draft.summary,
                        draft.content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                if milestone["state"] != "submitted":
                    self.connection.execute(
                        "UPDATE milestones SET state='submitted',revision=revision+1 WHERE milestone_id=?",
                        (milestone_id,),
                    )
                self._audit(
                    "milestone",
                    milestone_id,
                    "evidence.submitted",
                    actor_id,
                    {"evidence_id": draft.evidence_id, "kind": draft.kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号已经存在") from exc
        return {"evidence_id": draft.evidence_id, "milestone_id": milestone_id, "milestone_state": "submitted"}

    # ---- 分期拨付 ----

    def schedule_disbursement(self, actor_id: str, milestone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "disbursement.schedule")
        draft = DisbursementDraft.from_dict(raw)
        request_digest = digest({"milestone_id": milestone_id, "payload": dict(raw)})
        stored = self._idempotent_response("disbursement", draft.idempotency_key, request_digest)
        if stored is not None:
            return stored
        milestone = self._milestone_row(milestone_id)
        if milestone["state"] not in DISBURSEMENT_SCHEDULABLE:
            raise InvalidState("当前节点状态不能安排拨付")
        if self._open_dispute_count(milestone_id) > 0:
            raise InvalidState("节点存在未解决争议，不能安排新拨付")
        committed = Decimal("0")
        for row in self.connection.execute(
            "SELECT amount_cny FROM disbursements WHERE milestone_id=?", (milestone_id,)
        ).fetchall():
            committed += Decimal(row["amount_cny"])
        if committed + draft.amount_cny > Decimal(milestone["planned_amount_cny"]):
            raise Conflict("拨付总额超过节点预算")
        response = {
            "disbursement_id": draft.disbursement_id,
            "milestone_id": milestone_id,
            "funding_source_id": milestone["funding_source_id"],
            "amount_cny": decimal_text(draft.amount_cny),
            "state": "scheduled",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO disbursements(disbursement_id,milestone_id,funding_source_id,amount_cny,"
                    "idempotency_key,scheduled_by,scheduled_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        draft.disbursement_id,
                        milestone_id,
                        milestone["funding_source_id"],
                        decimal_text(draft.amount_cny),
                        draft.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO milestone_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('disbursement',?,?,?,?)",
                    (draft.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "disbursement",
                    draft.disbursement_id,
                    "disbursement.scheduled",
                    actor_id,
                    {"milestone_id": milestone_id, "amount_cny": decimal_text(draft.amount_cny)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("拨付编号或幂等键冲突") from exc
        return response

    # ---- 节点决定：科学验收与财务复核双签 ----

    def propose_decision(self, actor_id: str, milestone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "decision.propose")
        draft = DecisionDraft.from_dict(raw)
        request_digest = digest({"milestone_id": milestone_id, "payload": dict(raw)})
        stored = self._idempotent_response("decision", draft.idempotency_key, request_digest)
        if stored is not None:
            return stored
        milestone = self._milestone_row(milestone_id)
        if milestone["state"] != "submitted":
            raise InvalidState("节点不在评审状态，不能提交决定")
        if self._open_dispute_count(milestone_id) > 0:
            raise InvalidState("节点存在未解决争议，不能提交决定")
        pending = self.connection.execute(
            "SELECT 1 FROM milestone_decisions WHERE milestone_id=? AND state='pending' LIMIT 1",
            (milestone_id,),
        ).fetchone()
        if pending is not None:
            raise Conflict("节点已有未结决定")
        rule = self._rule_set_row(milestone["rule_set_id"])
        if draft.action == "partial_accept":
            partial = json.loads(rule["content_json"])["partial_acceptance"]
            if not partial["allowed"]:
                raise ValidationFailed("签署时的规则版本不允许部分接受")
            assert draft.accept_percent is not None
            if draft.accept_percent < Decimal(partial["min_percent"]) or draft.accept_percent > Decimal(partial["max_percent"]):
                raise ValidationFailed("部分接受比例超出签署时规则版本允许的范围")
        response = {
            "decision_id": draft.decision_id,
            "milestone_id": milestone_id,
            "action": draft.action,
            "state": "pending",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO milestone_decisions(decision_id,milestone_id,action,accept_percent,note,"
                    "idempotency_key,proposed_by,proposed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        draft.decision_id,
                        milestone_id,
                        draft.action,
                        None if draft.accept_percent is None else decimal_text(draft.accept_percent),
                        draft.note,
                        draft.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO milestone_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('decision',?,?,?,?)",
                    (draft.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "decision",
                    draft.decision_id,
                    "decision.proposed",
                    actor_id,
                    {"milestone_id": milestone_id, "action": draft.action},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("决定编号或幂等键冲突") from exc
        return response

    def sign_decision_science(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        self._require(actor_id, "decision.science")
        return self._sign_decision(actor_id, decision_id, "science")

    def sign_decision_finance(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        self._require(actor_id, "decision.finance")
        return self._sign_decision(actor_id, decision_id, "finance")

    def _sign_decision(self, actor_id: str, decision_id: str, side: str) -> dict[str, Any]:
        with transaction(self.connection, immediate=True):
            row = self._decision_row(decision_id)
            if row["state"] != "pending":
                raise InvalidState("决定已经结案")
            own_column = "science_signed_by" if side == "science" else "finance_signed_by"
            other_signer = row["finance_signed_by"] if side == "science" else row["science_signed_by"]
            if other_signer == actor_id:
                raise Forbidden("科学验收与财务复核必须由不同授权人完成")
            if row[own_column] is not None:
                raise Conflict("该侧复核已经完成")
            now = self._now()
            self.connection.execute(
                f"UPDATE milestone_decisions SET {own_column}=?,{side}_signed_at=? WHERE decision_id=?",
                (actor_id, now, decision_id),
            )
            self._audit("decision", decision_id, f"decision.{side}_signed", actor_id, {})
            updated = self._decision_row(decision_id)
            confirmed: dict[str, Any] | None = None
            if updated["science_signed_by"] is not None and updated["finance_signed_by"] is not None:
                if self._open_dispute_count(updated["milestone_id"]) > 0:
                    raise InvalidState("节点存在未解决争议，决定不能确认")
                confirmed = self._confirm_decision(updated)
        result: dict[str, Any] = {
            "decision_id": decision_id,
            "milestone_id": updated["milestone_id"],
            "state": "pending" if confirmed is None else "confirmed",
            "science_signed_by": updated["science_signed_by"],
            "finance_signed_by": updated["finance_signed_by"],
        }
        if confirmed is not None:
            result.update(confirmed)
        return result

    def _confirm_decision(self, decision: sqlite3.Row) -> dict[str, Any]:
        """双签齐备后在同一事务内固化决定并联动拨付。"""
        milestone = self._milestone_row(decision["milestone_id"])
        rule = self._rule_set_row(milestone["rule_set_id"])
        evidence_rows = self.connection.execute(
            "SELECT * FROM evidence_items WHERE milestone_id=? ORDER BY submitted_at,evidence_id",
            (milestone["milestone_id"],),
        ).fetchall()
        evaluation = evaluate_evidence(json.loads(rule["content_json"]), evidence_rows)
        snapshot = [
            {
                "evidence_id": item["evidence_id"],
                "kind": item["kind"],
                "summary": item["summary"],
                "content_sha256": item["content_sha256"],
                "submitted_by": item["submitted_by"],
                "submitted_at": item["submitted_at"],
            }
            for item in evidence_rows
        ]
        action = decision["action"]
        now = self._now()
        milestone_state = {
            "accept": "accepted",
            "partial_accept": "partially_accepted",
            "return_for_evidence": "returned",
            "suspend": "suspended",
        }[action]
        released_total = Decimal("0")
        if action in ("accept", "partial_accept"):
            percent = None if action == "accept" else Decimal(decision["accept_percent"])
            released_total = self._release_scheduled(milestone, decision["decision_id"], percent, now)
        elif action == "suspend":
            self.connection.execute(
                "UPDATE disbursements SET state='held',revision=revision+1 "
                "WHERE milestone_id=? AND state='scheduled'",
                (milestone["milestone_id"],),
            )
        self.connection.execute(
            "UPDATE milestones SET state=?,revision=revision+1 WHERE milestone_id=?",
            (milestone_state, milestone["milestone_id"]),
        )
        self.connection.execute(
            "UPDATE milestone_decisions SET state='confirmed',confirmed_at=?,"
            "evidence_snapshot_json=?,missing_conditions_json=? WHERE decision_id=?",
            (
                now,
                canonical_json(snapshot),
                canonical_json(evaluation["missing_conditions"]),
                decision["decision_id"],
            ),
        )
        self._audit(
            "decision",
            decision["decision_id"],
            "decision.confirmed",
            decision["proposed_by"],
            {
                "milestone_id": milestone["milestone_id"],
                "action": action,
                "milestone_state": milestone_state,
                "released_total_cny": decimal_text(quantize_money(released_total)),
                "missing_conditions": evaluation["missing_conditions"],
            },
        )
        return {
            "milestone_state": milestone_state,
            "released_total_cny": decimal_text(quantize_money(released_total)),
            "missing_conditions": evaluation["missing_conditions"],
        }

    def _release_scheduled(
        self,
        milestone: sqlite3.Row,
        decision_id: str,
        percent: Decimal | None,
        now: str,
    ) -> Decimal:
        rows = self.connection.execute(
            "SELECT * FROM disbursements WHERE milestone_id=? AND state='scheduled' "
            "ORDER BY scheduled_at,disbursement_id",
            (milestone["milestone_id"],),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            amount = Decimal(row["amount_cny"])
            released = amount if percent is None else partial_release(amount, percent)[0]
            source = self.connection.execute(
                "SELECT * FROM funding_sources WHERE source_id=?", (row["funding_source_id"],)
            ).fetchone()
            available = Decimal(source["available_amount_cny"])
            if available < released:
                raise Conflict("资金来源可用余额不足，拨付不能放行")
            cursor = self.connection.execute(
                "UPDATE funding_sources SET available_amount_cny=?,released_amount_cny=?,revision=revision+1 "
                "WHERE source_id=? AND revision=?",
                (
                    decimal_text(quantize_money(available - released)),
                    decimal_text(quantize_money(Decimal(source["released_amount_cny"]) + released)),
                    row["funding_source_id"],
                    source["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise Conflict("资金来源版本冲突")
            state = "released" if released == amount else "partially_released"
            self.connection.execute(
                "UPDATE disbursements SET state=?,released_amount_cny=?,decision_id=?,released_at=?,"
                "revision=revision+1 WHERE disbursement_id=?",
                (state, decimal_text(released), decision_id, now, row["disbursement_id"]),
            )
            total += released
        return total

    def _decision_row(self, decision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM milestone_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise NotFound("决定不存在")
        return row

    def _open_dispute_count(self, milestone_id: str) -> int:
        row = self.connection.execute(
            "SELECT count(*) AS c FROM disputes WHERE milestone_id=? AND state='open'",
            (milestone_id,),
        ).fetchone()
        return int(row["c"])

    def pending_decisions(self, actor_id: str) -> dict[str, Any]:
        """未结决定清单：服务重启后从 SQLite 原样恢复。"""
        self._require(actor_id, "ledger.read")
        rows = self.connection.execute(
            "SELECT * FROM milestone_decisions WHERE state='pending' ORDER BY proposed_at,decision_id"
        ).fetchall()
        return {
            "pending": [
                {
                    "decision_id": row["decision_id"],
                    "milestone_id": row["milestone_id"],
                    "action": row["action"],
                    "accept_percent": row["accept_percent"],
                    "proposed_by": row["proposed_by"],
                    "proposed_at": row["proposed_at"],
                    "science_signed_by": row["science_signed_by"],
                    "finance_signed_by": row["finance_signed_by"],
                }
                for row in rows
            ]
        }

    # ---- 争议：只冻结相关拨付 ----

    def raise_dispute(self, actor_id: str, milestone_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dispute.raise")
        milestone = self._milestone_row(milestone_id)
        draft = DisputeDraft.from_dict(raw)
        if draft.disbursement_id is not None:
            target = self.connection.execute(
                "SELECT 1 FROM disbursements WHERE disbursement_id=? AND milestone_id=?",
                (draft.disbursement_id, milestone_id),
            ).fetchone()
            if target is None:
                raise NotFound("拨付不存在或不属于该节点")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO disputes(dispute_id,milestone_id,disbursement_id,reason,raised_by,raised_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    draft.dispute_id,
                    milestone_id,
                    draft.disbursement_id,
                    draft.reason,
                    actor_id,
                    self._now(),
                ),
            )
            if draft.disbursement_id is None:
                frozen = self._freeze_disbursements(
                    "milestone_id=? AND state IN ('scheduled','held')",
                    (milestone_id,),
                )
            else:
                frozen = self._freeze_disbursements(
                    "disbursement_id=? AND state IN ('scheduled','held')",
                    (draft.disbursement_id,),
                )
            self._audit(
                "dispute",
                draft.dispute_id,
                "dispute.raised",
                actor_id,
                {"milestone_id": milestone_id, "frozen_disbursements": frozen},
            )
        return {
            "dispute_id": draft.dispute_id,
            "milestone_id": milestone_id,
            "state": "open",
            "frozen_disbursements": frozen,
        }

    def _freeze_disbursements(self, where: str, params: tuple[Any, ...]) -> list[str]:
        rows = self.connection.execute(
            f"SELECT disbursement_id FROM disbursements WHERE {where} ORDER BY disbursement_id",
            params,
        ).fetchall()
        frozen = [row["disbursement_id"] for row in rows]
        if frozen:
            placeholders = ",".join("?" for _ in frozen)
            self.connection.execute(
                f"UPDATE disbursements SET state_before_freeze=state,state='frozen',revision=revision+1 "
                f"WHERE disbursement_id IN ({placeholders})",
                tuple(frozen),
            )
        return frozen

    def resolve_dispute(self, actor_id: str, dispute_id: str, resolution: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.resolve")
        if not resolution.strip():
            raise ValidationFailed("resolution 不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("争议不存在")
            if row["state"] != "open":
                raise InvalidState("争议已经结案")
            self.connection.execute(
                "UPDATE disputes SET state='resolved',resolved_by=?,resolved_at=?,resolution=? WHERE dispute_id=?",
                (actor_id, self._now(), resolution.strip(), dispute_id),
            )
            unfrozen: list[str] = []
            if self._open_dispute_count(row["milestone_id"]) == 0:
                restored = self.connection.execute(
                    "SELECT disbursement_id FROM disbursements WHERE milestone_id=? AND state='frozen' "
                    "ORDER BY disbursement_id",
                    (row["milestone_id"],),
                ).fetchall()
                unfrozen = [item["disbursement_id"] for item in restored]
                if unfrozen:
                    placeholders = ",".join("?" for _ in unfrozen)
                    self.connection.execute(
                        f"UPDATE disbursements SET state=state_before_freeze,state_before_freeze=NULL,"
                        f"revision=revision+1 WHERE disbursement_id IN ({placeholders})",
                        tuple(unfrozen),
                    )
            self._audit(
                "dispute",
                dispute_id,
                "dispute.resolved",
                actor_id,
                {"resolution": resolution.strip(), "unfrozen_disbursements": unfrozen},
            )
        return {"dispute_id": dispute_id, "state": "resolved", "unfrozen_disbursements": unfrozen}

    # ---- 暂停恢复 ----

    def resume_milestone(self, actor_id: str, milestone_id: str) -> dict[str, Any]:
        self._require(actor_id, "milestone.resume")
        with transaction(self.connection, immediate=True):
            milestone = self._milestone_row(milestone_id)
            if milestone["state"] != "suspended":
                raise InvalidState("只有已暂停节点可以恢复")
            self.connection.execute(
                "UPDATE milestones SET state='signed',revision=revision+1 WHERE milestone_id=?",
                (milestone_id,),
            )
            self.connection.execute(
                "UPDATE disbursements SET state='scheduled',revision=revision+1 "
                "WHERE milestone_id=? AND state='held'",
                (milestone_id,),
            )
            self._audit("milestone", milestone_id, "milestone.resumed", actor_id, {})
        return {"milestone_id": milestone_id, "state": "signed"}

    # ---- 项目秘书视图 ----

    def project_ledger(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """每笔拨付采用的证据、尚缺条件和下一次可执行动作。"""
        self._require(actor_id, "ledger.read")
        project = self._project_row(project_id)
        milestones = self.connection.execute(
            "SELECT * FROM milestones WHERE project_id=? ORDER BY sequence,milestone_id",
            (project_id,),
        ).fetchall()
        milestone_views = []
        for milestone in milestones:
            rule = self._rule_set_row(milestone["rule_set_id"])
            evidence_rows = self.connection.execute(
                "SELECT * FROM evidence_items WHERE milestone_id=? ORDER BY submitted_at,evidence_id",
                (milestone["milestone_id"],),
            ).fetchall()
            evaluation = evaluate_evidence(json.loads(rule["content_json"]), evidence_rows)
            pending = self.connection.execute(
                "SELECT * FROM milestone_decisions WHERE milestone_id=? AND state='pending' "
                "ORDER BY proposed_at DESC LIMIT 1",
                (milestone["milestone_id"],),
            ).fetchone()
            open_disputes = self._open_dispute_count(milestone["milestone_id"])
            action = next_action(
                milestone_state=milestone["state"],
                pending_decision=None if pending is None else dict(pending),
                open_disputes=open_disputes,
            )
            latest_confirmed = self.connection.execute(
                "SELECT * FROM milestone_decisions WHERE milestone_id=? AND state='confirmed' "
                "ORDER BY confirmed_at DESC,decision_id DESC LIMIT 1",
                (milestone["milestone_id"],),
            ).fetchone()
            fallback_snapshot = (
                None
                if latest_confirmed is None
                else json.loads(latest_confirmed["evidence_snapshot_json"])
            )
            current_evidence = [
                {
                    "evidence_id": item["evidence_id"],
                    "kind": item["kind"],
                    "summary": item["summary"],
                    "content_sha256": item["content_sha256"],
                    "submitted_by": item["submitted_by"],
                    "submitted_at": item["submitted_at"],
                }
                for item in evidence_rows
            ]
            disbursement_views = []
            for row in self.connection.execute(
                "SELECT * FROM disbursements WHERE milestone_id=? ORDER BY scheduled_at,disbursement_id",
                (milestone["milestone_id"],),
            ).fetchall():
                evidence_used, evidence_source = self._evidence_for_disbursement(
                    row, fallback_snapshot, current_evidence
                )
                disbursement_views.append(
                    {
                        "disbursement_id": row["disbursement_id"],
                        "funding_source_id": row["funding_source_id"],
                        "amount_cny": row["amount_cny"],
                        "released_amount_cny": row["released_amount_cny"],
                        "state": row["state"],
                        "decision_id": row["decision_id"],
                        "evidence_used": evidence_used,
                        "evidence_source": evidence_source,
                        "missing_conditions": evaluation["missing_conditions"],
                        "next_action": action,
                    }
                )
            milestone_views.append(
                {
                    "milestone_id": milestone["milestone_id"],
                    "sequence": milestone["sequence"],
                    "title": milestone["title"],
                    "state": milestone["state"],
                    "rule_set_id": milestone["rule_set_id"],
                    "rule_version": milestone["rule_version"],
                    "planned_amount_cny": milestone["planned_amount_cny"],
                    "evaluation": evaluation,
                    "open_disputes": open_disputes,
                    "next_action": action,
                    "disbursements": disbursement_views,
                }
            )
        return {
            "project_id": project_id,
            "project_state": project["state"],
            "milestones": milestone_views,
        }

    def _evidence_for_disbursement(
        self,
        disbursement: sqlite3.Row,
        fallback_snapshot: list[dict[str, Any]] | None,
        current_evidence: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        if disbursement["decision_id"] is not None:
            decision = self._decision_row(disbursement["decision_id"])
            return json.loads(decision["evidence_snapshot_json"]), "decision_snapshot"
        if fallback_snapshot is not None:
            return fallback_snapshot, "decision_snapshot"
        return current_evidence, "current_submissions"

    # ---- 审计 ----

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM milestone_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
