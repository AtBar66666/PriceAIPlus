"""关键词搜索的后台刷新协调器。

API 请求只读取 SQLite；真实网络搜索在守护线程里更新缓存，避免用户每次
搜索都等待货源、商品详情和候选店铺请求完成。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlmodel import Session

from .db import engine
from .service import live_search

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _RefreshState:
    refreshing: bool = False
    refreshed_at: Optional[datetime] = None
    attempted_at: Optional[datetime] = None
    error: str = ""


class SearchRefreshCoordinator:
    """按关键词去重、限频并串行执行联网刷新。"""

    def __init__(
        self,
        *,
        db_engine=None,
        refresh_fn: Optional[Callable[..., tuple[list, int]]] = None,
        ttl: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=30),
        start_delay: float = 0.35,
    ) -> None:
        self._engine = db_engine or engine
        self._refresh_fn = refresh_fn or live_search
        self._ttl = ttl
        self._retry_delay = retry_delay
        self._start_delay = max(0.0, start_delay)
        self._lock = threading.RLock()
        self._network_slot = threading.Semaphore(1)
        self._states: dict[tuple[str, str, str], _RefreshState] = {}

    @staticmethod
    def _key(keywords: str, goods_type: str, platform: str) -> tuple[str, str, str]:
        query = " ".join((keywords or "").split()).casefold()
        return (
            query,
            (goods_type or "").strip().casefold(),
            (platform or "all").strip().casefold(),
        )

    @staticmethod
    def _snapshot(state: _RefreshState, *, refresh_started: bool = False) -> dict:
        return {
            "refreshing": state.refreshing,
            "refreshed_at": state.refreshed_at.isoformat() if state.refreshed_at else None,
            "refresh_attempted_at": (
                state.attempted_at.isoformat() if state.attempted_at else None
            ),
            "refresh_error": state.error,
            "refresh_started": refresh_started,
        }

    def status(self, keywords: str, goods_type: str = "", platform: str = "all") -> dict:
        key = self._key(keywords, goods_type, platform)
        with self._lock:
            state = self._states.get(key, _RefreshState())
            return self._snapshot(state)

    def start(
        self,
        keywords: str,
        goods_type: str = "",
        platform: str = "all",
        public_only: bool = False,
    ) -> dict:
        """非阻塞启动；同关键词运行中或 TTL 内不会重复联网。"""
        key = self._key(keywords, goods_type, platform)
        if not key[0]:
            return self._snapshot(_RefreshState())

        now = _now()
        with self._lock:
            state = self._states.setdefault(key, _RefreshState())
            if state.refreshing:
                return self._snapshot(state)
            if state.refreshed_at and now - state.refreshed_at < self._ttl:
                return self._snapshot(state)
            if state.error and state.attempted_at and now - state.attempted_at < self._retry_delay:
                return self._snapshot(state)

            state.refreshing = True
            state.attempted_at = now
            state.error = ""
            thread = threading.Timer(
                self._start_delay,
                function=self._run,
                args=(key, keywords.strip(), goods_type, platform, public_only),
            )
            thread.name = f"search-refresh-{abs(hash(key))}"
            thread.daemon = True
            thread.start()
            return self._snapshot(state, refresh_started=True)

    def _run(
        self,
        key: tuple[str, str, str],
        keywords: str,
        goods_type: str,
        platform: str,
        public_only: bool,
    ) -> None:
        error = ""
        refreshed_at: Optional[datetime] = None
        try:
            with self._network_slot:
                with Session(self._engine) as session:
                    self._refresh_fn(
                        session,
                        keywords,
                        goods_type,
                        current=1,
                        page_size=0,
                        in_stock_only=False,
                        platform=platform,
                        public_only=public_only,
                    )
            refreshed_at = _now()
        except Exception as exc:  # noqa: BLE001 - 后台失败不能阻断本地搜索
            error = str(exc)
            logger.exception("Keyword refresh failed for %r", keywords)
        finally:
            with self._lock:
                state = self._states.setdefault(key, _RefreshState())
                state.refreshing = False
                state.error = error
                if refreshed_at is not None:
                    state.refreshed_at = refreshed_at


search_refresh = SearchRefreshCoordinator()
