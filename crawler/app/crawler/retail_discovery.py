"""按关键词从公开网页索引中发现链动小铺零售店与商品。

链动小铺的公开店铺接口必须先知道店铺 token，平台前端没有提供跨店搜索
接口。这里使用公开搜索索引补齐入口发现，只提取 pay.ldxp.cn 的公开链接，
随后仍由 ShopApi 向链动小铺接口核验商品是否在售。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from curl_cffi import requests as cffi

from ..config import settings
from .shop_api import item_url_key, shop_url_token


DISCOVERY_URL = "https://lite.duckduckgo.com/lite/"
_PUBLIC_LINK_RE = re.compile(
    r"https?://pay\.ldxp\.cn/(?:shop|item)/[^\s\"'<>?&#]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RetailDiscoveryResult:
    shop_tokens: tuple[str, ...] = ()
    item_keys: tuple[str, ...] = ()


class RetailDiscovery:
    """发现候选链接；搜索索引失败时返回空结果，不阻断主搜索。"""

    def __init__(
        self,
        session: Optional[Any] = None,
        *,
        max_shops: int = 8,
        max_items: int = 12,
        timeout_s: float = 2.5,
    ) -> None:
        self._owns_session = session is None
        self.session = session or cffi.Session(impersonate=settings.impersonate)
        self.max_shops = max(0, max_shops)
        self.max_items = max(0, max_items)
        self.timeout_s = max(0.5, timeout_s)

    @staticmethod
    def _queries(keywords: str) -> list[str]:
        value = " ".join((keywords or "").split())
        if not value:
            return []
        return [
            f"pay.ldxp.cn/shop {value}",
            f"pay.ldxp.cn/item {value}",
        ]

    def discover(self, keywords: str) -> RetailDiscoveryResult:
        shops: list[str] = []
        items: list[str] = []
        seen_shops: set[str] = set()
        seen_items: set[str] = set()

        for query in self._queries(keywords):
            try:
                response = self.session.get(
                    DISCOVERY_URL,
                    params={"q": query},
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                    },
                    timeout=self.timeout_s,
                )
                if response.status_code != 200:
                    continue
                # DuckDuckGo 的真实目标位于 uddg 参数中，通常经过一至两层编码。
                text = unquote(unquote(response.text or ""))
            except Exception:  # noqa: BLE001 - 外部索引不可用不应拖垮主搜索
                continue

            for match in _PUBLIC_LINK_RE.finditer(text):
                url = match.group(0).rstrip(".,，。;；)）]】").strip()
                shop_key = shop_url_token(url)
                item_key = item_url_key(url)
                if shop_key:
                    folded = shop_key.casefold()
                    if folded not in seen_shops and len(shops) < self.max_shops:
                        seen_shops.add(folded)
                        shops.append(shop_key)
                elif item_key:
                    folded = item_key.casefold()
                    if folded in seen_items or len(items) >= self.max_items:
                        continue
                    seen_items.add(folded)
                    items.append(item_key)

        return RetailDiscoveryResult(tuple(shops), tuple(items))

    def close(self) -> None:
        if self._owns_session:
            self.session.close()
