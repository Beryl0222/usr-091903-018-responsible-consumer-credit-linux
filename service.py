"""消费贷审慎额度管理的运行入口。

- python3 service.py --check           核对服务配置
- python3 service.py --port 8000       启动 HTTP 服务
- python3 service.py --db credit.db    使用文件数据库（默认内存库，便于联调）
"""

import argparse
import os
from http.server import ThreadingHTTPServer

from credit.app import BankApp
from credit.httpapi import build_handler

SERVICE_ID = "responsible-consumer-credit"
SERVICE_NAME = "消费贷审慎额度管理"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app(db_path: str | None = None) -> BankApp:
    return BankApp(db_path or os.environ.get("RCC_DB", ":memory:"))


# 默认模块级 Handler（内存库），供契约测试与本地联调使用
app = build_app()
Handler = build_handler(app)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.environ.get("RCC_DB", ":memory:"),
                        help="SQLite 数据库路径，默认内存库")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 冒烟：领域装配可正常构造
        probe = build_app(":memory:")
        assert probe.store is not None
        print("基础检查通过")
        return
    server_app = app if args.db == ":memory:" else build_app(args.db)
    handler = Handler if args.db == ":memory:" else build_handler(server_app)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
