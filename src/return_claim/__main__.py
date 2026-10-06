"""命令行启动：python -m return_claim [--host H] [--port P] [--store FILE]"""
from __future__ import annotations

import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="退货索赔协同 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default=None,
                        help="事件 JSONL 持久化路径（缺省为纯内存）")
    args = parser.parse_args()
    serve(args.host, args.port, args.store)


if __name__ == "__main__":
    main()
