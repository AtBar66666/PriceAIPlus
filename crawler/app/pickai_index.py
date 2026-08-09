"""PickAI 全量公开报价索引协调器。"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from .config import DATA_DIR, settings
from .crawler.pickai_catalog import (
    PICKAI_SHOP_NAME,
    PickAICatalog,
    PickAISnapshot,
    export_snapshot,
    load_snapshot,
)
from .db import engine
from .models import Shop, SourceKind


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PickAIIndexCoordinator:
    """单进程后台同步；只在完整抓取成功后替换本地快照。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._message = "尚未同步 PickAI 公开报价"
        self._error = ""
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._types_total = 0
        self._types_completed = 0
        self._pages_completed = 0
        self._current_type = ""
        self._declared_quotes = 0
        self._last_summary: dict[str, Any] = {}
        self._restore_export_summary()

    def _restore_export_summary(self) -> None:
        path = DATA_DIR / "pickai_snapshot.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            summary = payload.get("summary") or {}
            self._types_total = int(summary.get("product_types") or 0)
            self._types_completed = self._types_total
            self._declared_quotes = int(summary.get("declared_quotes") or 0)
            self._last_summary = {
                "categories": int(summary.get("categories") or 0),
                "relay_items": int(summary.get("relay_items") or 0),
                "duplicates_merged": int(summary.get("duplicates_merged") or 0),
                "request_count": int(summary.get("requests") or 0),
            }
            self._message = (
                f"本地已有 {int(summary.get('quotes') or 0):,} 条 PickAI 公开报价"
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # 导出损坏不影响 SQLite 搜索；下次成功同步会原子重建文件。
            return

    @staticmethod
    def _shop(session: Session) -> Shop | None:
        return session.exec(
            select(Shop).where(
                Shop.name == PICKAI_SHOP_NAME,
                Shop.kind == SourceKind.SOURCE_SQUARE,
            )
        ).first()

    def _stored_status(self) -> tuple[int, datetime | None]:
        try:
            with Session(engine) as session:
                shop = self._shop(session)
                if shop is None:
                    return 0, None
                return int(shop.product_count or 0), shop.last_synced_at
        except Exception:  # noqa: BLE001 - 初始化表之前读取时按空快照处理
            return 0, None

    @staticmethod
    def _is_stale(last_synced_at: datetime | None) -> bool:
        if last_synced_at is None:
            return True
        return _now() - last_synced_at > timedelta(
            minutes=max(1, settings.pickai_refresh_minutes)
        )

    def status(self) -> dict[str, Any]:
        product_count, last_synced_at = self._stored_status()
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            state = "syncing" if running else self._state
            if not running and state == "idle" and last_synced_at is not None:
                state = "ready"
            total = max(1, self._types_total)
            progress = (
                min(99, round(self._types_completed / total * 100))
                if running
                else 100 if last_synced_at is not None else 0
            )
            return {
                "state": state,
                "running": running,
                "stale": self._is_stale(last_synced_at),
                "product_count": product_count,
                "product_types": self._types_total,
                "completed_types": self._types_completed,
                "pages_completed": self._pages_completed,
                "declared_quotes": self._declared_quotes,
                "progress": progress,
                "current_type": self._current_type,
                "message": self._message,
                "error": self._error,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "finished_at": self._finished_at.isoformat() if self._finished_at else None,
                "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
                "json_path": str(DATA_DIR / "pickai_snapshot.json"),
                "csv_path": str(DATA_DIR / "pickai_quotes.csv"),
                **self._last_summary,
            }

    def start(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            _product_count, last_synced_at = self._stored_status()
            if not force and not self._is_stale(last_synced_at):
                self._state = "ready"
                self._message = "PickAI 本地目录已就绪；搜索实时查关键词，全量更新仅手动触发"
                return self.status()
            self._state = "syncing"
            self._message = "正在读取 PickAI 分类和全部报价分页"
            self._error = ""
            self._started_at = _now()
            self._finished_at = None
            self._types_total = 0
            self._types_completed = 0
            self._pages_completed = 0
            self._current_type = ""
            self._declared_quotes = 0
            self._last_summary = {}
            self._thread = threading.Thread(
                target=self._run,
                name="pickai-full-index",
                daemon=True,
            )
            self._thread.start()
            return self.status()

    def run_blocking(self) -> dict[str, Any]:
        """命令行/测试入口：在当前线程完成一次强制同步。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("PickAI 同步已经在运行")
            self._state = "syncing"
            self._message = "正在读取 PickAI 分类和全部报价分页"
            self._error = ""
            self._started_at = _now()
            self._finished_at = None
            self._types_total = 0
            self._types_completed = 0
            self._pages_completed = 0
            self._current_type = ""
            self._declared_quotes = 0
            self._last_summary = {}
        self._run()
        status = self.status()
        if status["state"] == "error":
            raise RuntimeError(status["error"])
        return status

    def import_snapshot(self, snapshot: PickAISnapshot) -> dict[str, Any]:
        """不联网，把已导出的完整 JSON 快照重新写入搜索库。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("PickAI 同步已经在运行")
            self._state = "syncing"
            self._message = f"正在离线写入 {len(snapshot.quotes):,} 条报价"
            self._error = ""
            self._started_at = _now()
        self._persist(snapshot)
        with self._lock:
            self._state = "ready"
            self._message = f"已从本地快照导入 {len(snapshot.quotes):,} 条公开报价"
            self._finished_at = _now()
            self._types_total = len(snapshot.product_types)
            self._types_completed = len(snapshot.product_types)
            self._declared_quotes = snapshot.declared_quotes
        return self.status()

    @staticmethod
    def _bundled_snapshot_path() -> Path | None:
        """返回 PyInstaller onefile 解包目录中的首启种子。"""
        if not getattr(sys, "frozen", False):
            return None
        bundle_root = Path(str(getattr(sys, "_MEIPASS", "")))
        if not bundle_root:
            return None
        for candidate in (
            bundle_root / "seed" / "pickai_snapshot.json",
            bundle_root / "pickai_snapshot.json",
        ):
            if candidate.is_file():
                return candidate
        return None

    def bootstrap_bundled_snapshot(self) -> dict[str, Any]:
        """新安装首次启动时先导入随程序附带的完整快照。"""
        product_count, _last_synced_at = self._stored_status()
        if product_count > 0:
            with self._lock:
                self._state = "ready"
                self._message = (
                    f"本地已有 {product_count:,} 条 PickAI 报价；搜索实时查关键词，全量更新仅手动触发"
                )
                self._error = ""
            return self.status()
        bundled = self._bundled_snapshot_path()
        if bundled is None:
            return self.status()
        try:
            return self.import_snapshot(load_snapshot(bundled))
        except Exception as exc:  # noqa: BLE001 - 失败后仍可手动联网重建
            with self._lock:
                self._state = "error"
                self._message = "内置 PickAI 快照导入失败，可手动联网重建"
                self._error = str(exc)
            return self.status()

    def _progress(self, event: dict[str, Any]) -> None:
        with self._lock:
            if event.get("event") == "page":
                self._pages_completed += 1
                self._current_type = str(event.get("product_type_name") or "")
                self._declared_quotes = max(
                    self._declared_quotes,
                    int(event.get("total") or 0),
                )
                self._message = (
                    f"正在抓取 {self._current_type}："
                    f"{int(event.get('received') or 0)}/{int(event.get('total') or 0)}"
                )
            elif event.get("event") == "type":
                self._types_completed += 1
                product_type = event.get("product_type") or {}
                self._current_type = str(product_type.get("name") or "")

    def _persist(self, snapshot: PickAISnapshot) -> None:
        # 先原子生成可独立使用的导出；完整抓取失败时不会覆盖旧文件/旧库。
        export_snapshot(
            snapshot,
            DATA_DIR / "pickai_snapshot.json",
            DATA_DIR / "pickai_quotes.csv",
        )
        from .service import ingest  # 延迟导入，避免 service -> 常量时形成循环

        with Session(engine) as session:
            shop = self._shop(session)
            if shop is None:
                shop = Shop(
                    name=PICKAI_SHOP_NAME,
                    url=settings.pickai_base_url,
                    kind=SourceKind.SOURCE_SQUARE,
                    note="PickAI 公开 JSON 接口全量报价快照",
                )
                session.add(shop)
                session.commit()
                session.refresh(shop)
            else:
                shop.url = settings.pickai_base_url
                shop.active = True
                shop.note = "PickAI 公开 JSON 接口全量报价快照"
                session.add(shop)
                session.commit()
            ingest(
                session,
                shop,
                snapshot.product_records(),
                complete_snapshot=True,
                inventory_verified=False,
            )

    def _run(self) -> None:
        catalog = PickAICatalog(base_url=settings.pickai_base_url)
        try:
            # 元数据拿到后 full_snapshot 才能精确知道类型数；前台先按当前站点
            # 的已知规模展示，随后由最终结果覆盖。
            with self._lock:
                self._types_total = 48
            snapshot = catalog.full_snapshot(
                workers=max(1, settings.pickai_workers),
                progress=self._progress,
            )
            with self._lock:
                self._types_total = len(snapshot.product_types)
                self._message = f"正在写入 {len(snapshot.quotes):,} 条去重报价"
            self._persist(snapshot)
            with self._lock:
                self._state = "ready"
                self._message = (
                    f"已同步 {len(snapshot.product_types)} 类、"
                    f"{len(snapshot.quotes):,} 条公开报价；后续全量更新仅手动触发"
                )
                self._error = ""
                self._finished_at = _now()
                self._types_completed = len(snapshot.product_types)
                self._declared_quotes = snapshot.declared_quotes
                self._last_summary = {
                    "categories": len(snapshot.categories),
                    "relay_items": len(snapshot.relay_providers.get("items") or []),
                    "duplicates_merged": snapshot.duplicate_quotes,
                    "request_count": snapshot.request_count,
                }
        except Exception as exc:  # noqa: BLE001 - 保留旧完整快照并暴露错误
            with self._lock:
                self._state = "error"
                self._message = "PickAI 全量同步失败，已保留上一次完整快照"
                self._error = str(exc)
                self._finished_at = _now()
        finally:
            catalog.close()


pickai_index = PickAIIndexCoordinator()
