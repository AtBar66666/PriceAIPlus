"""开发入口：python -m app.main"""
from __future__ import annotations

import logging
import sys

import uvicorn

from .api import app
from .config import DATA_DIR, settings


def main() -> None:
    packaged = getattr(sys, "frozen", False)
    if packaged:
        logging.basicConfig(
            filename=DATA_DIR / "backend.log",
            encoding="utf-8",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    # 桌面版只导入内置/现有本地快照，不在启动时联网扫 PickAI 百余分页。
    # 全量更新由界面的“重新同步”按钮显式触发。
    app.state.bootstrap_pickai_snapshot = True
    app.state.auto_pickai_sync = False
    options = {"access_log": False, "log_config": None} if packaged else {}
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, **options)


if __name__ == "__main__":
    main()
