"""HTTP 接口层：路由、认证与 JSON 编解码（仅标准库）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import services
from .auth import authenticate, issue_token
from .db import connect
from .errors import ApiError

OFFICE = {"office"}
PANEL = {"panel"}
APPLICANT = {"applicant"}
AUDIT = {"office", "auditor"}
READ = {"office", "auditor", "panel"}


class Ctx:
    """单次请求上下文。"""

    def __init__(self, conn, actor, body, params, query, app_state):
        self.conn = conn
        self.actor = actor
        self.body = body
        self.params = params
        self.query = query
        self.app_state = app_state


ROUTES = []


def route(method, pattern, roles=None):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn, roles))
        return fn

    return deco


# ---------- 认证与元信息 ----------

@route("POST", "/tokens")
def h_login(ctx):
    actor_id = ctx.body.get("actor_id", "")
    secret = ctx.body.get("secret", "")
    return 200, issue_token(ctx.conn, actor_id, secret)


@route("GET", "/meta/contract")
def h_contract(ctx):
    summary = ctx.app_state.get("contract_summary")
    if summary is None:
        raise ApiError(404, "CONTRACT_UNAVAILABLE", "领域契约文件不可用")
    return 200, summary


# ---------- 资金批次 ----------

@route("POST", "/fund-batches", OFFICE)
def h_create_batch(ctx):
    return services.create_batch(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/fund-batches", READ)
def h_list_batches(ctx):
    return services.list_batches(ctx.conn, ctx.actor)


@route("GET", "/fund-batches/{batch_id}", READ)
def h_get_batch(ctx):
    return services.get_batch(ctx.conn, ctx.actor, ctx.params["batch_id"])


# ---------- 汇率口径 ----------

@route("POST", "/exchange-rates", OFFICE)
def h_create_rate(ctx):
    return services.create_exchange_rate(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/exchange-rates", READ)
def h_list_rates(ctx):
    return services.list_exchange_rates(ctx.conn, ctx.actor)


# ---------- 评审轮次 ----------

@route("POST", "/rounds", OFFICE)
def h_create_round(ctx):
    return services.create_round(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/rounds", READ)
def h_list_rounds(ctx):
    return services.list_rounds(ctx.conn, ctx.actor)


@route("GET", "/rounds/{round_id}", READ)
def h_get_round(ctx):
    return services.get_round_view(ctx.conn, ctx.actor, ctx.params["round_id"])


@route("POST", "/rounds/{round_id}/seal", OFFICE)
def h_seal_round(ctx):
    return services.seal_round(ctx.conn, ctx.actor, ctx.params["round_id"])


# ---------- 评审与试算、授予 ----------

@route("POST", "/rounds/{round_id}/reviews", PANEL)
def h_submit_review(ctx):
    return services.submit_review(ctx.conn, ctx.actor, ctx.params["round_id"], ctx.body)


@route("GET", "/rounds/{round_id}/reviews", READ)
def h_list_reviews(ctx):
    return services.list_reviews(ctx.conn, ctx.actor, ctx.params["round_id"])


@route("POST", "/rounds/{round_id}/trial", {"office", "panel"})
def h_trial(ctx):
    return services.trial(ctx.conn, ctx.actor, ctx.params["round_id"], ctx.body)


@route("POST", "/rounds/{round_id}/awards", PANEL)
def h_create_award(ctx):
    return services.create_award(ctx.conn, ctx.actor, ctx.params["round_id"], ctx.body)


@route("GET", "/rounds/{round_id}/awards", AUDIT)
def h_round_awards(ctx):
    return services.list_round_awards(ctx.conn, ctx.actor, ctx.params["round_id"])


# ---------- 候选材料 ----------

@route("POST", "/me/materials", APPLICANT)
def h_submit_materials(ctx):
    return services.submit_materials(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/me/materials", APPLICANT)
def h_my_materials(ctx):
    return services.my_materials(ctx.conn, ctx.actor)


@route("GET", "/me/awards", APPLICANT)
def h_my_awards(ctx):
    return services.my_awards(ctx.conn, ctx.actor)


@route("GET", "/candidates", READ)
def h_candidates(ctx):
    return services.list_candidates(ctx.conn, ctx.actor)


@route("POST", "/candidates/{applicant_id}/verify", OFFICE)
def h_verify(ctx):
    return services.verify_materials(ctx.conn, ctx.actor, ctx.params["applicant_id"])


# ---------- 回避关系与限制规则 ----------

@route("POST", "/recusals", OFFICE)
def h_create_recusal(ctx):
    return services.create_recusal(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/recusals", READ)
def h_list_recusals(ctx):
    return services.list_recusals(ctx.conn, ctx.actor)


@route("POST", "/rules", OFFICE)
def h_create_rule(ctx):
    return services.create_rule(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/rules", READ)
def h_list_rules(ctx):
    return services.list_rules(ctx.conn, ctx.actor)


# ---------- 授予查询、结算与快照 ----------

@route("GET", "/awards/{award_id}", AUDIT)
def h_get_award(ctx):
    return services.get_award(ctx.conn, ctx.actor, ctx.params["award_id"])


@route("GET", "/awards/{award_id}/snapshot", AUDIT)
def h_get_snapshot(ctx):
    return services.get_snapshot(ctx.conn, ctx.actor, ctx.params["award_id"])


@route("POST", "/awards/{award_id}/settle", OFFICE)
def h_settle(ctx):
    return services.settle_award(ctx.conn, ctx.actor, ctx.params["award_id"], ctx.body)


# ---------- 资金转移 ----------

@route("POST", "/transfers", OFFICE)
def h_transfer(ctx):
    return services.transfer(ctx.conn, ctx.actor, ctx.body)


@route("GET", "/transfers", AUDIT)
def h_list_transfers(ctx):
    return services.list_transfers(ctx.conn, ctx.actor)


# ---------- 审计复算 ----------

@route("GET", "/audit/rounds/{round_id}/recompute", AUDIT)
def h_recompute(ctx):
    return services.recompute_round(ctx.conn, ctx.actor, ctx.params["round_id"])


@route("GET", "/audit/batches/{batch_id}/ledger", AUDIT)
def h_batch_ledger(ctx):
    return services.batch_ledger(ctx.conn, ctx.actor, ctx.params["batch_id"])


@route("GET", "/audit/residuals", AUDIT)
def h_residuals(ctx):
    return services.residuals(ctx.conn, ctx.actor)


# ---------- 服务装配 ----------

def load_contract_summary():
    """读取仓库内的领域契约摘要（不可用时返回 None）。"""
    try:
        from domain_contract.validator import load_contract, summarize
    except ImportError:
        return None
    target = Path(__file__).resolve().parents[2] / "domain" / "contract.json"
    if not target.exists():
        return None
    try:
        return summarize(load_contract(target))
    except Exception:
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(db_path, app_state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ScholarshipServer/1.0"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ApiError(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
                for m, regex, fn, roles in ROUTES:
                    if m != method:
                        continue
                    match = regex.match(parsed.path)
                    if not match:
                        continue
                    conn = connect(db_path)
                    try:
                        actor = None
                        if roles is not None:
                            header = self.headers.get("Authorization", "")
                            token = header[7:] if header.startswith("Bearer ") else ""
                            actor = authenticate(conn, token)
                            if actor is None:
                                raise ApiError(401, "UNAUTHORIZED", "缺少或无效的访问令牌")
                            if actor["role"] not in roles:
                                raise ApiError(403, "FORBIDDEN", "当前角色无权访问该资源")
                        ctx = Ctx(conn, actor, body, match.groupdict(), query, app_state)
                        result = fn(ctx)
                        status, payload = result if isinstance(result, tuple) else (200, result)
                        self._send(status, payload)
                    finally:
                        conn.close()
                    return
                raise ApiError(404, "NOT_FOUND", "资源不存在")
            except ApiError as exc:
                error = {"code": exc.code, "message": exc.message}
                if exc.details:
                    error["details"] = exc.details
                self._send(exc.status, {"error": error})
            except json.JSONDecodeError:
                self._send(400, {"error": {"code": "BAD_JSON",
                                           "message": "请求体不是合法 JSON"}})
            except BrokenPipeError:
                pass
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": f"服务内部错误：{exc}"}})

    return Handler


def create_server(db_path, host="127.0.0.1", port=8000):
    app_state = {"contract_summary": load_contract_summary()}
    return Server((host, port), make_handler(db_path, app_state))


def serve(db_path, host="127.0.0.1", port=8000):
    server = create_server(db_path, host, port)
    actual = server.server_address[1]
    print(f"国际奖学金配置服务已启动：http://{host}:{actual}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
