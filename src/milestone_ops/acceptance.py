"""贯通规则版本、里程碑签署、证据、双签决定、拨付、争议与重启恢复的离线验收。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import TranslationMilestoneService
from .storage import connect


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc))


def _bootstrap(service: TranslationMilestoneService) -> None:
    for user_id, role in (
        ("research", "research_lead"),
        ("advisor", "advisor"),
        ("fund", "fund_manager"),
        ("gov", "governance"),
        ("secretary", "secretary"),
    ):
        service.create_user(user_id, user_id, role)
    service.create_rule_set(
        "gov",
        {
            "rule_set_id": "rules-v1",
            "name": "转化评审规则 v1",
            "required_evidence_kinds": ["experiment_result", "clinical_assessment"],
            "min_evidence_items": 2,
            "partial_acceptance": {"allowed": True, "min_percent": "25", "max_percent": "75"},
        },
    )
    service.activate_rule_set("gov", "rules-v1")
    service.create_funding_source("fund", {"source_id": "src-1", "name": "转化基金一期", "total_amount_cny": "5000000"})
    service.create_project("gov", {"project_id": "proj-1", "name": "口服 GLP-1 前体", "research_lead_id": "research"})
    service.sign_milestone(
        "gov",
        {
            "milestone_id": "ms-1",
            "project_id": "proj-1",
            "sequence": 1,
            "title": "体外活性与选择性验证",
            "experiment_plan": "双靶点细胞实验三批重复，IC50 与选择性窗口达标",
            "clinical_hypothesis": "2 型糖尿病肥胖亚组存在未满足口服给药需求",
            "commercial_path": "国内权益自持，海外授权引进方共同开发",
            "funding_source_id": "src-1",
            "planned_amount_cny": "1000000",
        },
    )


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(dir=workspace) as tmp:
        database = Path(tmp) / "milestone.sqlite3"
        service = TranslationMilestoneService(connect(database), _clock())
        _bootstrap(service)
        service.schedule_disbursement(
            "fund", "ms-1", {"disbursement_id": "d-1", "amount_cny": "400000", "idempotency_key": "d-key-1"}
        )
        service.submit_evidence(
            "research",
            "ms-1",
            {
                "evidence_id": "e-1",
                "kind": "experiment_result",
                "summary": "三批重复 IC50 均值 3.2nM，选择性窗口 40 倍",
                "content_sha256": "a" * 64,
            },
        )
        service.submit_evidence(
            "research",
            "ms-1",
            {
                "evidence_id": "e-2",
                "kind": "clinical_assessment",
                "summary": "目标适应症未满足需求访谈纪要",
                "content_sha256": "b" * 64,
            },
        )
        service.propose_decision(
            "gov",
            "ms-1",
            {
                "decision_id": "dec-1",
                "action": "partial_accept",
                "accept_percent": "50",
                "note": "季度会议：成药性达标，商业路径待补海外意向书",
                "idempotency_key": "dec-key-1",
            },
        )
        service.sign_decision_science("advisor", "dec-1")
        service.connection.close()

        # 模拟服务重启：同一数据库文件恢复未结决定后继续财务复核
        restarted = TranslationMilestoneService(connect(database), _clock())
        recovered = restarted.pending_decisions("secretary")["pending"]
        confirmed = restarted.sign_decision_finance("fund", "dec-1")
        restarted.schedule_disbursement(
            "fund", "ms-1", {"disbursement_id": "d-2", "amount_cny": "300000", "idempotency_key": "d-key-2"}
        )
        dispute = restarted.raise_dispute("fund", "ms-1", {"dispute_id": "disp-1", "reason": "海外意向书真实性待核"})
        resolved = restarted.resolve_dispute("gov", "disp-1", "意向书经第三方核验属实，解冻相关拨付")
        restarted.create_rule_set(
            "gov",
            {
                "rule_set_id": "rules-v2",
                "name": "转化评审规则 v2",
                "required_evidence_kinds": ["experiment_result", "clinical_assessment", "commercial_analysis"],
                "min_evidence_items": 3,
                "partial_acceptance": {"allowed": True, "min_percent": "20", "max_percent": "80"},
            },
        )
        restarted.activate_rule_set("gov", "rules-v2")
        signed_v2 = restarted.sign_milestone(
            "gov",
            {
                "milestone_id": "ms-2",
                "project_id": "proj-1",
                "sequence": 2,
                "title": "毒理预实验与制剂可行性",
                "experiment_plan": "28 天重复给药预实验，同步处方筛选",
                "clinical_hypothesis": "胃肠道安全性窗口支持口服慢病长期给药",
                "commercial_path": "制剂专利先行，授权谈判与自研并行",
                "funding_source_id": "src-1",
                "planned_amount_cny": "1500000",
            },
        )
        ledger = restarted.project_ledger("secretary", "proj-1")
        audit = restarted.audit_chain("secretary")
        source = restarted.funding_source("src-1")
        restarted.connection.close()
    return {
        "status": "ok",
        "recovered_pending": len(recovered),
        "confirmed": confirmed,
        "dispute": dispute,
        "resolved": resolved,
        "signed_under_v2": signed_v2["rule_version"],
        "funding_source": {
            "available_amount_cny": source["available_amount_cny"],
            "released_amount_cny": source["released_amount_cny"],
        },
        "ledger_milestones": [
            {
                "milestone_id": item["milestone_id"],
                "state": item["state"],
                "rule_version": item["rule_version"],
                "next_action": item["next_action"],
                "disbursements": [
                    {
                        "disbursement_id": d["disbursement_id"],
                        "state": d["state"],
                        "released_amount_cny": d["released_amount_cny"],
                        "evidence_source": d["evidence_source"],
                        "missing_conditions": d["missing_conditions"],
                        "next_action": d["next_action"],
                    }
                    for d in item["disbursements"]
                ],
            }
            for item in ledger["milestones"]
        ],
        "audit": audit,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行转化里程碑与拨付联动服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
