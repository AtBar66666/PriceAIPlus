"""手动全量同步 PickAI：python sync_pickai.py [--workers 3]."""
from __future__ import annotations

import argparse
import sys
import time

from app.config import settings
from app.crawler.pickai_catalog import load_snapshot
from app.db import init_db
from app.pickai_index import pickai_index


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 PickAI 全部公开报价到本地 SQLite/JSON/CSV")
    parser.add_argument("--workers", type=int, default=settings.pickai_workers, help="并发会话数（1-6）")
    parser.add_argument("--status", action="store_true", help="只显示本地快照状态")
    parser.add_argument(
        "--import-existing",
        action="store_true",
        help="不联网，重新导入 data/pickai_snapshot.json",
    )
    args = parser.parse_args()

    settings.pickai_workers = min(6, max(1, args.workers))
    init_db()
    if args.status:
        print(pickai_index.status())
        return 0
    if args.import_existing:
        status = pickai_index.import_snapshot(
            load_snapshot(settings.db_path.parent / "pickai_snapshot.json")
        )
        print(status["message"])
        return 0

    status = pickai_index.start(force=True)
    print("PickAI 全量同步已启动。")
    last_line = ""
    while status["running"]:
        line = (
            f"[{status['progress']:>3}%] 类型 "
            f"{status['completed_types']}/{max(1, status['product_types'])} · "
            f"分页 {status['pages_completed']} · {status['message']}"
        )
        if line != last_line:
            print(line, flush=True)
            last_line = line
        time.sleep(1.5)
        status = pickai_index.status()

    print(status["message"])
    if status["state"] == "error":
        print(status["error"], file=sys.stderr)
        return 1
    print(f"JSON: {status['json_path']}")
    print(f"CSV : {status['csv_path']}")
    print(f"SQLite 商品: {status['product_count']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
