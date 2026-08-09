"""参考店铺目录的联网搜索适配器。

参考站后台已经按店铺分类页轮询公开库存。这里仅在用户主动搜索时读取它的
公开产品接口，用作候选覆盖和结果补充；不发送任何链动账号凭据。
"""
from __future__ import annotations

from ..models import Category, ProductStatus
from .base import ProductRecord, to_float, to_int
from .session import Fetcher
from .shop_api import item_url_key, shop_token


REFERENCE_CATALOG_BASE = "https://ldxp.wdnmd.wang"
REFERENCE_STORE_TOKENS = frozenset(
    token.casefold()
    for token in (
        "2VWX76A4",
        "S23ZXR7X",
        "5CF1CBYF",
        "JW7OZLDA",
        "WPXSCE1B",
        "1G3OAIPK",
        "LV9C7XJE",
        "PAXOVOVJ",
        "IY16OXB7",
        "YIMENGAI",
        "MENGZE",
        "YE9N6WYK",
        "GPTICU",
        "IK7OYLXZ",
        "MIRAGE",
        "7LFUCYI0",
        "CAO",
        "Z65QS0QG",
        "RCCFTO9M",
        "PLUS123",
        "SUBAIP",
        "857",
    )
)
_CATEGORY_BY_PRODUCT_TYPE = {
    "card": Category.CARD,
    "article": Category.KNOWLEDGE,
    "resource": Category.RESOURCE,
    "equity": Category.RIGHTS,
}


class ReferenceCatalog:
    """读取参考站当前监控到的公开零售商品。"""

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher or Fetcher(
            credential_policy="public",
            base_url=REFERENCE_CATALOG_BASE,
            merchant_token="",
        )

    def search(
        self,
        keywords: str,
        *,
        in_stock_only: bool = True,
        max_pages: int = 3,
        page_size: int = 100,
    ) -> list[ProductRecord]:
        query = " ".join((keywords or "").split())
        if not query:
            return []

        records: dict[str, ProductRecord] = {}
        total_pages = 1
        for page in range(1, max(1, max_pages) + 1):
            params: dict[str, object] = {
                "search": query,
                "page": page,
                "pageSize": page_size,
            }
            if in_stock_only:
                params["inStock"] = "true"
            payload = self.fetcher.get_json("/api/products", params=params)
            if not isinstance(payload, dict):
                raise RuntimeError("参考店铺目录返回非预期格式")

            items = payload.get("items")
            if not isinstance(items, list):
                raise RuntimeError("参考店铺目录缺少商品列表")
            total_pages = max(1, to_int(payload.get("totalPages"), 1))

            for item in items:
                if not isinstance(item, dict):
                    continue
                record = self._map(item)
                if record is not None:
                    records[record.external_id] = record
            if page >= total_pages or not items:
                break
        return list(records.values())

    @staticmethod
    def _map(item: dict) -> ProductRecord | None:
        store_url = str(item.get("storeUrl") or "")
        token = shop_token(store_url)
        product_url = str(item.get("url") or "")
        key = item_url_key(product_url) or str(item.get("externalId") or "").strip()
        if not token or not key:
            return None

        active = bool(item.get("active", True))
        in_stock = bool(item.get("inStock", False))
        stock = max(0, to_int(item.get("stockCount"), 0))
        if not active:
            status = ProductStatus.OFF
            stock = 0
        elif in_stock and stock > 0:
            # 聚合目录可能比店铺公开接口慢一轮同步。正库存只能用来发现候选，
            # 不能直接当作本次搜索已核验库存；必须由 ShopApi 再确认。
            status = ProductStatus.NORMAL
            stock = -1
        else:
            status = ProductStatus.OUT
            stock = 0

        price_cents = to_float(item.get("currentPriceCents"), 0.0)
        product_type = str(item.get("productType") or "").lower()
        return ProductRecord(
            external_id=f"r:{key}",
            name=str(item.get("name") or "未命名商品"),
            category=_CATEGORY_BY_PRODUCT_TYPE.get(product_type, Category.OTHER),
            merchant_name=str(item.get("storeName") or token),
            merchant_link=store_url,
            sale_price=round(price_cents / 100, 2),
            stock=stock,
            status=status,
            url=product_url,
            raw={**item, "_source": "reference_catalog"},
        )
