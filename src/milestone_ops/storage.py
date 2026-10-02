"""转化里程碑与拨付联动服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS milestone_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('research_lead','advisor','fund_manager','governance','secretary')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','active','superseded')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE TABLE IF NOT EXISTS funding_sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    total_amount_cny TEXT NOT NULL,
    available_amount_cny TEXT NOT NULL,
    released_amount_cny TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incubation_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    research_lead_id TEXT NOT NULL REFERENCES milestone_users(user_id),
    state TEXT NOT NULL DEFAULT 'incubating' CHECK(state IN ('incubating','paused','closed')),
    created_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES incubation_projects(project_id),
    sequence INTEGER NOT NULL,
    title TEXT NOT NULL,
    experiment_plan TEXT NOT NULL,
    clinical_hypothesis TEXT NOT NULL,
    commercial_path TEXT NOT NULL,
    funding_source_id TEXT NOT NULL REFERENCES funding_sources(source_id),
    planned_amount_cny TEXT NOT NULL,
    rule_set_id TEXT NOT NULL REFERENCES rule_sets(rule_set_id),
    rule_version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'signed'
        CHECK(state IN ('signed','submitted','returned','partially_accepted','suspended','accepted')),
    revision INTEGER NOT NULL DEFAULT 1,
    signed_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    signed_at TEXT NOT NULL,
    UNIQUE(project_id, sequence)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    kind TEXT NOT NULL
        CHECK(kind IN ('experiment_result','clinical_assessment','commercial_analysis','financial_document')),
    summary TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_milestone
ON evidence_items(milestone_id, submitted_at);

CREATE TABLE IF NOT EXISTS milestone_decisions (
    decision_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    action TEXT NOT NULL CHECK(action IN ('accept','partial_accept','return_for_evidence','suspend')),
    accept_percent TEXT,
    note TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed')),
    science_signed_by TEXT REFERENCES milestone_users(user_id),
    science_signed_at TEXT,
    finance_signed_by TEXT REFERENCES milestone_users(user_id),
    finance_signed_at TEXT,
    evidence_snapshot_json TEXT,
    missing_conditions_json TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    proposed_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    proposed_at TEXT NOT NULL,
    confirmed_at TEXT,
    CHECK(
        science_signed_by IS NULL
        OR finance_signed_by IS NULL
        OR science_signed_by <> finance_signed_by
    )
);

CREATE INDEX IF NOT EXISTS idx_decisions_milestone
ON milestone_decisions(milestone_id, state);

CREATE TABLE IF NOT EXISTS disbursements (
    disbursement_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    funding_source_id TEXT NOT NULL REFERENCES funding_sources(source_id),
    amount_cny TEXT NOT NULL,
    released_amount_cny TEXT NOT NULL DEFAULT '0',
    state TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(state IN ('scheduled','held','released','partially_released','frozen')),
    state_before_freeze TEXT,
    decision_id TEXT REFERENCES milestone_decisions(decision_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    scheduled_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    scheduled_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_disbursements_milestone
ON disbursements(milestone_id, state);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    disbursement_id TEXT REFERENCES disbursements(disbursement_id),
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    raised_by TEXT NOT NULL REFERENCES milestone_users(user_id),
    raised_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES milestone_users(user_id),
    resolved_at TEXT,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_disputes_milestone
ON disputes(milestone_id, state);

CREATE TABLE IF NOT EXISTS milestone_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS milestone_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_milestone_audit_entity
ON milestone_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务按线程分发请求，连接需跨线程共享；并发由服务层事务与 API 锁串行化
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
