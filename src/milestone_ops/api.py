"""无第三方依赖的转化里程碑与拨付联动 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import MilestoneOpsError, ValidationFailed
from .service import TranslationMilestoneService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: TranslationMilestoneService) -> None:
        self.service = service
        self._lock = threading.Lock()

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        # SQLite 连接跨线程共享，请求串行执行以避免事务交错
        with self._lock:
            return self._dispatch(method, target, headers, body)

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def _dispatch(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/rule_sets":
                return Response(201, self.service.create_rule_set(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "rule_sets" and parts[2] == "activate":
                return Response(200, self.service.activate_rule_set(actor, parts[1]))
            if method == "GET" and path == "/rule_sets/active":
                active = self.service.active_rule_set()
                if active is None:
                    return Response(404, {"error": {"code": "not_found", "message": "没有已生效的规则版本"}})
                return Response(200, active)
            if method == "POST" and path == "/funding_sources":
                return Response(201, self.service.create_funding_source(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "funding_sources" and parts[2] == "adjust":
                return Response(200, self.service.adjust_funding_source(actor, parts[1], payload["additional_amount_cny"]))
            if method == "GET" and len(parts) == 2 and parts[0] == "funding_sources":
                return Response(200, self.service.funding_source(parts[1]))
            if method == "POST" and path == "/projects":
                return Response(201, self.service.create_project(actor, payload))
            if method == "POST" and path == "/milestones":
                return Response(201, self.service.sign_milestone(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "evidence":
                return Response(201, self.service.submit_evidence(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "disbursements":
                return Response(201, self.service.schedule_disbursement(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "decisions":
                return Response(201, self.service.propose_decision(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "disputes":
                return Response(201, self.service.raise_dispute(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "milestones" and parts[2] == "resume":
                return Response(200, self.service.resume_milestone(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "decisions" and parts[2] == "science_signoff":
                return Response(200, self.service.sign_decision_science(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "decisions" and parts[2] == "finance_signoff":
                return Response(200, self.service.sign_decision_finance(actor, parts[1]))
            if method == "GET" and path == "/decisions/pending":
                return Response(200, self.service.pending_decisions(actor))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, self.service.resolve_dispute(actor, parts[1], payload["resolution"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "ledger":
                return Response(200, self.service.project_ledger(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except MilestoneOpsError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MilestoneOps/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动转化里程碑与分期拨付联动服务")
    parser.add_argument("--database", type=Path, default=Path("milestone_ops.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(TranslationMilestoneService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
