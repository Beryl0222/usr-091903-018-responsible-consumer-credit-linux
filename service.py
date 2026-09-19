"""消费贷审慎额度管理的运行入口。

- python3 service.py --check          核对服务配置
- python3 service.py --port 8000      启动 HTTP 服务，/health 为身份检查
- --db PATH                           指定 SQLite 路径（默认内存库，重启清空）
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from credit.api import App, create_server
from credit.clock import Clock
from credit.storage import Storage

SERVICE_ID = "responsible-consumer-credit"
SERVICE_NAME = "消费贷审慎额度管理"
API_VERSION = "v1"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME,
            "api_version": API_VERSION}


class Handler(BaseHTTPRequestHandler):
    """保留无依赖的 /health 处理：未携带 App 时也能独立响应健康检查。"""

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            body = json.dumps({"error": "not_found"}, ensure_ascii=False).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(health_payload(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def build_app(db_path=None):
    storage = Storage(db_path or ":memory:")
    return App(storage=storage, clock=Clock())


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default=os.environ.get("CREDIT_DB"),
                        help="SQLite 数据库路径，默认内存库")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 冒烟：存储/规则/台账可导入并完成一次基础计算
        from credit.util import monthly_payment_cents

        assert monthly_payment_cents(120000, "0.072", 12) > 0
        app = build_app(args.db)
        assert app.storage is not None
        print("基础检查通过")
        return
    app = build_app(args.db)
    server = create_server(args.host, args.port, app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
