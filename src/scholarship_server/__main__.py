"""命令行入口：PYTHONPATH=src python3 -m scholarship_server"""
from __future__ import annotations

import argparse

from .app import serve
from .db import connect, init_db
from .seed import seed_users


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scholarship_server", description="国际奖学金配置服务端")
    parser.add_argument("--db", default="scholarship.db", help="SQLite 数据库文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="写入预置账号")
    args = parser.parse_args()
    init_db(args.db)
    if args.seed:
        conn = connect(args.db)
        try:
            added = seed_users(conn)
        finally:
            conn.close()
        print(f"预置账号写入完成（新增 {added} 个）")
    serve(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
