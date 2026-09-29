"""基于标准库的 HTTP 接口层。

路由表集中声明方法、路径、允许角色与处理器；角色在分发时统一校验，
对象级权限（如资助方只能看本资助方批次）在 service 层校验。
"""
from __future__ import annotations

import json
import re
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, service
from .auth import authenticate
from .errors import ApiError, forbidden, not_found, unauthorized


def _bearer_token(headers) -> str | None:
    value = headers.get("Authorization")
    if not value or not value.startswith("Bearer "):
        return None
    return value[len("Bearer ") :].strip() or None


def _path_id(params: dict, key: str = "id") -> int:
    return int(params[key])


class App:
    """路由分发：每个请求独立数据库连接。"""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self.routes: list[tuple[str, re.Pattern, tuple[str, ...] | None, object]] = []
        self._register()

    def _route(self, method: str, pattern: str, roles, handler) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), roles, handler))

    def _register(self) -> None:
        s = service
        self._route("GET", r"/health", None, lambda c, a, p, b, q: (200, {"status": "ok"}))

        self._route("POST", r"/fx-rate-sets", ("admin",),
                    lambda c, a, p, b, q: (201, s.create_fx_rate_set(c, a, b)))
        self._route("GET", r"/fx-rate-sets/(?P<id>\d+)", ("admin", "auditor", "committee", "funder"),
                    lambda c, a, p, b, q: (200, s.get_fx_rate_set(c, a, _path_id(p))))

        self._route("POST", r"/batches", ("admin", "funder"),
                    lambda c, a, p, b, q: (201, s.create_batch(c, a, b)))
        self._route("GET", r"/batches", ("admin", "funder", "auditor"),
                    lambda c, a, p, b, q: (200, s.list_batches(c, a)))
        self._route("GET", r"/batches/(?P<id>\d+)", ("admin", "funder", "auditor"),
                    lambda c, a, p, b, q: (200, s.get_batch(c, a, _path_id(p))))

        self._route("POST", r"/rules", ("admin",),
                    lambda c, a, p, b, q: (201, s.create_rule(c, a, b)))
        self._route("GET", r"/rules", ("admin", "auditor", "committee"),
                    lambda c, a, p, b, q: (200, s.list_rules(c, a)))

        self._route("POST", r"/rounds", ("admin",),
                    lambda c, a, p, b, q: (201, s.create_round(c, a, b)))
        self._route("GET", r"/rounds", ("admin", "committee", "auditor"),
                    lambda c, a, p, b, q: (200, s.list_rounds(c, a)))
        self._route("GET", r"/rounds/(?P<id>\d+)", ("admin", "committee", "auditor"),
                    lambda c, a, p, b, q: (200, s.get_round(c, a, _path_id(p))))
        self._route("POST", r"/rounds/(?P<id>\d+)/seal", ("admin",),
                    lambda c, a, p, b, q: (200, s.seal_round(c, a, _path_id(p))))

        self._route("POST", r"/candidates", ("admin", "committee"),
                    lambda c, a, p, b, q: (201, s.create_candidate(c, a, b)))
        self._route("GET", r"/candidates", ("admin", "committee", "auditor"), self._list_candidates)
        self._route("GET", r"/candidates/(?P<id>\d+)", ("admin", "committee", "auditor"),
                    lambda c, a, p, b, q: (200, s.get_candidate(c, a, _path_id(p))))
        self._route("POST", r"/candidates/(?P<id>\d+)/eligibility", ("admin", "committee"),
                    lambda c, a, p, b, q: (200, s.set_eligibility(c, a, _path_id(p), b)))

        self._route("POST", r"/recusals", ("admin",),
                    lambda c, a, p, b, q: (201, s.create_recusal(c, a, b)))
        self._route("GET", r"/recusals", ("admin", "auditor", "committee"),
                    lambda c, a, p, b, q: (200, s.list_recusals(c, a)))

        self._route("POST", r"/trials", ("admin", "committee"),
                    lambda c, a, p, b, q: (201, s.create_trial(c, a, b)))
        self._route("GET", r"/trials/(?P<id>\d+)", ("admin", "committee", "auditor"),
                    lambda c, a, p, b, q: (200, s.get_trial(c, a, _path_id(p))))

        self._route("POST", r"/awards", ("admin",), self._create_award)
        self._route("GET", r"/awards", ("admin", "auditor"), self._list_awards)
        self._route("GET", r"/awards/(?P<id>\d+)", ("admin", "auditor"),
                    lambda c, a, p, b, q: (200, s.get_award(c, a, _path_id(p))))
        self._route("POST", r"/awards/(?P<id>\d+)/adjustments", ("admin",),
                    lambda c, a, p, b, q: (200, s.adjust_award(c, a, _path_id(p), b)))
        self._route("GET", r"/awards/(?P<id>\d+)/adjustments", ("admin", "auditor"),
                    lambda c, a, p, b, q: (200, s.list_adjustments(c, a, _path_id(p))))

        self._route("POST", r"/transfers", ("admin",),
                    lambda c, a, p, b, q: (201, s.transfer(c, a, b)))

        self._route("GET", r"/me/candidates", ("applicant",),
                    lambda c, a, p, b, q: (200, s.my_candidates(c, a)))
        self._route("GET", r"/me/awards", ("applicant",),
                    lambda c, a, p, b, q: (200, s.my_awards(c, a)))

        self._route("GET", r"/audit/batches/(?P<id>\d+)/ledger", ("admin", "auditor"),
                    lambda c, a, p, b, q: (200, s.audit_batch_ledger(c, a, _path_id(p))))
        self._route("GET", r"/audit/batches/(?P<id>\d+)/reconcile", ("admin", "auditor"),
                    lambda c, a, p, b, q: (200, s.audit_batch_reconcile(c, a, _path_id(p))))
        self._route("GET", r"/audit/rounds/(?P<id>\d+)/recompute", ("admin", "auditor"),
                    lambda c, a, p, b, q: (200, s.audit_round_recompute(c, a, _path_id(p))))

    @staticmethod
    def _list_candidates(conn, actor, params, body, query):
        raw = query.get("round_id", [None])[0]
        round_id = int(raw) if raw is not None else None
        return 200, service.list_candidates(conn, actor, round_id)

    @staticmethod
    def _create_award(conn, actor, params, body, query):
        view, created = service.create_award(conn, actor, body)
        return (201 if created else 200), view

    @staticmethod
    def _list_awards(conn, actor, params, body, query):
        raw_round = query.get("round_id", [None])[0]
        applicant = query.get("applicant_id", [None])[0]
        round_id = int(raw_round) if raw_round is not None else None
        return 200, service.list_awards(conn, actor, round_id=round_id, applicant_id=applicant)

    def dispatch(self, method: str, path: str, query: dict, body: dict, headers):
        for route_method, pattern, roles, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match is None:
                continue
            conn = db.connect(self.db_path)
            try:
                actor = None
                if roles is not None:
                    actor = authenticate(conn, _bearer_token(headers))
                    if actor is None:
                        raise unauthorized()
                    if actor["role"] not in roles:
                        raise forbidden()
                return handler(conn, actor, match.groupdict(), body, query)
            finally:
                conn.close()
        raise not_found(f"接口不存在：{method} {path}")


def _make_handler(app: App):
    class ScholarshipHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Scholarship/0.2"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str):
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            body = {}
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    self._respond(400, _error("BAD_REQUEST", "请求体不是合法 JSON"))
                    return
                if not isinstance(body, dict):
                    self._respond(400, _error("BAD_REQUEST", "请求体必须是 JSON 对象"))
                    return
            try:
                status, payload = app.dispatch(
                    method, parsed.path, parse_qs(parsed.query), body, self.headers
                )
            except ApiError as exc:
                envelope = {"code": exc.code, "message": exc.message}
                if exc.details is not None:
                    envelope["details"] = exc.details
                self._respond(exc.status, {"error": envelope})
                return
            except sqlite3.IntegrityError:
                self._respond(409, _error("CONFLICT", "数据唯一性冲突"))
                return
            except Exception:  # noqa: BLE001 - 兜底，避免连接悬挂
                traceback.print_exc()
                self._respond(500, _error("INTERNAL", "服务内部错误"))
                return
            self._respond(status, payload)

        def _respond(self, status: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # 静默访问日志
            return

    return ScholarshipHandler


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """构建 HTTP 服务；调用方需先 init_db。"""
    app = App(db_path)
    return ThreadingHTTPServer((host, port), _make_handler(app))
