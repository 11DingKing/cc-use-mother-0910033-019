"""HTTP API：基于标准库 http.server，无第三方依赖。

路由概览（所有写操作都需要 JSON body 含 ``actor`` 与 ``reason``，
reason 即「每次调整依据」，服务端强制非空）：

    POST /cases/<case_id>/open                         开立退货索赔单
    POST /cases/<case_id>/defect-batches               登记缺陷批次
    POST /cases/<case_id>/lines                        新增退货行
    POST /cases/<case_id>/evidences                    挂接物流证据
    POST /cases/<case_id>/liability                    责任认定
    POST /cases/<case_id>/approve                      批准退货（原子库存+应收）
    POST /cases/<case_id>/goods-received               物流签退结算
    POST /cases/<case_id>/partial-accept               部分接受（补偿）
    POST /cases/<case_id>/exchange                     换货抵扣（补偿）
    POST /cases/<case_id>/dispute-review               争议复核（补偿）
    POST /cases/<case_id>/revoke                       撤销（补偿）
    POST /cases/<case_id>/close                        关闭

    GET  /cases                                        单据清单
    GET  /cases/<case_id>                              单据详情
    GET  /cases/<case_id>/events                       事件流
    GET  /cases/<case_id>/reconciliation               实物/责任/金额对账
    GET  /cases/<case_id>/audit-trail                  逐事件调整依据

金额一律使用整数「分」，数量为非负整数。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .commands import CommandService
from .errors import DomainError
from .events import EventStore
from .queries import QueryService


def _make_handler(service: CommandService, query: QueryService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReturnClaim/1.0"

        # 静默默认访问日志，保留可通过子类打开
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        # ---- 基础收发 -----------------------------------------------------

        def _send(self, code: int, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("请求体不能为空")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 解析失败：{exc}") from None
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _context(self, data: dict[str, Any]) -> tuple[str, str]:
            actor = str(data.pop("actor", "")).strip()
            reason = str(data.pop("reason", "")).strip()
            if not actor:
                raise ValueError("actor 不能为空")
            if not reason:
                raise ValueError("reason 不能为空：每次调整必须记录依据")
            return actor, reason

        # ---- 命令统一封装 -------------------------------------------------

        def _command(self, data: dict[str, Any], case_id: str,
                     fn: Callable[[str, str, str, dict[str, Any]], Any]) -> None:
            try:
                actor, reason = self._context(data)
                state, event = fn(case_id, actor, reason, data)
            except DomainError as exc:
                self._send(409, {"error": type(exc).__name__, "message": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})
            else:
                self._send(201, {
                    "event": event.to_dict(),
                    "status": state.status.value,
                    "reconciliation": query.reconciliation(case_id),
                })

        # ---- 路由 ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/cases":
                    cases = []
                    for cid in query.store.all_case_ids():
                        s = query.load(cid)
                        cases.append({"case_id": cid, "supplier": s.supplier,
                                      "title": s.title, "status": s.status.value})
                    self._send(200, {"cases": cases})
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 2 and parts[0] == "cases":
                    self._send(200, query.case_detail(parts[1]))
                    return
                if len(parts) == 3 and parts[0] == "cases":
                    cid, sub = parts[1], parts[2]
                    if sub == "events":
                        self._send(200, {"case_id": cid,
                                         "events": query.events(cid)})
                    elif sub == "reconciliation":
                        self._send(200, query.reconciliation(cid))
                    elif sub == "audit-trail":
                        self._send(200, query.audit_trail(cid))
                    else:
                        self._send(404, {"error": "NotFound",
                                         "message": f"未知资源：{path}"})
                    return
            except DomainError as exc:
                self._send(404, {"error": type(exc).__name__,
                                 "message": str(exc)})
                return
            self._send(404, {"error": "NotFound", "message": f"未知路径：{path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            parts = path.strip("/").split("/") if path != "/" else []
            try:
                data = self._body()
            except ValueError as exc:
                self._send(400, {"error": "BadRequest", "message": str(exc)})
                return

            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "open":
                cid = parts[1]

                def fn(case_id: str, actor: str, reason: str,
                       d: dict[str, Any]) -> Any:
                    return service.open_case(
                        case_id,
                        supplier=str(d["supplier"]),
                        actor=actor, reason=reason,
                        title=str(d.get("title", "")),
                    )

                self._command(data, cid, fn)
                return

            routes: dict[str, Callable[..., Any]] = {
                "defect-batches": lambda c, a, r, d: service.register_defect_batch(
                    c, batch_no=str(d["batch_no"]), material=str(d["material"]),
                    defect_qty=int(d["defect_qty"]), actor=a, reason=r,
                    defect_desc=str(d.get("defect_desc", ""))),
                "lines": lambda c, a, r, d: service.add_return_line(
                    c, line_no=int(d["line_no"]), batch_no=str(d["batch_no"]),
                    request_qty=int(d["request_qty"]),
                    unit_price=int(d["unit_price"]), actor=a, reason=r),
                "evidences": lambda c, a, r, d: service.attach_logistics_evidence(
                    c, evidence_id=str(d["evidence_id"]),
                    doc_type=str(d["doc_type"]), doc_no=str(d["doc_no"]),
                    confirmed_return_qty=int(d["confirmed_return_qty"]),
                    actor=a, reason=r, carrier=str(d.get("carrier", "")),
                    note=str(d.get("note", ""))),
                "liability": lambda c, a, r, d: service.determine_liability(
                    c, determinations=list(d["determinations"]),
                    actor=a, reason=r),
                "approve": lambda c, a, r, d: service.approve_return(
                    c, approvals=list(d["approvals"]), actor=a, reason=r),
                "goods-received": lambda c, a, r, d: service.record_goods_received(
                    c, receipts=list(d["receipts"]),
                    evidence_id=str(d["evidence_id"]), actor=a, reason=r),
                "partial-accept": lambda c, a, r, d: service.partial_accept(
                    c, accepted=list(d["accepted"]), actor=a, reason=r),
                "exchange": lambda c, a, r, d: service.exchange_offset(
                    c, exchanges=list(d["exchanges"]), actor=a, reason=r),
                "dispute-review": lambda c, a, r, d: service.dispute_review(
                    c, reviews=list(d["reviews"]), actor=a, reason=r),
                "revoke": lambda c, a, r, d: service.revoke(
                    c, line_nos=list(d["line_nos"]), actor=a, reason=r),
                "close": lambda c, a, r, d: service.close(c, actor=a, reason=r),
            }

            if (len(parts) == 3 and parts[0] == "cases"
                    and parts[2] in routes):
                self._command(data, parts[1], routes[parts[2]])
                return

            self._send(404, {"error": "NotFound", "message": f"未知路径：{path}"})

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8080,
                  store_path: str | None = None) -> ThreadingHTTPServer:
    """构造 HTTP 服务。ThreadingHTTPServer + 每 case RLock 保证并发安全。"""
    store = EventStore(store_path)
    service = CommandService(store)
    query = QueryService(store)
    handler = _make_handler(service, query)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8080,
          store_path: str | None = None) -> None:
    httpd = create_server(host, port, store_path)
    print(f"退货索赔协同服务已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
