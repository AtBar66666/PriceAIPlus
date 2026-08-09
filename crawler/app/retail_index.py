"""后台维护账号可发现的公开零售店完整快照。"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from .config import settings
from .crawler.base import ProductRecord
from .crawler.session import host_cooldown_remaining
from .crawler.shop_api import ShopApi
from .crawler.source_square import ShopDirectoryEntry, SourceSquare
from .db import engine
from .models import Product, ProductStatus, Shop, SourceKind
from .service import ingest

logger = logging.getLogger(__name__)

RETAIL_INDEX_SCOPE = "authenticated_goods_pool_plus_keyword_public_index"
RETAIL_INDEX_COVERAGE_NOTE = (
    "索引当前商家账号可从 GoodsPool 发现的全部店铺，并补充关键词公开网页索引发现的店铺；"
    "PickAI 商品链接命中的原店会在实时搜索时增量加入；平台没有全局店铺目录，"
    "因此不代表全部私有或未公开店铺。"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class _ShopWork:
    shop_id: int
    token: str
    name: str
    refresh_time: int = 0
    goods_count: int = 0


@dataclass
class _ShopResult:
    work: _ShopWork
    records: Optional[list[ProductRecord]] = None
    error: Optional[BaseException] = None


class RetailIndexCoordinator:
    """单例式后台协调器；网络并发，所有 SQLite 写入集中在协调线程。"""

    def __init__(
        self,
        *,
        db_engine=None,
        directory_factory: Optional[Callable[[], SourceSquare]] = None,
        shop_api_factory: Optional[Callable[[], ShopApi]] = None,
        token_provider: Optional[Callable[[], str]] = None,
        ttl: timedelta = timedelta(days=7),
        max_concurrency: Optional[int] = None,
    ) -> None:
        self._engine = db_engine or engine
        self._directory_factory = directory_factory or SourceSquare
        # GoodsPool 目录读取可以使用用户主动配置的账号令牌；目录中的公开
        # 店铺内容始终走匿名 ShopApi，绝不把账号凭据带到批量零售抓取。
        self._shop_api_factory = shop_api_factory or ShopApi
        self._token_provider = token_provider or (lambda: settings.merchant_token)
        self._ttl = ttl
        self._max_concurrency = max_concurrency
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._defer_until = 0.0
        self._status = self._initial_status()

    @staticmethod
    def _initial_status() -> dict:
        return {
            "state": "idle",
            "running": False,
            "discovered_shops": 0,
            "indexed_shops": 0,
            "pending_shops": 0,
            "failed_shops": 0,
            "changed_shops": 0,
            "failures": [],
            "product_count": 0,
            "progress": 0.0,
            "current_shop": "",
            "message": "零售索引尚未启动。",
            "started_at": None,
            "finished_at": None,
            "scope": RETAIL_INDEX_SCOPE,
            "coverage_note": RETAIL_INDEX_COVERAGE_NOTE,
        }

    def _update(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def status(self) -> dict:
        with self._lock:
            snapshot = dict(self._status)
            snapshot["failures"] = list(self._status.get("failures", []))
            defer_until = self._defer_until
        cooldown_seconds = int(
            host_cooldown_remaining("https://pay.ldxp.cn") + 0.999
        )
        snapshot["cooldown_seconds"] = cooldown_seconds
        if snapshot.get("running") and cooldown_seconds > 0:
            snapshot["message"] = (
                f"站点触发访问保护，已自动暂停；约 {cooldown_seconds} 秒后继续。"
            )
        deferred_seconds = max(0, int(defer_until - time.monotonic() + 0.999))
        snapshot["deferred_seconds"] = deferred_seconds
        if snapshot.get("running") and cooldown_seconds <= 0 and deferred_seconds > 0:
            snapshot["message"] = "正在优先核验当前搜索价格，完整索引稍后自动继续。"
        try:
            with Session(self._engine) as session:
                snapshot["product_count"] = self._product_count(session)
                if not snapshot.get("running"):
                    discovered = int(
                        session.exec(
                            select(func.count(Shop.id)).where(
                                Shop.kind == SourceKind.PUBLIC_SHOP
                            )
                        ).one()
                        or 0
                    )
                    indexed = int(
                        session.exec(
                            select(func.count(func.distinct(Product.shop_id)))
                            .join(Shop, Product.shop_id == Shop.id)
                            .where(Shop.kind == SourceKind.PUBLIC_SHOP)
                        ).one()
                        or 0
                    )
                    snapshot["discovered_shops"] = discovered
                    snapshot["indexed_shops"] = indexed
                    snapshot["pending_shops"] = max(0, discovered - indexed)
                    if snapshot.get("state") == "idle" and discovered:
                        snapshot["message"] = "已加载本地零售索引，后台刷新处于手动模式。"
        except Exception:  # noqa: BLE001 - 数据库可能尚未由 startup 初始化
            pass
        return snapshot

    def start(self) -> dict:
        """非阻塞启动；已有任务运行时直接返回同一任务状态。"""
        with self._lock:
            if self._status["running"] and self._thread and self._thread.is_alive():
                return dict(self._status)
            if not (self._token_provider() or "").strip():
                self._status = self._initial_status()
                self._status.update(
                    state="error",
                    running=False,
                    message="未配置 Merchant-Token，无法刷新 GoodsPool 店铺目录。",
                    finished_at=_utc_now().isoformat(),
                )
                return dict(self._status)

            self._status = self._initial_status()
            self._status.update(
                state="discovering",
                running=True,
                message="正在刷新 GoodsPool 店铺目录。",
                started_at=_utc_now().isoformat(),
            )
            thread = threading.Thread(
                target=self._run,
                name="retail-index-coordinator",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return dict(self._status)

    trigger = start

    def defer(self, seconds: float = 45.0) -> None:
        """暂缓新店铺抓取，让用户触发的当前页价格核验优先使用站点额度。"""
        with self._lock:
            self._defer_until = max(
                self._defer_until,
                time.monotonic() + max(0.0, seconds),
            )

    def _wait_if_deferred(self) -> None:
        while True:
            with self._lock:
                remaining = self._defer_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def wait(self, timeout: Optional[float] = None) -> bool:
        """仅供关闭流程和测试等待，不用于 API 请求线程。"""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @staticmethod
    def _close_source(source) -> None:
        fetcher = getattr(source, "fetcher", None)
        close = getattr(fetcher, "close", None)
        if callable(close):
            close()
            return
        close = getattr(source, "close", None)
        if callable(close):
            close()

    def _discover_directory(self) -> list[ShopDirectoryEntry]:
        source = self._directory_factory()
        try:
            return source.list_shops()
        finally:
            self._close_source(source)

    @staticmethod
    def _directory_note(entry: ShopDirectoryEntry) -> str:
        return f"GoodsPool 自动发现（目录商品数 {entry.goods_count}）"

    def _upsert_directory(
        self,
        session: Session,
        entries: list[ShopDirectoryEntry],
    ) -> int:
        existing_shops = session.exec(
            select(Shop).where(Shop.kind == SourceKind.PUBLIC_SHOP)
        ).all()
        by_token = {shop.url.casefold(): shop for shop in existing_shops if shop.url}
        changed_shops = 0

        for entry in entries:
            folded = entry.token.casefold()
            shop = by_token.get(folded)
            if shop is None:
                shop = Shop(
                    name=entry.name or entry.token,
                    kind=SourceKind.PUBLIC_SHOP,
                    url=entry.token,
                    note=self._directory_note(entry),
                    directory_refresh_time=entry.refresh_time,
                    directory_goods_count=entry.goods_count,
                    directory_status=entry.status,
                )
                session.add(shop)
                by_token[folded] = shop
                changed_shops += 1
                continue
            fingerprint_known = (
                shop.directory_refresh_time is not None
                and shop.directory_goods_count is not None
                and shop.directory_status is not None
            )
            fingerprint_changed = fingerprint_known and (
                shop.directory_refresh_time != entry.refresh_time
                or shop.directory_goods_count != entry.goods_count
                or shop.directory_status != entry.status
            )
            if fingerprint_changed:
                shop.last_synced_at = None
                changed_shops += 1
            shop.name = entry.name or shop.name or entry.token
            shop.note = self._directory_note(entry)
            shop.directory_refresh_time = entry.refresh_time
            shop.directory_goods_count = entry.goods_count
            shop.directory_status = entry.status
            session.add(shop)
        session.commit()
        return changed_shops

    def _pending_work(self, session: Session) -> tuple[list[_ShopWork], int]:
        cutoff = _utc_now().replace(tzinfo=None) - self._ttl
        shops = session.exec(
            select(Shop).where(
                Shop.kind == SourceKind.PUBLIC_SHOP,
                Shop.active == True,  # noqa: E712
            )
        ).all()
        pending: list[_ShopWork] = []
        fresh = 0
        for shop in shops:
            if shop.id is None or not shop.url:
                continue
            last_synced = (
                _naive_utc(shop.last_synced_at)
                if shop.last_synced_at is not None
                else None
            )
            if last_synced is not None and last_synced >= cutoff:
                fresh += 1
                continue
            pending.append(
                _ShopWork(
                    shop.id,
                    shop.url,
                    shop.name or shop.url,
                    shop.directory_refresh_time or 0,
                    shop.directory_goods_count or 0,
                )
            )
        pending.sort(
            key=lambda work: (
                -work.refresh_time,
                -work.goods_count,
                work.name.casefold(),
            )
        )
        return pending, fresh

    @staticmethod
    def _product_count(session: Session) -> int:
        count = session.exec(
            select(func.count(Product.id))
            .join(Shop, Product.shop_id == Shop.id)
            .where(
                Shop.kind == SourceKind.PUBLIC_SHOP,
                Product.status != ProductStatus.OFF,
            )
        ).one()
        return int(count or 0)

    def _worker_queue(
        self,
        work_queue: "queue.Queue[_ShopWork]",
        results: "queue.Queue[_ShopResult]",
    ) -> None:
        source = None
        try:
            source = self._shop_api_factory()
        except BaseException as exc:  # noqa: BLE001 - 每家店都需留下失败结果
            while True:
                try:
                    work = work_queue.get_nowait()
                except queue.Empty:
                    break
                results.put(_ShopResult(work=work, error=exc))
            return

        try:
            while True:
                try:
                    work = work_queue.get_nowait()
                except queue.Empty:
                    break
                self._update(current_shop=work.name)
                try:
                    self._wait_if_deferred()
                    records = source.fetch(work.token, shop_name=work.name)
                    results.put(_ShopResult(work=work, records=records))
                except BaseException as exc:  # noqa: BLE001 - 单店失败不影响其余店铺
                    results.put(_ShopResult(work=work, error=exc))
        finally:
            self._close_source(source)

    def _index_pending(self, works: list[_ShopWork], fresh_count: int) -> list[str]:
        if not works:
            return []

        total_shops = fresh_count + len(works)
        worker_count = max(
            1,
            min(
                self._max_concurrency or settings.retail_index_concurrency,
                len(works),
            ),
        )
        work_queue: "queue.Queue[_ShopWork]" = queue.Queue()
        for work in works:
            work_queue.put(work)
        results: "queue.Queue[_ShopResult]" = queue.Queue()
        failures: list[str] = []
        completed = 0
        indexed = fresh_count

        workers = [
            threading.Thread(
                target=self._worker_queue,
                args=(work_queue, results),
                name=f"retail-index-worker-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()

        with Session(self._engine) as session:
            while completed < len(works):
                result = results.get()
                completed += 1
                remaining = len(works) - completed
                if result.error is None and result.records is not None:
                    shop = session.get(Shop, result.work.shop_id)
                    if shop is None:
                        result.error = RuntimeError("店铺在索引过程中被移除")
                    else:
                        try:
                            ingest(
                                session,
                                shop,
                                result.records,
                                complete_snapshot=True,
                            )
                            indexed += 1
                        except BaseException as exc:  # noqa: BLE001
                            session.rollback()
                            result.error = exc

                if result.error is not None:
                    failures.append(
                        f"{result.work.name} ({result.work.token}): {result.error}"
                    )
                self._update(
                    indexed_shops=indexed,
                    pending_shops=remaining + len(failures),
                    failed_shops=len(failures),
                    failures=list(failures),
                    progress=round(indexed / total_shops * 100, 1),
                    current_shop=result.work.name,
                    message=f"已处理 {completed}/{len(works)} 家待索引店铺。",
                )

        for worker in workers:
            worker.join()
        return failures

    def _run(self) -> None:
        directory_error = ""
        failures: list[str] = []
        changed_shops = 0
        try:
            try:
                entries = self._discover_directory()
                self._update(
                    discovered_shops=len(entries),
                    message=f"GoodsPool 发现 {len(entries)} 家账号可见店铺。",
                )
            except Exception as exc:  # noqa: BLE001 - 仍尝试恢复旧库中的未完成店铺
                entries = []
                directory_error = str(exc)
                logger.exception("GoodsPool directory refresh failed")
                self._update(message=f"GoodsPool 目录刷新失败：{exc}")

            with Session(self._engine) as session:
                if entries:
                    changed_shops = self._upsert_directory(session, entries)
                works, fresh_count = self._pending_work(session)
                product_count = self._product_count(session)
                discovered_count = fresh_count + len(works)

            self._update(
                state="indexing",
                changed_shops=changed_shops,
                discovered_shops=discovered_count,
                indexed_shops=fresh_count,
                pending_shops=len(works),
                product_count=product_count,
                progress=(
                    100.0
                    if not works
                    else round(fresh_count / discovered_count * 100, 1)
                ),
                message=(
                    "目录指纹未变化，所有公开店铺快照均仍有效。"
                    if not works
                    else f"准备索引 {len(works)} 家未同步或已过期店铺。"
                ),
            )
            failures = self._index_pending(works, fresh_count)

            with Session(self._engine) as session:
                product_count = self._product_count(session)

            has_error = bool(directory_error or failures)
            if directory_error and failures:
                message = (
                    f"目录刷新失败，且 {len(failures)} 家店铺索引失败；"
                    "未完成店铺会在下次任务继续。"
                )
            elif directory_error:
                message = "GoodsPool 目录刷新失败；已继续处理本地待索引店铺。"
            elif failures:
                message = f"{len(failures)} 家店铺索引失败，未完成店铺会在下次任务继续。"
            else:
                message = "账号可发现店铺的零售快照索引已更新。"
            with self._lock:
                indexed_shops = int(self._status["indexed_shops"])
                discovered_shops = int(self._status["discovered_shops"])
            progress = (
                round(indexed_shops / discovered_shops * 100, 1)
                if discovered_shops
                else 100.0
            )
            self._update(
                state="error" if has_error else "ready",
                running=False,
                pending_shops=len(failures),
                product_count=product_count,
                progress=progress,
                current_shop="",
                message=message,
                finished_at=_utc_now().isoformat(),
            )
        except BaseException as exc:  # noqa: BLE001 - 后台线程必须落到可查询状态
            logger.exception("Retail index coordinator failed")
            self._update(
                state="error",
                running=False,
                current_shop="",
                message=f"零售索引任务异常终止：{exc}",
                failures=[f"协调器: {exc}"],
                finished_at=_utc_now().isoformat(),
            )


retail_index = RetailIndexCoordinator()
