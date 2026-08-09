"""货源广场适配器：真实商品搜索接口（POST + Merchant-Token 鉴权）。

从 SPA source-square 路由源码 + 真实响应确认：
- 货源(供货商)列表：POST /merchantApi/GoodsPool/list  → 每项是一个店铺(含 goods_count)
- 商品搜索：POST /merchantApi/MyParent/searchGoodsList
  body: {current, pageSize, name, goods_type, keywords}
  返回信封：{code:1, data:{list:[...], total:N}}

商品项真实字段：
  id, goods_type(card/knowledge/resource/equity), name, price(售价),
  cost_price(我的成本), agent_priceN(代理价), stock_count, category{name},
  user{nickname,...}, link, child(已对接则非空)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..config import DATA_DIR
from ..models import Category, ProductStatus
from .base import ProductRecord, Source, to_float, to_int
from .session import BlockedError, Fetcher
from .shop_api import RETAIL_BASE, shop_url_token

SEARCH_ENDPOINT = "/merchantApi/MyParent/searchGoodsList"
POOL_ENDPOINT = "/merchantApi/GoodsPool/list"

GOODS_TYPE_TO_CAT = {
    "card": Category.CARD,
    "knowledge": Category.KNOWLEDGE,
    "resource": Category.RESOURCE,
    "equity": Category.RIGHTS,
}

_AGENT_KEY = re.compile(r"^agent_price\d+$")


@dataclass(frozen=True)
class ShopDirectoryEntry:
    token: str
    name: str
    goods_count: int
    refresh_time: int = 0
    status: int = 1


class SourceSquare(Source):
    kind = "source_square"

    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        page_size: int = 50,
        *,
        base_url: Optional[str] = None,
        merchant_token: Optional[str] = None,
        public_base_url: str = RETAIL_BASE,
    ) -> None:
        self.fetcher = fetcher or Fetcher(
            base_url=base_url,
            merchant_token=merchant_token,
            merchant_referer=(
                f"{base_url.rstrip('/')}/merchant/" if base_url else None
            ),
        )
        self.page_size = page_size
        self.public_base_url = public_base_url.rstrip("/")

    def search(
        self,
        keywords: str,
        goods_type: str = "",
        current: int = 1,
        page_size: Optional[int] = None,
        dump: bool = False,
    ) -> tuple[list[ProductRecord], int]:
        payload = self.fetcher.post_json(
            SEARCH_ENDPOINT,
            {
                "current": current,
                "pageSize": page_size or self.page_size,
                "name": "",
                "goods_type": goods_type or "",
                "keywords": keywords,
            },
        )
        if dump:
            (DATA_DIR / "last_search.txt").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2)[:400000], encoding="utf-8"
            )
        data = self._check(payload)
        items = data.get("list") or []
        total = to_int(data.get("total"), 0)
        return [self._map(i) for i in items], total

    def shop_directory_page(
        self,
        current: int = 1,
        page_size: Optional[int] = None,
    ) -> tuple[list[ShopDirectoryEntry], int]:
        """读取一页账号可见的 GoodsPool 店铺目录。"""
        records, total, _ = self._shop_directory_page(current, page_size or 2000)
        return records, total

    def _shop_directory_page(
        self,
        current: int,
        page_size: int,
    ) -> tuple[list[ShopDirectoryEntry], int, list[dict[str, Any]]]:
        payload = self.fetcher.post_json(
            POOL_ENDPOINT,
            {"current": current, "pageSize": page_size, "tags_id": 0},
        )
        data = self._check(payload)
        raw_items = data.get("list") or []
        if not isinstance(raw_items, list):
            raise BlockedError("货源池店铺目录返回了非预期列表格式。")

        by_token: dict[str, ShopDirectoryEntry] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            entry = self._map_shop(item)
            if entry is None:
                continue
            folded = entry.token.casefold()
            previous = by_token.get(folded)
            if previous is None:
                by_token[folded] = entry
                continue
            by_token[folded] = ShopDirectoryEntry(
                token=previous.token,
                name=entry.name if previous.name == previous.token else previous.name,
                goods_count=max(previous.goods_count, entry.goods_count),
                refresh_time=max(previous.refresh_time, entry.refresh_time),
                status=entry.status,
            )
        return list(by_token.values()), max(0, to_int(data.get("total"), 0)), raw_items

    def list_shops(
        self,
        page_size: int = 2000,
        max_pages: Optional[int] = None,
    ) -> list[ShopDirectoryEntry]:
        """完整读取 GoodsPool 店铺目录，并按 token 大小写无关去重。"""
        if page_size <= 0:
            raise ValueError("店铺目录 page_size 必须大于 0")

        by_token: dict[str, ShopDirectoryEntry] = {}
        expected_total = 0
        received = 0
        current = 1
        page_fingerprints: set[str] = set()

        while True:
            entries, page_total, raw_items = self._shop_directory_page(current, page_size)
            expected_total = max(expected_total, page_total)
            if not raw_items:
                if received >= expected_total:
                    return list(by_token.values())
                raise RuntimeError(
                    f"货源池店铺目录第 {current} 页提前为空："
                    f"应有 {expected_total} 条，实际仅收到 {received} 条"
                )

            fingerprint = json.dumps(raw_items, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint in page_fingerprints:
                raise RuntimeError(f"货源池店铺目录第 {current} 页重复，无法形成完整目录")
            page_fingerprints.add(fingerprint)
            received += len(raw_items)

            for entry in entries:
                folded = entry.token.casefold()
                previous = by_token.get(folded)
                if previous is None:
                    by_token[folded] = entry
                    continue
                by_token[folded] = ShopDirectoryEntry(
                    token=previous.token,
                    name=entry.name if previous.name == previous.token else previous.name,
                    goods_count=max(previous.goods_count, entry.goods_count),
                    refresh_time=max(previous.refresh_time, entry.refresh_time),
                    status=entry.status,
                )

            if expected_total and received >= expected_total:
                return list(by_token.values())
            if not expected_total and len(raw_items) < page_size:
                return list(by_token.values())
            if max_pages is not None and current >= max_pages:
                raise RuntimeError(
                    f"货源池店铺目录超过抓取上限（{max_pages} 页），未形成完整目录"
                )
            current += 1

    def _map_shop(self, item: dict[str, Any]) -> Optional[ShopDirectoryEntry]:
        user = item.get("user")
        user = user if isinstance(user, dict) else {}
        link = str(user.get("link") or item.get("shop_link") or item.get("link") or "")
        token = shop_url_token(link, self.public_base_url)
        if not token:
            return None
        raw_name = user.get("nickname") or item.get("nickname") or item.get("shop_name") or token
        name = " ".join(str(raw_name).split()) or token
        return ShopDirectoryEntry(
            token=token,
            name=name,
            goods_count=max(0, to_int(item.get("goods_count"), 0)),
            refresh_time=max(0, to_int(item.get("refresh_time"), 0)),
            status=to_int(item.get("status"), 1),
        )

    def search_all(
        self,
        keywords: str,
        goods_type: str = "",
        page_size: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> list[ProductRecord]:
        """按接口 total 抓完全部分页；任何缺页都明确失败。"""
        size = page_size or self.page_size
        by_id: dict[str, ProductRecord] = {}
        expected_total: Optional[int] = None
        current = 1

        while True:
            records, page_total = self.search(keywords, goods_type, current, size)
            expected_total = max(expected_total or 0, page_total)
            if not records:
                if len(by_id) >= expected_total:
                    return list(by_id.values())
                raise RuntimeError(
                    f"商品搜索第 {current} 页提前为空：应有 {expected_total} 条，实际仅 {len(by_id)} 条"
                )

            before = len(by_id)
            for record in records:
                by_id.setdefault(record.external_id, record)
            if len(by_id) == before:
                raise RuntimeError(f"商品搜索第 {current} 页与前页重复，无法形成完整结果")
            if len(by_id) >= expected_total:
                return list(by_id.values())

            if max_pages is not None and current >= max_pages:
                raise RuntimeError(
                    f"商品搜索超过抓取上限（{max_pages} 页），未形成完整结果"
                )
            current += 1

    def fetch(self, target: Optional[str] = None, max_pages: Optional[int] = None) -> list[ProductRecord]:
        """把一个关键词的所有页抓全。"""
        if not target:
            return []
        return self.search_all(target, "", self.page_size, max_pages)

    @staticmethod
    def _check(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BlockedError("接口返回非预期格式（可能不是 JSON）。")
        code = payload.get("code")
        if code != 1:
            msg = payload.get("msg") or "未知错误"
            if code == 401:
                raise BlockedError(f"未登录：{msg}（请在设置里重新填 Merchant-Token）")
            raise BlockedError(f"接口返回失败(code={code})：{msg}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _map(item: dict[str, Any]) -> ProductRecord:
        user = item.get("user") or {}
        stock = to_int(item.get("stock_count"), 0)
        cost = to_float(item.get("cost_price"), 0)
        sale = to_float(item.get("price"), 0)

        agent = 0.0
        for k, v in item.items():
            if _AGENT_KEY.match(k):
                agent = to_float(v, 0)
                break

        gt = str(item.get("goods_type") or "")
        category = GOODS_TYPE_TO_CAT.get(gt, Category.OTHER)

        # 接口会把未上架商品也混入搜索结果。status=1 才是已上架；
        # 未上架商品即使仍有 stock_count，也绝不能当作有货。
        listed = to_int(item.get("status"), 1) == 1
        if not listed:
            status = ProductStatus.OFF
        else:
            status = ProductStatus.NORMAL if stock > 0 else ProductStatus.OUT
        normalized_stock = stock if listed else 0

        return ProductRecord(
            external_id=str(item.get("id", "")),
            name=str(item.get("name", "未命名商品")),
            category=category,
            merchant_name=str(user.get("nickname", "")),
            merchant_link=str(user.get("link", "")),
            sale_price=sale,
            agent_price=agent,
            cost_price=cost,
            stock=normalized_stock,
            status=status,
            is_linked=item.get("child") is not None,
            url=str(item.get("link", "")),
            raw=item,
        )
