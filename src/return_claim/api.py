"""退货索赔协同的 HTTP API（仅标准库）。

运行：PYTHONPATH=src python3 -m return_claim.api --port 8000 \
        --db data/return_claim.db --contract domain/contract.json
"""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import DomainError, NotFoundError, PermissionDenied, ValidationError
from .service import ReturnClaimService
from .store import Store


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _need(body: dict, *fields: str) -> None:
    missing = [field for field in fields if body.get(field) in (None, "")]
    if missing:
        raise ValidationError("缺少字段：" + "、".join(missing))


# ---------------------------------------------------------------------------
# 路由处理：每个 handler 返回 (status, payload)
# ---------------------------------------------------------------------------

def _health(service, body):
    return 200, {"status": "ok", "product": "退货索赔协同"}


def _create_batch(service, body):
    _need(body, "actor", "batch_no", "material", "supplier", "defect_qty", "unit_price")
    return 201, service.register_defect_batch(
        actor=body["actor"], batch_no=body["batch_no"], material=body["material"],
        supplier=body["supplier"], defect_qty=body["defect_qty"], unit_price=body["unit_price"])


def _get_batch(service, body, batch_id):
    return 200, service.get_batch(int(batch_id))


def _batch_reconciliation(service, body, batch_id):
    return 200, service.reconcile_batch(int(batch_id))


def _create_order(service, body):
    _need(body, "actor", "order_no", "lines")
    return 201, service.create_return_order(
        actor=body["actor"], order_no=body["order_no"], lines=body["lines"])


def _get_order(service, body, order_id):
    return 200, service.get_order(int(order_id))


def _submit_order(service, body, order_id):
    _need(body, "actor")
    return 200, service.submit_return_order(actor=body["actor"], order_id=int(order_id))


def _approve_order(service, body, order_id):
    _need(body, "actor")
    return 200, service.approve_return_order(actor=body["actor"], order_id=int(order_id))


def _close_order(service, body, order_id):
    _need(body, "actor")
    return 200, service.close_return_order(actor=body["actor"], order_id=int(order_id))


def _cancel_order(service, body, order_id):
    _need(body, "actor")
    return 200, service.cancel_return_order(
        actor=body["actor"], order_id=int(order_id), reason=body.get("reason", ""))


def _add_evidence(service, body, line_id):
    _need(body, "actor", "kind", "qty")
    return 201, service.record_evidence(
        actor=body["actor"], line_id=int(line_id), kind=body["kind"], qty=body["qty"],
        carrier=body.get("carrier", ""), tracking_no=body.get("tracking_no", ""))


def _determine_liability(service, body, line_id):
    _need(body, "actor", "determined_qty", "reason")
    return 200, service.determine_liability(
        actor=body["actor"], line_id=int(line_id),
        determined_qty=body["determined_qty"], reason=body["reason"])


def _compensate(service, body, line_id):
    _need(body, "actor", "type")
    return 200, service.apply_compensation(
        actor=body["actor"], line_id=int(line_id),
        compensation_type=body["type"], payload=body.get("payload"))


def _line_reconciliation(service, body, line_id):
    return 200, service.reconcile_line(int(line_id))


ROUTES = [
    ("GET", re.compile(r"/api/health"), _health),
    ("POST", re.compile(r"/api/defect-batches"), _create_batch),
    ("GET", re.compile(r"/api/defect-batches/(\d+)"), _get_batch),
    ("GET", re.compile(r"/api/defect-batches/(\d+)/reconciliation"), _batch_reconciliation),
    ("POST", re.compile(r"/api/return-orders"), _create_order),
    ("GET", re.compile(r"/api/return-orders/(\d+)"), _get_order),
    ("POST", re.compile(r"/api/return-orders/(\d+)/submit"), _submit_order),
    ("POST", re.compile(r"/api/return-orders/(\d+)/approve"), _approve_order),
    ("POST", re.compile(r"/api/return-orders/(\d+)/close"), _close_order),
    ("POST", re.compile(r"/api/return-orders/(\d+)/cancel"), _cancel_order),
    ("POST", re.compile(r"/api/return-lines/(\d+)/evidence"), _add_evidence),
    ("POST", re.compile(r"/api/return-lines/(\d+)/liability"), _determine_liability),
    ("POST", re.compile(r"/api/return-lines/(\d+)/compensations"), _compensate),
    ("GET", re.compile(r"/api/return-lines/(\d+)/reconciliation"), _line_reconciliation),
]

_ERROR_STATUS = {
    ValidationError: 400,
    PermissionDenied: 403,
    NotFoundError: 404,
    DomainError: 409,
}


def make_server(service: ReturnClaimService, host: str = "127.0.0.1",
                port: int = 8000) -> ThreadingHTTPServer:
    """构建 HTTP 服务（线程模式，写操作由 Store 的事务锁串行化）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReturnClaim/0.1"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = urlparse(self.path).path
            for route_method, pattern, handler in ROUTES:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                try:
                    body = self._read_body() if method == "POST" else {}
                    status, payload = handler(service, body, *match.groups())
                except DomainError as exc:
                    status = next(
                        (code for kind, code in _ERROR_STATUS.items()
                         if isinstance(exc, kind)), 500)
                    payload = {"error": type(exc).__name__, "message": str(exc)}
                self._send(status, payload)
                return
            self._send(404, {"error": "NotFound", "message": f"未知路径：{method} {path}"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValidationError("请求体必须是合法 JSON") from None
            if not isinstance(body, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return body

        def _send(self, status: int, payload) -> None:
            data = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 保持测试输出干净
            pass

    return ThreadingHTTPServer((host, port), Handler)


def build_service(db_path: str = ":memory:", contract_path: str | None = None
                  ) -> ReturnClaimService:
    contract = None
    if contract_path:
        try:
            from domain_contract.validator import load_contract
        except ImportError:
            load_contract = None
        if load_contract is not None:
            contract = load_contract(contract_path)
    return ReturnClaimService(Store(db_path), contract)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="退货索赔协同后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=":memory:", help="SQLite 路径，默认内存库")
    parser.add_argument("--contract", default=None, help="领域契约 JSON，用于角色校验")
    args = parser.parse_args(argv)
    service = build_service(args.db, args.contract)
    server = make_server(service, args.host, args.port)
    print(f"退货索赔协同 API 监听 http://{args.host}:{args.port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
