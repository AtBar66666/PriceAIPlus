"""零售店铺适配器：pay.ldxp.cn 的公开店铺接口（无需登录，Visitorid 头即可）。

- 店铺信息：POST /shopApi/Shop/info      body: {token, category_key}
- 商品列表：POST /shopApi/Shop/goodsList body: {token, keywords, goods_type, current, pageSize}
  token 即店铺 URL 里 /shop/<token> 的那段（如 jianshang）。
返回 {code:1, data:{list, total}}。商品详情接口可用于确认是否仍然上架：
- 商品详情：POST /shopApi/Shop/goodsInfo body: {goods_key, trade_no}

商品列表可能在 extend.stock_count 暴露库存；缺少该字段时用 stock=-1
表示「库存未知」。未上架用 status=OFF、stock=0，绝不能把上架等同于有货。
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Optional
from urllib.parse import unquote, urlsplit

from ..models import Category, ProductStatus
from .base import ProductRecord, Source, to_float, to_int
from .session import Fetcher

RETAIL_BASE = "https://pay.ldxp.cn"
GOODS_TYPES = ["card", "article", "resource", "equity"]
GOODS_TYPE_COUNT_KEYS = {
    "card": "card_count",
    "article": "article_count",
    "resource": "resource_count",
    "equity": "equity_count",
}
MAX_SCOPED_CATEGORIES_PER_SEARCH = 6
GT_CAT = {
    "card": Category.CARD,
    "article": Category.KNOWLEDGE,
    "resource": Category.RESOURCE,
    "equity": Category.RIGHTS,
}
UNLISTED_MARKERS = ("未上架", "已下架", "商品不存在")
SHOP_CLOSED_MARKERS = ("店铺已打烊", "店铺已关闭", "店铺休息中")
_MAX_PUBLIC_KEY_LENGTH = 96
_MAX_PUBLIC_KEY_BYTES = 384
_SAFE_KEY_PUNCTUATION = frozenset("._-")
_BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_API_GOODS_TYPE = {
    "card": "card",
    "knowledge": "article",
    "article": "article",
    "resource": "resource",
    "equity": "equity",
}
_PUBLIC_VISITOR_ID = uuid.uuid4().hex[:9]


class ShopClosedError(RuntimeError):
    """商品仍有列表缓存，但所属店铺当前不可购买。"""


def _safe_public_key(value: str) -> str:
    """校验公开 URL 的单段标识，允许中文等 Unicode 字母和数字。"""
    key = str(value or "")
    try:
        byte_length = len(key.encode("utf-8"))
    except UnicodeEncodeError:
        return ""
    if (
        not key
        or len(key) > _MAX_PUBLIC_KEY_LENGTH
        or byte_length > _MAX_PUBLIC_KEY_BYTES
        or key in {".", ".."}
    ):
        return ""

    first_category = unicodedata.category(key[0])
    if first_category[0] not in {"L", "N"}:
        return ""
    for char in key:
        category = unicodedata.category(char)
        if category[0] in {"L", "N", "M"} or char in _SAFE_KEY_PUNCTUATION:
            continue
        return ""
    return key


def _public_url_key(value: str, kind: str, base_url: str = RETAIL_BASE) -> str:
    raw = (value or "").strip()
    if (
        not raw
        or len(raw) > 2048
        or any(unicodedata.category(char)[0] == "C" for char in raw)
    ):
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        expected_host = (urlsplit(base_url).hostname or "").lower()
        if not expected_host or (parsed.hostname or "").lower() != expected_host:
            return ""
        if parsed.port not in {None, 80, 443}:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        if _BAD_PERCENT_ESCAPE_RE.search(parsed.path):
            return ""
        path = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return ""

    # 先解码再按路径段拆分，防止 %2f、%5c、双重编码等越界技巧。
    if "\\" in path or "%" in path:
        return ""
    parts = path.split("/")
    if len(parts) == 4 and parts[-1] == "":
        parts.pop()
    if len(parts) != 3 or parts[0] != "" or parts[1] != kind:
        return ""
    return _safe_public_key(parts[2])


def shop_url_token(value: str, base_url: str = RETAIL_BASE) -> str:
    """只从指定平台的完整公开店铺 URL 中提取 token。"""
    return _public_url_key(value, "shop", base_url)


def shop_token(url_or_token: str, base_url: str = RETAIL_BASE) -> str:
    """接受完整公开店铺 URL 或裸 token，拒绝商品页和外域 URL。"""
    raw = (url_or_token or "").strip()
    from_url = shop_url_token(raw, base_url)
    if from_url:
        return from_url
    return _safe_public_key(raw)


def item_url_key(value: str, base_url: str = RETAIL_BASE) -> str:
    """只从指定平台的完整公开商品 URL 中提取 goods_key。"""
    return _public_url_key(value, "item", base_url)


class ShopApi(Source):
    kind = "public_shop"

    def __init__(self, fetcher: Optional[Fetcher] = None) -> None:
        self.fetcher = fetcher or Fetcher(credential_policy="public")
        self._vid = _PUBLIC_VISITOR_ID

    def _post(self, path: str, body: dict) -> dict:
        return self.fetcher.post_json(
            RETAIL_BASE + path,
            body,
            extra_headers={
                "Origin": RETAIL_BASE,
                "Referer": RETAIL_BASE + "/",
                "Visitorid": self._vid,
            },
        )

    def shop_info(self, token: str) -> dict:
        payload = self._post("/shopApi/Shop/info", {"token": token, "category_key": ""})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("code") != 1 or not isinstance(data, dict):
            message = payload.get("msg") if isinstance(payload, dict) else "非 JSON 响应"
            raise ValueError(f"店铺不存在或无法访问：{message or '未知错误'}")
        return data

    def shop_name(self, token: str) -> str:
        return str(self.shop_info(token).get("nickname") or token)

    def category_ids(self, token: str, goods_type: str) -> list[int]:
        """读取店铺分类 ID；分类上下文是公开接口返回真实库存所必需的。"""
        payload = self._post(
            "/shopApi/Shop/categoryList",
            {"token": token, "goods_type": goods_type, "category_key": ""},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("code") != 1 or not isinstance(data, list):
            return []

        category_ids: list[int] = []
        seen: set[int] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            category_id = to_int(item.get("id"), -1)
            if category_id < 0 or category_id in seen:
                continue
            seen.add(category_id)
            category_ids.append(category_id)
        return category_ids

    def item_status(self, goods_key: str) -> tuple[ProductStatus, Optional[dict]]:
        """实时确认零售商品是否仍上架。

        goodsInfo 对未上架商品返回 code=0、data=null；其他接口错误不能误判
        为下架，因此会抛出异常并保留数据库原状态。
        """
        key = str(goods_key or "").removeprefix("r:").strip()
        if not key:
            raise ValueError("商品 goods_key 为空")

        payload = self._post(
            "/shopApi/Shop/goodsInfo",
            {"goods_key": key, "trade_no": ""},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("商品详情接口返回非预期格式")

        data = payload.get("data")
        if payload.get("code") == 1 and isinstance(data, dict):
            listed = to_int(data.get("status"), 1) == 1
            return (ProductStatus.NORMAL if listed else ProductStatus.OFF), data

        message = str(payload.get("msg") or "")
        if any(marker in message for marker in SHOP_CLOSED_MARKERS):
            raise ShopClosedError(message)
        if any(marker in message for marker in UNLISTED_MARKERS):
            return ProductStatus.OFF, None
        raise RuntimeError(f"商品详情接口失败：{message or payload.get('code')}")

    def item_record(self, value: str) -> tuple[str, ProductRecord] | None:
        """读取一个公开商品，并返回它所属的店铺 token 与归一化商品。"""
        key = item_url_key(value) or str(value or "").removeprefix("r:").strip()
        if not _safe_public_key(key):
            return None
        status, data = self.item_status(key)
        if status != ProductStatus.NORMAL or not isinstance(data, dict):
            return None

        user = data.get("user") or {}
        token = shop_token(str(user.get("link") or user.get("token") or ""))
        if not token:
            return None
        name = str(user.get("nickname") or token)
        return token, self._map(data, name)

    def category_records(
        self,
        token: str,
        category_id: int,
        keywords: str,
        goods_type: str = "",
        *,
        shop_name: str = "",
        preferred_goods_type: str = "",
        exact_name: str = "",
        max_pages: int = 1,
    ) -> list[ProductRecord]:
        """用已知原店分类直接读取当前商品；热路径只需要一次 goodsList。"""
        resolved_token = shop_token(token)
        if not resolved_token or category_id < 0:
            return []
        selected_type = (
            _API_GOODS_TYPE.get(goods_type)
            or _API_GOODS_TYPE.get(preferred_goods_type)
            or "card"
        )
        terms = [" ".join((keywords or "").split())]
        normalized_exact = " ".join((exact_name or "").split())
        if normalized_exact and normalized_exact.casefold() not in {
            term.casefold() for term in terms if term
        }:
            terms.append(normalized_exact)

        records: dict[str, ProductRecord] = {}
        for term_index, term in enumerate(term for term in terms if term):
            current = 1
            expected_total = 0
            category_seen: set[str] = set()
            while current <= max(1, max_pages):
                payload = self._post(
                    "/shopApi/Shop/goodsList",
                    {
                        "token": resolved_token,
                        "keywords": term,
                        "category_id": category_id,
                        "goods_type": selected_type,
                        "current": current,
                        "pageSize": 50,
                    },
                )
                if not isinstance(payload, dict) or payload.get("code") != 1:
                    message = (
                        payload.get("msg")
                        if isinstance(payload, dict)
                        else "非 JSON 响应"
                    )
                    raise RuntimeError(f"店铺分类商品搜索接口失败：{message}")
                payload_data = payload.get("data") or {}
                items = payload_data.get("list") or []
                expected_total = max(
                    expected_total,
                    to_int(payload_data.get("total"), 0),
                )
                if not items:
                    break
                before = len(category_seen)
                for item in items:
                    record = self._map(item, shop_name or resolved_token)
                    category_seen.add(record.external_id)
                    records[record.external_id] = record
                if len(category_seen) == before or len(category_seen) >= expected_total:
                    break
                current += 1

            # 宽词已召回就不再用长标题重复请求。
            if records or term_index + 1 >= len(terms):
                break
        return list(records.values())

    def category_records_for_item(
        self,
        value: str,
        keywords: str,
        goods_type: str = "",
        *,
        max_pages: int = 1,
    ) -> tuple[str, list[ProductRecord]] | None:
        """由商品详情取得原店分类，再在该分类中读取当前价格和真实库存。

        ``goodsList`` 不带 ``category_id`` 时，不少店会把真实库存统一返回 0。
        PickAI 已经给了具体 item URL，因此没必要先盲查店铺：先用一次
        ``goodsInfo`` 取得 token/category_id，再带分类宽词查询，通常两次请求
        就能同时核验该候选和同分类的其他当前商品。
        """
        key = item_url_key(value) or str(value or "").removeprefix("r:").strip()
        if not _safe_public_key(key):
            return None
        status, data = self.item_status(key)
        if status != ProductStatus.NORMAL or not isinstance(data, dict):
            return None

        user = data.get("user") or {}
        token = shop_token(str(user.get("link") or user.get("token") or ""))
        if not token:
            return None
        shop_name = str(user.get("nickname") or token)
        selected_type = (
            _API_GOODS_TYPE.get(goods_type)
            or str(data.get("goods_type") or "card")
        )
        category = data.get("category")
        category = category if isinstance(category, dict) else {}
        category_id = to_int(category.get("id"), -1)
        if category_id < 0:
            # 极少数旧商品详情没有分类；保留兼容路径，但正常热路径不会走它。
            return token, self.search(
                token,
                keywords,
                goods_type,
                shop_name=shop_name,
                preferred_goods_type=selected_type,
                max_pages=max_pages,
                max_scoped_categories=1,
                skip_scoped_when_positive=True,
            )

        exact_name = " ".join(str(data.get("name") or "").split())
        return token, self.category_records(
            token,
            category_id,
            keywords,
            goods_type,
            shop_name=shop_name,
            preferred_goods_type=selected_type,
            exact_name=exact_name,
            max_pages=max_pages,
        )

    def search(
        self,
        target: str,
        keywords: str,
        goods_type: str = "",
        *,
        shop_name: Optional[str] = None,
        preferred_goods_type: str = "",
        max_pages: int = 4,
        max_scoped_categories: int = MAX_SCOPED_CATEGORIES_PER_SEARCH,
        skip_scoped_when_positive: bool = False,
    ) -> list[ProductRecord]:
        """在一个公开店铺内按关键词查询，供自动发现流程即时核验。

        已有货源结果能确定品类时只核验该品类，避免一次关键词搜索对每个候选店
        连发四类请求；后台完整索引仍会抓取全部品类。
        """
        token = shop_token(target)
        raw = " ".join((keywords or "").split())
        if not token or not raw:
            return []

        selected_type = _API_GOODS_TYPE.get(goods_type)
        preferred_type = _API_GOODS_TYPE.get(preferred_goods_type)
        if selected_type:
            primary_types = [selected_type]
            remaining_types: list[str] = []
        elif preferred_type:
            primary_types = [preferred_type]
            remaining_types = []
        else:
            primary_types = GOODS_TYPES
            remaining_types = []
        name = shop_name or token
        records: dict[str, ProductRecord] = {}

        def search_term(term: str, search_types: list[str]) -> None:
            for gt in search_types:
                current = 1
                category_seen: set[str] = set()
                category_hits: dict[int, int] = {}
                expected_total = 0
                while current <= max_pages:
                    payload = self._post(
                        "/shopApi/Shop/goodsList",
                        {
                            "token": token,
                            "keywords": term,
                            "goods_type": gt,
                            "current": current,
                            "pageSize": 50,
                        },
                    )
                    if not isinstance(payload, dict) or payload.get("code") != 1:
                        message = payload.get("msg") if isinstance(payload, dict) else "非 JSON 响应"
                        raise RuntimeError(f"店铺商品搜索接口失败：{message}")
                    data = payload.get("data") or {}
                    items = data.get("list") or []
                    expected_total = max(expected_total, to_int(data.get("total"), 0))
                    if not items:
                        break

                    before = len(category_seen)
                    for item in items:
                        record = self._map(item, name)
                        category_seen.add(record.external_id)
                        records.setdefault(record.external_id, record)
                        category = item.get("category")
                        if isinstance(category, dict):
                            category_id = to_int(category.get("id"), -1)
                            if category_id >= 0:
                                category_hits[category_id] = category_hits.get(category_id, 0) + 1
                    if len(category_seen) == before or len(category_seen) >= expected_total:
                        break
                    current += 1

                # goodsList 在缺少 category_id 时会把部分真实库存统一返回为 0。
                # 用关键词首轮返回的分类 ID 再查一次，覆盖成店铺页面实际展示的库存。
                scoped_category_ids = [
                    category_id
                    for category_id, _count in sorted(
                        category_hits.items(),
                        key=lambda entry: entry[1],
                        reverse=True,
                    )[:max(0, max_scoped_categories)]
                ]
                # 无 category_id 时返回的正库存本身可信；问题只是部分店会把
                # 实际有货误报成 0。极速搜索一旦已经拿到正库存，就不再为了
                # 重复确认而多发一轮请求，常见有货店可由两次请求降为一次。
                if skip_scoped_when_positive and any(
                    record.category == GT_CAT.get(gt)
                    and record.status == ProductStatus.NORMAL
                    and record.stock > 0
                    for record in records.values()
                ):
                    scoped_category_ids = []
                for category_id in scoped_category_ids:
                    scoped_current = 1
                    scoped_seen: set[str] = set()
                    scoped_total = 0
                    while scoped_current <= max_pages:
                        payload = self._post(
                            "/shopApi/Shop/goodsList",
                            {
                                "token": token,
                                "keywords": term,
                                "category_id": category_id,
                                "goods_type": gt,
                                "current": scoped_current,
                                "pageSize": 50,
                            },
                        )
                        if not isinstance(payload, dict) or payload.get("code") != 1:
                            break
                        data = payload.get("data") or {}
                        items = data.get("list") or []
                        scoped_total = max(scoped_total, to_int(data.get("total"), 0))
                        if not items:
                            break
                        before = len(scoped_seen)
                        for item in items:
                            record = self._map(item, name)
                            scoped_seen.add(record.external_id)
                            records[record.external_id] = record
                        if len(scoped_seen) == before or len(scoped_seen) >= scoped_total:
                            break
                        scoped_current += 1

        search_term(raw, primary_types)
        if not records and remaining_types:
            search_term(raw, remaining_types)
        # 店铺接口对空格较敏感；原词零命中时用短前缀召回，再由业务层严格过滤。
        compact = re.sub(r"\s+", "", raw)
        fallback = compact[:3] if len(compact) >= 4 else ""
        if not records and fallback and fallback.casefold() != raw.casefold():
            search_term(fallback, primary_types)
            if not records and remaining_types:
                search_term(fallback, remaining_types)

        # 无结果的候选无需额外读取店铺资料；有结果时再补全可读商家名。
        if records and not shop_name:
            try:
                resolved_name = self.shop_name(token)
            except Exception:  # noqa: BLE001 - 商品结果仍可使用 token 作为店名
                resolved_name = token
            for record in records.values():
                record.merchant_name = resolved_name
        return list(records.values())

    def fetch(
        self,
        target: Optional[str] = None,
        max_pages: Optional[int] = None,
        shop_name: Optional[str] = None,
    ) -> list[ProductRecord]:
        token = shop_token(target or "")
        if not token:
            return []
        info = self.shop_info(token)
        name = shop_name or str(info.get("nickname") or token)
        if all(key in info for key in GOODS_TYPE_COUNT_KEYS.values()):
            goods_types = [
                goods_type
                for goods_type, count_key in GOODS_TYPE_COUNT_KEYS.items()
                if to_int(info.get(count_key), 0) > 0
            ]
            # 某些旧店资料的分类型计数会暂时为 0，但总商品数仍为正。
            if not goods_types and to_int(info.get("goods_count"), 0) > 0:
                goods_types = GOODS_TYPES
        else:
            goods_types = GOODS_TYPES
        out: list[ProductRecord] = []
        seen: set[str] = set()
        for gt in goods_types:
            category_ids = self.category_ids(token, gt)
            scopes: list[int | None] = category_ids or [None]
            for category_id in scopes:
                current = 1
                category_seen: set[str] = set()
                expected_total = 0
                while True:
                    body = {
                        "token": token,
                        "keywords": "",
                        "goods_type": gt,
                        "current": current,
                        "pageSize": 50,
                    }
                    if category_id is not None:
                        body["category_id"] = category_id
                    d = self._post("/shopApi/Shop/goodsList", body)
                    if not isinstance(d, dict) or d.get("code") != 1:
                        message = d.get("msg") if isinstance(d, dict) else "非 JSON 响应"
                        raise RuntimeError(f"店铺商品列表接口失败：{message}")
                    data = d.get("data") or {}
                    items = data.get("list") or []
                    expected_total = max(expected_total, to_int(data.get("total"), 0))
                    if not items:
                        if len(category_seen) >= expected_total:
                            break
                        raise RuntimeError(
                            f"店铺 {gt} 商品第 {current} 页提前为空："
                            f"应有 {expected_total} 条，实际仅 {len(category_seen)} 条"
                        )
                    before = len(category_seen)
                    for it in items:
                        r = self._map(it, name)
                        category_seen.add(r.external_id)
                        if r.external_id in seen:
                            continue
                        seen.add(r.external_id)
                        out.append(r)
                    if len(category_seen) == before:
                        raise RuntimeError(
                            f"店铺 {gt} 商品第 {current} 页重复，无法形成完整快照"
                        )
                    if len(category_seen) >= expected_total:
                        break
                    if max_pages is not None and current >= max_pages:
                        raise RuntimeError(
                            f"店铺商品超过抓取上限（{max_pages} 页），未形成完整快照"
                        )
                    current += 1
        return out

    @staticmethod
    def _map(item: dict, shop_name: str) -> ProductRecord:
        gt = str(item.get("goods_type") or "")
        listed = to_int(item.get("status"), 1) == 1
        extend = item.get("extend")
        extend = extend if isinstance(extend, dict) else {}
        raw_stock = item.get("stock_count")
        if raw_stock is None and "stock_count" in extend:
            raw_stock = extend.get("stock_count")
        stock_known = raw_stock is not None and str(raw_stock).strip() != ""
        stock = max(0, to_int(raw_stock, 0)) if stock_known else -1
        if not listed:
            status = ProductStatus.OFF
            stock = 0
        elif stock_known and stock == 0:
            status = ProductStatus.OUT
        else:
            status = ProductStatus.NORMAL
        return ProductRecord(
            external_id="r:" + str(item.get("goods_key") or item.get("link") or item.get("name")),
            name=str(item.get("name") or "未命名商品"),
            category=GT_CAT.get(gt, Category.OTHER),
            merchant_name=shop_name,
            sale_price=to_float(item.get("price")),
            agent_price=0.0,
            cost_price=0.0,
            stock=stock,
            status=status,
            is_linked=False,
            url=str(item.get("link") or ""),
            raw=item,
        )
