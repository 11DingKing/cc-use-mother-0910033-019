"""退货索赔协同后端。

公开入口：

* ``EventStore``：只追加事件日志（内存 / JSONL 持久化）；
* ``CommandService``：命令服务（批准原子联动、补偿事件）；
* ``QueryService``：读模型与三方对账；
* ``create_server`` / ``serve``：零依赖 HTTP API。
"""
from __future__ import annotations

from .commands import CommandService
from .errors import DomainError
from .events import EventStore
from .queries import QueryService

__all__ = ["CommandService", "DomainError", "EventStore", "QueryService",
           "create_server", "serve"]


def __getattr__(name: str):  # 延迟导入，避免无关场景拉起 http.server
    if name in ("create_server", "serve"):
        from .api import create_server, serve
        return {"create_server": create_server, "serve": serve}[name]
    raise AttributeError(name)
