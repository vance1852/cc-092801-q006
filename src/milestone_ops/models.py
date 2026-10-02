"""转化里程碑与拨付联动领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed
from .rules import decimal_text


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
EVIDENCE_KINDS = {"experiment_result", "clinical_assessment", "commercial_analysis", "financial_document"}
DECISION_ACTIONS = {"accept", "partial_accept", "return_for_evidence", "suspend"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制摘要")
    return result


@dataclass(frozen=True, slots=True)
class RuleSetDraft:
    rule_set_id: str
    name: str
    required_evidence_kinds: tuple[str, ...]
    min_evidence_items: int
    partial_allowed: bool
    partial_min_percent: Decimal
    partial_max_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleSetDraft":
        kinds_raw = raw.get("required_evidence_kinds")
        if not isinstance(kinds_raw, (list, tuple)) or not kinds_raw:
            raise ValidationFailed("required_evidence_kinds 必须是非空数组")
        kinds: list[str] = []
        for item in kinds_raw:
            kind = required_text(item, "required_evidence_kinds 元素", 32)
            if kind not in EVIDENCE_KINDS:
                raise ValidationFailed(f"required_evidence_kinds 包含未知证据类型 {kind}")
            if kind in kinds:
                raise ValidationFailed("required_evidence_kinds 不能重复")
            kinds.append(kind)
        partial_raw = raw.get("partial_acceptance", {})
        if not isinstance(partial_raw, Mapping):
            raise ValidationFailed("partial_acceptance 必须是对象")
        allowed = partial_raw.get("allowed", False)
        if not isinstance(allowed, bool):
            raise ValidationFailed("partial_acceptance.allowed 必须是布尔值")
        min_percent = decimal_value(
            partial_raw.get("min_percent", 1),
            "partial_acceptance.min_percent",
            minimum=Decimal("1"),
            maximum=Decimal("99"),
        )
        max_percent = decimal_value(
            partial_raw.get("max_percent", 99),
            "partial_acceptance.max_percent",
            minimum=Decimal("1"),
            maximum=Decimal("99"),
        )
        if min_percent > max_percent:
            raise ValidationFailed("部分接受比例下限不能高于上限")
        return cls(
            rule_set_id=identifier(raw.get("rule_set_id"), "rule_set_id"),
            name=required_text(raw.get("name"), "name"),
            required_evidence_kinds=tuple(kinds),
            min_evidence_items=positive_integer(raw.get("min_evidence_items"), "min_evidence_items"),
            partial_allowed=allowed,
            partial_min_percent=min_percent,
            partial_max_percent=max_percent,
        )

    def content(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "min_evidence_items": self.min_evidence_items,
            "partial_acceptance": {
                "allowed": self.partial_allowed,
                "min_percent": decimal_text(self.partial_min_percent),
                "max_percent": decimal_text(self.partial_max_percent),
            },
        }


@dataclass(frozen=True, slots=True)
class FundingSourceDraft:
    source_id: str
    name: str
    total_amount_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FundingSourceDraft":
        return cls(
            source_id=identifier(raw.get("source_id"), "source_id"),
            name=required_text(raw.get("name"), "name"),
            total_amount_cny=decimal_value(
                raw.get("total_amount_cny"), "total_amount_cny", minimum=Decimal("0.01")
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectDraft:
    project_id: str
    name: str
    research_lead_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectDraft":
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            name=required_text(raw.get("name"), "name"),
            research_lead_id=identifier(raw.get("research_lead_id"), "research_lead_id"),
        )


@dataclass(frozen=True, slots=True)
class MilestoneContract:
    milestone_id: str
    project_id: str
    sequence: int
    title: str
    experiment_plan: str
    clinical_hypothesis: str
    commercial_path: str
    funding_source_id: str
    planned_amount_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MilestoneContract":
        return cls(
            milestone_id=identifier(raw.get("milestone_id"), "milestone_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            sequence=positive_integer(raw.get("sequence"), "sequence"),
            title=required_text(raw.get("title"), "title"),
            experiment_plan=required_text(raw.get("experiment_plan"), "experiment_plan", 2000),
            clinical_hypothesis=required_text(raw.get("clinical_hypothesis"), "clinical_hypothesis", 2000),
            commercial_path=required_text(raw.get("commercial_path"), "commercial_path", 2000),
            funding_source_id=identifier(raw.get("funding_source_id"), "funding_source_id"),
            planned_amount_cny=decimal_value(
                raw.get("planned_amount_cny"), "planned_amount_cny", minimum=Decimal("0.01")
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    evidence_id: str
    kind: str
    summary: str
    content_sha256: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceDraft":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in EVIDENCE_KINDS:
            raise ValidationFailed("kind 不是受支持的证据类型")
        return cls(
            evidence_id=identifier(raw.get("evidence_id"), "evidence_id"),
            kind=kind,
            summary=required_text(raw.get("summary"), "summary", 1000),
            content_sha256=sha256_text(raw.get("content_sha256"), "content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    decision_id: str
    action: str
    accept_percent: Decimal | None
    note: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DecisionDraft":
        action = required_text(raw.get("action"), "action", 32)
        if action not in DECISION_ACTIONS:
            raise ValidationFailed("action 必须是 accept、partial_accept、return_for_evidence 或 suspend")
        percent_raw = raw.get("accept_percent")
        if action == "partial_accept":
            if percent_raw is None:
                raise ValidationFailed("部分接受必须提供 accept_percent")
            percent: Decimal | None = decimal_value(
                percent_raw, "accept_percent", minimum=Decimal("1"), maximum=Decimal("99")
            )
        else:
            if percent_raw is not None:
                raise ValidationFailed("只有部分接受可以携带 accept_percent")
            percent = None
        return cls(
            decision_id=identifier(raw.get("decision_id"), "decision_id"),
            action=action,
            accept_percent=percent,
            note=required_text(raw.get("note"), "note", 1000),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class DisbursementDraft:
    disbursement_id: str
    amount_cny: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DisbursementDraft":
        return cls(
            disbursement_id=identifier(raw.get("disbursement_id"), "disbursement_id"),
            amount_cny=decimal_value(raw.get("amount_cny"), "amount_cny", minimum=Decimal("0.01")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class DisputeDraft:
    dispute_id: str
    reason: str
    disbursement_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DisputeDraft":
        disbursement_id = raw.get("disbursement_id")
        return cls(
            dispute_id=identifier(raw.get("dispute_id"), "dispute_id"),
            reason=required_text(raw.get("reason"), "reason", 1000),
            disbursement_id=None if disbursement_id is None else identifier(disbursement_id, "disbursement_id"),
        )
