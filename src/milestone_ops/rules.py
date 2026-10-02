"""转化评审规则求值、拨付拆分与下一步动作的确定性计算。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evaluate_evidence(
    rule_content: Mapping[str, object],
    evidence_items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """按签署时钉住的规则版本核对证据，给出尚缺条件清单。"""
    required_kinds = [str(kind) for kind in rule_content["required_evidence_kinds"]]  # type: ignore[index]
    minimum = int(rule_content["min_evidence_items"])  # type: ignore[arg-type]
    counts = {kind: 0 for kind in required_kinds}
    total = 0
    for item in evidence_items:
        total += 1
        kind = str(item["kind"])
        if kind in counts:
            counts[kind] += 1
    missing: list[str] = []
    for kind in required_kinds:
        if counts[kind] == 0:
            missing.append(f"缺少证据类型 {kind}")
    if total < minimum:
        missing.append(f"证据数量不足：需要 {minimum} 条，当前 {total} 条")
    return {
        "satisfied": not missing,
        "missing_conditions": missing,
        "provided_counts": counts,
        "evidence_items": total,
        "min_evidence_items": minimum,
    }


def partial_release(amount: Decimal, percent: Decimal) -> tuple[Decimal, Decimal]:
    """部分接受时把一笔拨付拆成本次放行额与留存额。"""
    if percent <= ZERO or percent >= HUNDRED:
        raise ValueError("部分接受比例必须在 1 到 99 之间")
    released = quantize_money(amount * percent / HUNDRED)
    return released, quantize_money(amount - released)


def next_action(
    *,
    milestone_state: str,
    pending_decision: Mapping[str, object] | None,
    open_disputes: int,
) -> str:
    """由节点状态机推导项目秘书下一次可执行动作。"""
    if open_disputes > 0:
        return "resolve_dispute"
    if milestone_state == "submitted":
        if pending_decision is None:
            return "propose_decision"
        if pending_decision["science_signed_by"] is None:
            return "science_signoff"
        if pending_decision["finance_signed_by"] is None:
            return "finance_signoff"
        return "confirm_decision"
    if milestone_state in ("signed", "returned", "partially_accepted"):
        return "submit_evidence"
    if milestone_state == "suspended":
        return "resume_milestone"
    return "none"
