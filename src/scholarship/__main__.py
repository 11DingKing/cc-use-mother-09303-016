"""命令行入口：PYTHONPATH=src python3 -m scholarship --db scholarship.db --port 8000 --seed"""
from __future__ import annotations

import argparse

from . import db
from .api import make_server
from .seed import DEMO_TOKENS, seed


def main() -> None:
    parser = argparse.ArgumentParser(prog="scholarship", description="国际奖学金配置服务端")
    parser.add_argument("--db", default="scholarship.db", help="SQLite 数据库文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="写入演示数据（含演示令牌）")
    args = parser.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)
    if args.seed:
        result = seed(conn)
        if result.get("seeded"):
            print("已写入演示数据，演示令牌（仅限本地演示）：")
            for name, token in DEMO_TOKENS.items():
                print(f"  {name}: {token}")
        else:
            print("数据库已有数据，跳过演示数据写入")
    conn.close()

    server = make_server(args.db, args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
