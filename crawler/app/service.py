"""商品完整抓取、搜索范围隔离与当前结果缓存。"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    wait,
)
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import Integer, and_, func, or_, text, update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from .crawler.base import ProductRecord
from .crawler.pickai_catalog import (
    PICKAI_SHOP_NAME,
    PickAICatalog,
    PickAISnapshot,
    is_chatgpt_plus_product_name,
    is_email_product_name,
    is_k12_product_name,
    is_openai_sms_product_name,
    strict_realtime_scope_for_query,
    strict_type_names_for_query,
)
from .crawler.reference_catalog import REFERENCE_STORE_TOKENS, ReferenceCatalog
from .crawler.retail_discovery import RetailDiscovery
from .crawler.session import BlockedError, JsonChallengeError, host_cooldown_remaining
from .crawler.shop_api import (
    RETAIL_BASE,
    ShopApi,
    ShopClosedError,
    item_url_key,
    shop_token,
    shop_url_token,
)
from .crawler.source_square import SourceSquare
from .config import settings
from .models import (
    Category,
    Product,
    ProductStatus,
    Shop,
    SourceKind,
)


LIVE_SHOP_NAME = "货源广场 · 实时搜索"
CATFK_LIVE_SHOP_NAME = "云猫寄售 · 实时搜索"
MAX_DISCOVERED_SHOPS_PER_SEARCH = 8
MAX_CHATGPT_SHOPS_PER_SEARCH = 4
STRICT_REALTIME_SHOPS_PER_SEARCH = 8
RECENT_RETAIL_SHOPS_PER_SEARCH = 1
LOW_PRICE_RETAIL_SHOPS_PER_SEARCH = 2
# 前台搜索只即时复核少量下架货源链接；完整零售覆盖由后台店铺索引负责，
# 避免一个关键词因几十个 goodsInfo 串行节流而等待近一分钟。
MAX_RETAIL_ITEM_CHECKS_PER_SEARCH = 3
MAX_MANUAL_SHOPS_PER_SEARCH = 4
# 零售实时核验的墙钟预算。索引变大或站点风控时，逐店 goodsList 会被全局
# 节流串行拖慢；超过预算就先返回已核验到的结果，避免前台一直转圈。
RETAIL_VERIFY_BUDGET_S = 9.0
REALTIME_ORIGIN_TIMEOUT_S = 5.0
# 第一次见到商品时需要 goodsInfo + 分类 goodsList；分类 ID 学习落库后每店只需
# 一次 goodsList。两个 worker 在全局主机节流下交错执行，7.5 秒足够覆盖至少
# 两家低价店，而不是旧版拿到第一家的一条商品便立即停止。
STRICT_REALTIME_VERIFY_BUDGET_S = 7.5
STRICT_REALTIME_ORIGIN_TIMEOUT_S = 4.0
STRICT_REALTIME_MIN_RESULTS = 6
STRICT_REALTIME_MIN_SHOPS = 2
USER_SEARCH_VERIFY_WAIT_S = 2.0
SOURCE_STOCK_FRESHNESS = timedelta(minutes=10)
PICKAI_SNAPSHOT_FRESHNESS = timedelta(hours=6)
RETAIL_STOCK_FRESHNESS = timedelta(minutes=2)
PICKAI_ORIGIN_VERIFICATION_FRESHNESS = timedelta(seconds=90)
# 搜索页不再等联网结果才显示，因此后台可以多核对几个低价候选。
# 仍设上限，避免一个宽泛关键词对原店连发几十个请求。
MAX_PICKAI_ORIGIN_CHECKS_PER_SEARCH = 20
_LIVE_RETAIL_VERIFY_LOCK = threading.Lock()
_STRICT_SEARCH_STATE_LOCK = threading.Lock()
_ACTIVE_STRICT_SEARCH: threading.Event | None = None
_GOODS_TYPE_BY_CATEGORY = {
    Category.CARD: "card",
    Category.KNOWLEDGE: "knowledge",
    Category.RESOURCE: "resource",
    Category.RIGHTS: "equity",
}
_CATEGORY_BY_GOODS_TYPE = {goods_type: category for category, goods_type in _GOODS_TYPE_BY_CATEGORY.items()}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _begin_strict_search() -> threading.Event:
    """让最新一次快捷搜索取代仍在排队的旧搜索。"""
    global _ACTIVE_STRICT_SEARCH
    event = threading.Event()
    with _STRICT_SEARCH_STATE_LOCK:
        if _ACTIVE_STRICT_SEARCH is not None:
            _ACTIVE_STRICT_SEARCH.set()
        _ACTIVE_STRICT_SEARCH = event
    return event


def _finish_strict_search(event: threading.Event) -> None:
    global _ACTIVE_STRICT_SEARCH
    with _STRICT_SEARCH_STATE_LOCK:
        if _ACTIVE_STRICT_SEARCH is event:
            _ACTIVE_STRICT_SEARCH = None


def _origin_shop_api(
    *,
    timeout_s: float | None = None,
    deadline_monotonic: float | None = None,
    cancel_event: threading.Event | None = None,
) -> ShopApi:
    """创建匿名原店会话；普通商品核验绝不携带商家账号凭据。

    Merchant-Token 只用于用户明确配置的 ``www.ldxp.cn/merchantApi``
    官方货源接口。公开 ``pay.ldxp.cn/shopApi`` 即使遇到滑块也不会自动
    升级为账号会话，避免把高频零售核验风险关联到用户账号。
    """
    if timeout_s is None and deadline_monotonic is None:
        return ShopApi()
    from .crawler.session import Fetcher

    fetcher = Fetcher(
        credential_policy="public",
        base_url=RETAIL_BASE,
        merchant_token="",
        merchant_referer=f"{RETAIL_BASE}/",
        timeout_s=timeout_s,
        deadline_monotonic=deadline_monotonic,
        cancel_event=cancel_event,
    )
    try:
        return ShopApi(fetcher)
    except TypeError:
        # 单元测试和第三方适配器可能提供无参 ShopApi 兼容实现。
        fetcher.close()
        return ShopApi()


def _ensure_retail_shop(
    session: Session,
    token: str,
    name: str,
    *,
    note: str = "自动发现的公开零售店",
) -> Shop:
    folded_token = token.casefold()
    existing = next(
        (
            shop
            for shop in session.exec(
                select(Shop).where(Shop.kind == SourceKind.PUBLIC_SHOP)
            ).all()
            if shop.url.casefold() == folded_token
        ),
        None,
    )
    display_name = (name or token).strip()
    if existing:
        changed = False
        if display_name and existing.name != display_name:
            existing.name = display_name
            changed = True
        if note and existing.note != note:
            existing.note = note
            changed = True
        if changed:
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    shop = Shop(
        name=display_name,
        kind=SourceKind.PUBLIC_SHOP,
        url=token,
        note=note,
    )
    session.add(shop)
    session.commit()
    session.refresh(shop)
    return shop


def add_retail_shop(
    session: Session,
    url_or_token: str,
    source: ShopApi | None = None,
) -> Shop:
    """保存一个已知公开零售店；关键词搜索不依赖此操作。"""
    token = shop_token(url_or_token)
    if not token:
        raise ValueError("无效的店铺地址")
    owns_source = source is None
    api = source or _origin_shop_api()
    try:
        name = api.shop_name(token)
    finally:
        if owns_source:
            api.fetcher.close()
    return _ensure_retail_shop(session, token, name, note="公开零售店")


def sync_retail_shop(session: Session, shop: Shop, source: ShopApi) -> set[str]:
    """用已校验的店铺会话形成完整商品快照。"""
    records = source.fetch(shop.url, shop_name=shop.name)
    ingest(session, shop, records, complete_snapshot=True)
    return {record.external_id for record in records}


def get_or_create_live_shop(
    session: Session,
    name: str = LIVE_SHOP_NAME,
    base_url: str = "",
) -> Shop:
    """实时搜索到的商品统一挂在这个缓存店铺下。"""
    shop = session.exec(
        select(Shop).where(
            Shop.name == name,
            Shop.kind == SourceKind.SOURCE_SQUARE,
        )
    ).first()
    if shop is None:
        shop = Shop(
            name=name,
            kind=SourceKind.SOURCE_SQUARE,
            url=base_url,
            note="实时搜索结果",
        )
        session.add(shop)
        session.commit()
        session.refresh(shop)
    elif base_url and shop.url != base_url:
        shop.url = base_url
        session.add(shop)
        session.commit()
        session.refresh(shop)
    return shop


def _search_variants(src: SourceSquare, keywords: str, goods_type: str = "") -> list[ProductRecord]:
    """空格无关的召回：接口按空格切词做子串 AND 匹配，"bugteam" 与 "bug team" 结果不同。
    这里查多个变体（原词 + 短前缀跨空格召回）合并，再用「去空格」子串在客户端过滤保证相关。"""
    raw = keywords.strip()
    concat = re.sub(r"\s+", "", raw).lower()
    tokens = [t.lower() for t in raw.split() if t]

    terms: list[str] = []
    for t in (raw, concat[:3] if len(concat) >= 4 else ""):
        t = t.strip()
        if t and t.lower() not in {x.lower() for x in terms}:
            terms.append(t)

    by_id: dict[str, ProductRecord] = {}
    # 每个平台每个关键词变体最多请求一页。即使结果超过 200 条，也不再为了
    # “抓全”连续翻页；前台只需展示最相关的一页，账号安全优先。
    for term in terms[:2]:
        recs, _ = src.search(term, goods_type, current=1, page_size=200)
        for r in recs:
            by_id.setdefault(r.external_id, r)

    def relevant(r: ProductRecord) -> bool:
        n = r.name.lower()
        nc = re.sub(r"\s+", "", n)
        if concat and concat in nc:
            return True
        if len(tokens) > 1 and all(tk in n for tk in tokens):
            return True
        return False

    return [r for r in by_id.values() if relevant(r)]


_CATFK_CANONICAL_TERMS = {
    "k12": "K12",
    "gpt": "GPT",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "midjourney": "Midjourney",
    "codex": "Codex",
    "outlook": "Outlook",
}


def _catfk_search_keywords(keywords: str) -> str:
    """云猫货源搜索区分 ASCII 大小写，统一常见产品名以避免小写零召回。"""
    parts = re.split(r"(\s+)", keywords.strip())
    normalized: list[str] = []
    for part in parts:
        folded = part.casefold()
        if folded in _CATFK_CANONICAL_TERMS:
            normalized.append(_CATFK_CANONICAL_TERMS[folded])
        elif (
            re.fullmatch(r"[a-zA-Z0-9._-]+", part)
            and any(character.isalpha() for character in part)
            and any(character.isdigit() for character in part)
        ):
            normalized.append(part.upper())
        else:
            normalized.append(part)
    return "".join(normalized)


_CAT_BY_GOODS_TYPE = {"card": "卡密", "knowledge": "知识", "resource": "资源", "equity": "权益"}


def _text_matches(name: str, merchant_name: str, keywords: str) -> bool:
    concat = re.sub(r"\s+", "", keywords).strip().lower()
    tokens = [t for t in keywords.lower().split() if t]
    searchable = f"{name} {merchant_name}".lower()
    compact_searchable = re.sub(r"\s+", "", searchable)
    return (
        (bool(concat) and concat in compact_searchable)
        or (len(tokens) > 1 and all(token in searchable for token in tokens))
        or (len(tokens) == 1 and tokens[0] in searchable)
    )


def _matches_search(p: Product, keywords: str, goods_type: str) -> bool:
    relevant = _text_matches(p.name, p.merchant_name, keywords)
    category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
    return relevant and (not category or p.category.value == category)


def _record_matches(record: ProductRecord, keywords: str, goods_type: str) -> bool:
    relevant = _text_matches(record.name, record.merchant_name, keywords)
    category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
    return relevant and (not category or record.category.value == category)


def _pickai_standard_name(name: str) -> str:
    return name.split(" · ", 1)[0].strip() if " · " in name else ""


def _pickai_raw_name(name: str) -> str:
    return name.split(" · ", 1)[1].strip() if " · " in name else name.strip()


def _strict_catalog_urls(session: Session, keywords: str) -> set[str]:
    """返回当前严格分类允许进入原店核验的商品 URL。"""
    scope = strict_realtime_scope_for_query(keywords)
    if scope is None:
        return set()
    if scope == "k12":
        candidates = session.exec(
            select(Product).where(func.lower(Product.name).contains("k12"))
        ).all()
        return {
            product.url
            for product in candidates
            if product.url and is_k12_product_name(product.name)
        }

    type_names = strict_type_names_for_query(keywords)
    if not type_names:
        return set()
    clauses = [Product.name.startswith(f"{name} · ") for name in type_names]
    return {
        url
        for url in session.exec(select(Product.url).where(or_(*clauses))).all()
        if url
    }


# 保留旧私有名，避免外部脚本或历史测试直接引用时失效。
_chatgpt_catalog_urls = _strict_catalog_urls


def _strict_origin_keywords(keywords: str) -> str:
    """把标准分类名转换成原店实际会命中的短词。"""
    scope = strict_realtime_scope_for_query(keywords)
    compact = re.sub(r"\s+", "", (keywords or "").casefold())
    if scope == "chatgpt" and "plus" in compact:
        return "plus"
    if scope == "openai_sms":
        return "接码"
    if scope == "email":
        return "邮箱"
    return keywords


def _strict_product_name_matches(name: str, keywords: str) -> bool:
    scope = strict_realtime_scope_for_query(keywords)
    compact = re.sub(r"\s+", "", (keywords or "").casefold())
    if scope == "chatgpt" and "plus" in compact:
        return is_chatgpt_plus_product_name(name)
    if scope == "openai_sms":
        return is_openai_sms_product_name(name)
    if scope == "email":
        return is_email_product_name(name)
    if scope == "k12":
        return is_k12_product_name(name)
    return False


def _search_relevance_rank(product: Product, keywords: str) -> int:
    """PickAI 标准商品前缀精确命中优先于原始标题里的偶然关键词。"""
    if product.external_id.startswith("p:") and " · " in product.name:
        standard_name = product.name.split(" · ", 1)[0]
        standard_compact = re.sub(r"\s+", "", standard_name.casefold())
        query_compact = re.sub(r"\s+", "", keywords.strip().casefold())
        if query_compact and (
            standard_compact == query_compact
            or standard_compact.endswith(query_compact)
        ):
            return 0
        if _text_matches(standard_name, "", keywords):
            return 1
    return 2


def _available(p: Product) -> bool:
    """只有已上架且存在明确正库存的商品才可进入“有货”结果。"""
    return p.status == ProductStatus.NORMAL and p.stock > 0


def _record_available(record: ProductRecord) -> bool:
    return record.status == ProductStatus.NORMAL and record.stock > 0


def _product_platform(product: Product) -> str:
    host = (urlsplit(product.url).hostname or "").lower()
    return "catfk" if host == "catfk.com" or host.endswith(".catfk.com") else "ldxp"


def _retail_inventory_view(product: Product) -> Product:
    """过期的零售正库存只能视为未知，不能继续冒充实时有货。"""
    if (
        product.status == ProductStatus.NORMAL
        and product.stock > 0
        and _now() - product.last_seen_at > RETAIL_STOCK_FRESHNESS
    ):
        current = product.model_copy()
        current.stock = -1
        return current
    return product


def _mark_retail_item_unavailable(
    session: Session,
    item_key: str,
    *,
    shop_closed: bool,
) -> set[str]:
    """用 goodsInfo 的实时不可购买信号覆盖店铺列表里的残留库存。"""
    external_id = "r:" + str(item_key or "").removeprefix("r:").strip()
    retail_shop_ids = set(
        session.exec(select(Shop.id).where(Shop.kind == SourceKind.PUBLIC_SHOP)).all()
    )
    matched = [
        product
        for product in session.exec(
            select(Product).where(Product.external_id == external_id)
        ).all()
        if product.shop_id in retail_shop_ids
    ]
    if not matched:
        return set()

    now = _now()
    affected = matched
    closed_tokens: set[str] = set()
    if shop_closed:
        closed_shop_ids = {product.shop_id for product in matched}
        affected = session.exec(
            select(Product).where(Product.shop_id.in_(closed_shop_ids))
        ).all()
        for shop_id in closed_shop_ids:
            shop = session.get(Shop, shop_id)
            if shop is None:
                continue
            if shop.url:
                closed_tokens.add(shop.url)
            # 防止后台索引立即用仍有残留库存的 goodsList 覆盖打烊状态。
            shop.last_synced_at = now
            session.add(shop)

    for product in affected:
        product.stock = 0
        product.status = ProductStatus.OUT if shop_closed else ProductStatus.OFF
        product.last_seen_at = now
        session.add(product)
    session.commit()
    return closed_tokens


def _discover_retail_matches(
    session: Session,
    keywords: str,
    goods_type: str,
    source_records: list[ProductRecord],
    *,
    require_item_confirmation: bool = False,
    prioritize_source_records: bool = False,
    allowed_item_urls: set[str] | None = None,
    max_candidates: int = MAX_DISCOVERED_SHOPS_PER_SEARCH,
    request_timeout_s: float | None = None,
    max_scoped_categories: int = 6,
    stats: dict[str, int] | None = None,
    allow_web_discovery: bool = True,
    verify_budget_s: float = RETAIL_VERIFY_BUDGET_S,
    stop_after_first_available: bool = False,
    first_available_grace_s: float = 0.0,
    minimum_available_results: int = 1,
    minimum_available_shops: int = 1,
    prefer_available_candidates: bool = False,
    cancel_event: threading.Event | None = None,
) -> dict[int, set[str]]:
    """自动发现候选零售页，并向店铺接口核验当前关键词商品。"""
    if stats is not None:
        stats.clear()
        stats.update(
            candidate_count=0,
            item_count=0,
            network_task_count=0,
            available_result_count=0,
            available_shop_count=0,
        )
    if cancel_event is not None and cancel_event.is_set():
        return {}
    if host_cooldown_remaining(RETAIL_BASE) > 0:
        return {}
    discovered_shops: tuple[str, ...] = ()
    discovered_items: tuple[str, ...] = ()

    candidates: list[str] = []
    candidate_names: dict[str, str] = {}
    candidate_goods_types: dict[str, str] = {}
    candidate_keywords: dict[str, str] = {}
    candidate_item_urls: dict[str, str] = {}
    candidate_category_ids: dict[str, int] = {}
    seen_candidates: set[str] = set()

    def add_candidate(
        value: str,
        name: str = "",
        preferred_goods_type: str = "",
        preferred_keyword: str = "",
        preferred_item_url: str = "",
        preferred_category_id: int | None = None,
    ) -> None:
        token = shop_token(value)
        folded = token.casefold()
        if not token:
            return
        if folded in seen_candidates:
            if name and token not in candidate_names:
                candidate_names[token] = name
            if preferred_goods_type and token not in candidate_goods_types:
                candidate_goods_types[token] = preferred_goods_type
            if preferred_keyword and token not in candidate_keywords:
                candidate_keywords[token] = preferred_keyword
            if preferred_item_url and token not in candidate_item_urls:
                candidate_item_urls[token] = preferred_item_url
            if preferred_category_id is not None and token not in candidate_category_ids:
                candidate_category_ids[token] = preferred_category_id
            return
        if len(candidates) >= max(1, max_candidates):
            return
        seen_candidates.add(folded)
        candidates.append(token)
        if name:
            candidate_names[token] = name
        if preferred_goods_type:
            candidate_goods_types[token] = preferred_goods_type
        if preferred_keyword:
            candidate_keywords[token] = preferred_keyword
        if preferred_item_url:
            candidate_item_urls[token] = preferred_item_url
        if preferred_category_id is not None:
            candidate_category_ids[token] = preferred_category_id

    def add_source_candidates(limit: int | None = None) -> None:
        added = 0
        for record in sorted(
            source_records,
            key=lambda item: (
                item.sale_price if item.sale_price > 0 else float("inf"),
                item.merchant_name.casefold(),
            ),
        ):
            before = len(candidates)
            add_candidate(
                record.merchant_link,
                record.merchant_name,
                _GOODS_TYPE_BY_CATEGORY.get(record.category, ""),
                preferred_item_url=record.url,
            )
            if len(candidates) > before:
                added += 1
                if limit is not None and added >= limit:
                    break

    # 先收集全部本地匹配零售店。只取最低价前 50 条会让旧价较高的店永久
    # 挤不进候选（例如 LV9C7XJE 的缓存价 2.5，实时价已降到 1.0）。
    if allowed_item_urls:
        # 严格分类已有 PickAI URL 白名单时直接读取其原店映射。不能先走
        # _db_search 的 URL 去重：同一 URL 的 PickAI 聚合行会覆盖 PUBLIC_SHOP
        # 原店行，导致明明已收录 200+ 家店却得到 0 个候选。
        statement = (
            select(Product)
            .join(Shop, Product.shop_id == Shop.id)
            .where(
                Shop.kind == SourceKind.PUBLIC_SHOP,
                Shop.active.is_(True),
                Product.url.in_(allowed_item_urls),
            )
        )
        category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
        if category:
            statement = statement.where(Product.category == category)
        visible_products = session.exec(statement).all()
    else:
        visible_products, _ = _db_search(
            session,
            keywords,
            goods_type,
            current=1,
            page_size=0,
            in_stock_only=False,
        )

    pickai_priority_by_url: dict[str, Product] = {}
    if prefer_available_candidates and allowed_item_urls:
        pickai_rows = session.exec(
            select(Product)
            .join(Shop, Product.shop_id == Shop.id)
            .where(
                Shop.name == PICKAI_SHOP_NAME,
                Product.url.in_(allowed_item_urls),
            )
        ).all()
        for product in pickai_rows:
            previous = pickai_priority_by_url.get(product.url)
            if previous is None or (
                0 if _available(product) else 1,
                product.sale_price if product.sale_price > 0 else float("inf"),
            ) < (
                0 if _available(previous) else 1,
                previous.sale_price if previous.sale_price > 0 else float("inf"),
            ):
                pickai_priority_by_url[product.url] = product

    candidate_now = _now()

    def candidate_rank(product: Product) -> tuple[int, float, datetime]:
        # 本轮/最近一次原店已经明确缺货或返回库存未知时，先轮换其他店；
        # 否则用 PickAI 最新快照只做“可能有货”的候选排序，最终结果仍只认原店。
        recently_checked = (
            candidate_now - product.last_seen_at <= timedelta(minutes=5)
            and (
                product.inventory_verified_at is not None
                or product.stock < 0
            )
        )
        if recently_checked and not _available(product):
            return (4, float("inf"), product.last_seen_at)
        pickai = pickai_priority_by_url.get(product.url)
        if pickai is not None and _available(pickai):
            return (
                # PickAI 的库存只用于排核验顺序，绝不作为结果。库存 1～4
                # 最容易在聚合延迟期间已经卖完；先查库存 >=10 的同价店，
                # 可避免 tt小铺/小晨这类假有货挡住后面的真实低价店。
                0 if pickai.stock >= 10 else 1,
                pickai.sale_price if pickai.sale_price > 0 else float("inf"),
                product.last_seen_at,
            )
        return (
            2 if _available(product) else 3,
            product.sale_price if product.sale_price > 0 else float("inf"),
            product.last_seen_at,
        )

    retail_matches: dict[str, tuple[Shop, Product, datetime]] = {}
    for product in visible_products:
        if allowed_item_urls is not None and product.url not in allowed_item_urls:
            continue
        shop = session.get(Shop, product.shop_id)
        if shop is None or shop.kind != SourceKind.PUBLIC_SHOP:
            continue
        folded = shop.url.casefold()
        previous = retail_matches.get(folded)
        if previous is None:
            retail_matches[folded] = (shop, product, product.last_seen_at)
            continue
        if prefer_available_candidates:
            best_product = min(
                (previous[1], product),
                key=candidate_rank,
            )
        else:
            best_product = (
                product
                if product.sale_price > 0
                and (
                    previous[1].sale_price <= 0
                    or product.sale_price < previous[1].sale_price
                )
                else previous[1]
            )
        retail_matches[folded] = (
            shop,
            best_product,
            min(previous[2], product.last_seen_at),
        )

    # PickAI 会不断换商品链接，但商家通常还是同一家。内置快照可能认识
    # “牟利ai -> 2VWX76A4”这家店，却还没见过它今天刚上的 item URL。
    # 只按 URL 关联会退化成仅核验一两家旧店。这里用 PickAI 当前商家名
    # 匹配本地已经确认过的 PUBLIC_SHOP token；PickAI 仍然不能提供 token，
    # 也不能决定最终价格/库存，只负责把已知原店排进本轮候选。
    if prefer_available_candidates and pickai_priority_by_url:
        known_shops = session.exec(
            select(Shop).where(
                Shop.kind == SourceKind.PUBLIC_SHOP,
                Shop.active.is_(True),
            )
        ).all()
        shops_by_name: dict[str, Shop] = {}
        for known_shop in known_shops:
            normalized_name = re.sub(r"\s+", "", known_shop.name.casefold())
            if normalized_name:
                shops_by_name[normalized_name] = known_shop
        known_category_by_shop_id: dict[int, int] = {}
        known_shop_ids = [shop.id for shop in known_shops if shop.id is not None]
        if known_shop_ids:
            category_rows = session.exec(
                select(Product)
                .where(
                    Product.shop_id.in_(known_shop_ids),
                    Product.origin_category_id.is_not(None),
                )
                .order_by(Product.last_seen_at.desc())
            ).all()
            for product in category_rows:
                if (
                    product.origin_category_id is not None
                    and _strict_product_name_matches(product.name, keywords)
                ):
                    known_category_by_shop_id.setdefault(
                        product.shop_id,
                        product.origin_category_id,
                    )
        for pickai in sorted(
            pickai_priority_by_url.values(),
            key=candidate_rank,
        ):
            normalized_name = re.sub(
                r"\s+",
                "",
                pickai.merchant_name.casefold(),
            )
            known_shop = shops_by_name.get(normalized_name)
            if known_shop is None:
                continue
            add_candidate(
                known_shop.url,
                known_shop.name,
                _GOODS_TYPE_BY_CATEGORY.get(pickai.category, ""),
                _pickai_raw_name(pickai.name),
                pickai.url,
                known_category_by_shop_id.get(known_shop.id or 0),
            )

    # 参考目录存在时先放两个最低价候选，让只有两个网络 worker 时也能尽快
    # 产出结果；随后穿插两个用户手动店铺，再用目录候选补满剩余名额。
    if prioritize_source_records:
        add_source_candidates(limit=2)

    # 快捷实时搜索先打“历史上最近明确有货且价格最低”的原店。目录只负责
    # 排序，不作为结果；这样有限的 2 个网络名额不会被刚核验过的缺货店占掉。
    if prefer_available_candidates:
        for shop, product, _last_seen_at in sorted(
            retail_matches.values(),
            key=lambda entry: candidate_rank(entry[1]),
        ):
            current_pickai = pickai_priority_by_url.get(product.url)
            add_candidate(
                shop.url,
                shop.name,
                _GOODS_TYPE_BY_CATEGORY.get(product.category, ""),
                _pickai_raw_name(current_pickai.name)
                if current_pickai is not None
                else product.name,
                current_pickai.url if current_pickai is not None else product.url,
                product.origin_category_id,
            )

    manual_matches = sorted(
        (
            entry
            for entry in retail_matches.values()
            if entry[0].note == "公开零售店"
        ),
        key=lambda entry: entry[0].created_at,
        reverse=True,
    )
    manual_limit = 2 if prioritize_source_records else MAX_MANUAL_SHOPS_PER_SEARCH
    for shop, product, _ in manual_matches[:manual_limit]:
        add_candidate(
            shop.url,
            shop.name,
            _GOODS_TYPE_BY_CATEGORY.get(product.category, ""),
            preferred_category_id=product.origin_category_id,
        )

    # 聚合目录只负责提供候选，剩余容量继续按价格顺序补齐。
    if prioritize_source_records:
        add_source_candidates()

    # 先核验索引里确实命中关键词的零售店。货源广场本身已经会返回商品，
    # 若让它携带的 merchant_link 先占满全部名额，真正的 shop/ 独有商品
    # （例如 LV9C7XJE 的 K12）会永远进不了本次实时结果。
    recent_matches = sorted(
        (
            entry
            for entry in retail_matches.values()
            if entry[0].last_synced_at is not None
        ),
        key=lambda entry: entry[0].last_synced_at or datetime.min,
        reverse=True,
    )
    for shop, product, _ in recent_matches[:RECENT_RETAIL_SHOPS_PER_SEARCH]:
        add_candidate(
            shop.url,
            shop.name,
            _GOODS_TYPE_BY_CATEGORY.get(product.category, ""),
            preferred_category_id=product.origin_category_id,
        )

    # 剩余名额保留少量历史低价店，并优先核验最久未更新的店。核验后
    # last_seen_at 会前移，后续自动刷新会轮换到其他店，不再永远只请求同一批。
    low_price_matches = sorted(
        retail_matches.values(),
        key=lambda entry: (
            0 if _available(entry[1]) else 1,
            entry[1].sale_price if entry[1].sale_price > 0 else float("inf"),
            entry[2],
        ),
    )
    for shop, product, _ in low_price_matches[:LOW_PRICE_RETAIL_SHOPS_PER_SEARCH]:
        add_candidate(
            shop.url,
            shop.name,
            _GOODS_TYPE_BY_CATEGORY.get(product.category, ""),
            preferred_category_id=product.origin_category_id,
        )

    # 索引命中店保留名额后，再用货源搜索项自带的 user.link 补充候选。
    # 没有本地命中时，这些实时来源仍可使用全部候选容量。
    if not prioritize_source_records:
        add_source_candidates()

    # 最后用最久未更新的匹配店补满剩余名额，令高价旧快照也有轮换机会。
    stale_matches = sorted(
        retail_matches.values(),
        key=lambda entry: (
            entry[2],
            entry[1].sale_price if entry[1].sale_price > 0 else float("inf"),
        ),
    )
    for shop, product, _ in stale_matches:
        add_candidate(
            shop.url,
            shop.name,
            _GOODS_TYPE_BY_CATEGORY.get(product.category, ""),
            preferred_category_id=product.origin_category_id,
        )

    visible_item_urls = [
        retail_matches[token.casefold()][1].url
        for token in candidates
        if token.casefold() in retail_matches
        and retail_matches[token.casefold()][1].url
    ]

    # 货源接口和已知零售店已经给出候选时，不再等待外部网页搜索。
    # DuckDuckGo 在国内网络经常等到超时，曾单独拖慢实时搜索约 50 秒。
    if not candidates and allow_web_discovery:
        discovery = RetailDiscovery()
        try:
            result = discovery.discover(keywords)
            discovered_shops = result.shop_tokens
            discovered_items = result.item_keys
        except Exception:  # noqa: BLE001 - 公开索引故障时仍返回货源广场结果
            pass
        finally:
            discovery.close()
    for token in discovered_shops:
        add_candidate(token)

    item_keys: list[str] = []
    seen_item_keys: set[str] = set()

    def add_item_key(value: str) -> None:
        key = item_url_key(value) or str(value or "").strip()
        folded = key.casefold()
        if (
            not key
            or folded in seen_item_keys
            or len(item_keys) >= MAX_RETAIL_ITEM_CHECKS_PER_SEARCH
        ):
            return
        seen_item_keys.add(folded)
        item_keys.append(key)

    # 当前页已有的正库存也必须经过 goodsInfo 核验。店铺打烊时 goodsList 仍可能
    # 返回旧 stock_count，仅依赖列表会把不可购买商品错误显示为“有货”。
    for item_url in visible_item_urls:
        add_item_key(item_url)
    for item_key in discovered_items:
        add_item_key(item_key)
    # 货源接口会返回库存为 0 的商品；goodsInfo 只能确认零售页是否仍上架，
    # 不能证明存在可购买库存。这里核验链接状态，但仍保留“库存未知”语义。
    for record in source_records:
        if not _record_available(record):
            add_item_key(record.url)

    # 快捷严格模式的候选已经通过店铺 goodsList 取得价格和库存；再并发跑旧链接
    # goodsInfo 既不增加库存可信度，又会在无货时白白占满剩余墙钟预算。
    if stop_after_first_available:
        item_keys.clear()

    if stats is not None:
        stats.update(candidate_count=len(candidates), item_count=len(item_keys))
    if not candidates and not item_keys:
        return {}

    source_goods_types = {
        _GOODS_TYPE_BY_CATEGORY[record.category]
        for record in source_records
        if record.category in _GOODS_TYPE_BY_CATEGORY
    }
    preferred_goods_type = (
        next(iter(source_goods_types)) if len(source_goods_types) == 1 else ""
    )
    buckets: dict[str, dict[str, ProductRecord]] = defaultdict(dict)
    verified_scopes: dict[int, set[str]] = defaultdict(set)
    confirmed_items: dict[str, set[str]] = defaultdict(set)
    verify_budget_s = max(0.1, float(verify_budget_s))
    request_deadline = time.monotonic() + verify_budget_s
    worker_cancel_event = cancel_event or threading.Event()
    origin_keywords = (
        _strict_origin_keywords(keywords)
        if allowed_item_urls is not None
        else keywords
    )

    def record_matches_effective_scope(record: ProductRecord) -> bool:
        strict_scope = strict_realtime_scope_for_query(keywords)
        if strict_scope is not None and allowed_item_urls is not None:
            category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
            return _strict_product_name_matches(record.name, keywords) and (
                not category or record.category.value == category
            )
        return _record_matches(record, keywords, goods_type)

    def check_item_batch(
        batch: list[str],
    ) -> tuple[list[tuple[str, ProductRecord]], list[str], list[str]]:
        item_api = _origin_shop_api(
            timeout_s=request_timeout_s,
            deadline_monotonic=request_deadline,
            cancel_event=worker_cancel_event,
        )
        found_items: list[tuple[str, ProductRecord]] = []
        unlisted_items: list[str] = []
        closed_shop_items: list[str] = []
        try:
            for item_key in batch:
                try:
                    found = item_api.item_record(item_key)
                except ShopClosedError:
                    closed_shop_items.append(item_key)
                except Exception:  # noqa: BLE001 - 单个失效链接不影响其他候选
                    continue
                else:
                    if found is not None:
                        found_items.append(found)
                    else:
                        unlisted_items.append(item_key)
        finally:
            item_api.fetcher.close()
        return found_items, unlisted_items, closed_shop_items

    expansion_lock = threading.Lock()
    expansion_claimed = False

    def search_candidate(token: str) -> tuple[str, list[ProductRecord]]:
        nonlocal expansion_claimed
        candidate_api = _origin_shop_api(
            timeout_s=request_timeout_s,
            deadline_monotonic=request_deadline,
            cancel_event=worker_cancel_event,
        )
        try:
            # 成功核验过的店已经把原店 category_id 落库。后续搜索先直接查
            # 分类 goodsList：每店一请求即可同时拿到当前价格和真实库存。
            cached_category_id = candidate_category_ids.get(token)
            category_reader = getattr(candidate_api, "category_records", None)
            if (
                stop_after_first_available
                and cached_category_id is not None
                and callable(category_reader)
            ):
                try:
                    cached_records = category_reader(
                        token,
                        cached_category_id,
                        origin_keywords,
                        goods_type,
                        shop_name=candidate_names.get(token, ""),
                        preferred_goods_type=(
                            candidate_goods_types.get(token) or preferred_goods_type
                        ),
                        max_pages=1,
                    )
                except (BlockedError, JsonChallengeError):
                    raise
                except Exception:  # noqa: BLE001 - 分类可能被商家删除/重建
                    cached_records = []
                if cached_records:
                    return token, cached_records

            # PickAI 当前报价自带具体 item URL。先用 goodsInfo 取得这件商品
            # 当前所属的原店与 category_id，再对该分类查一次宽词。无分类的
            # goodsList 会把不少店的真实库存报成 0，这正是此前“几千家却 0 条”
            # 的根因；分类查询同时还能带回这家店同分类的全部当前商品。
            item_url = candidate_item_urls.get(token)
            if (
                stop_after_first_available
                and allowed_item_urls is not None
                and item_url
                and isinstance(candidate_api, ShopApi)
            ):
                try:
                    scoped = candidate_api.category_records_for_item(
                        item_url,
                        origin_keywords,
                        goods_type,
                        max_pages=1,
                    )
                except Exception:  # noqa: BLE001 - 兼容旧店铺与测试适配器
                    scoped = None
                if scoped is not None:
                    resolved_token, records = scoped
                    return resolved_token or token, records

            search_options = {
                "shop_name": candidate_names.get(token) or None,
                "preferred_goods_type": (
                    candidate_goods_types.get(token) or preferred_goods_type
                ),
                "max_pages": 1,
            }
            if isinstance(candidate_api, ShopApi) and max_scoped_categories != 6:
                search_options["max_scoped_categories"] = max_scoped_categories
                search_options["skip_scoped_when_positive"] = (
                    stop_after_first_available
                )
            exact_keyword = candidate_keywords.get(token) or origin_keywords
            records = candidate_api.search(
                token,
                exact_keyword,
                goods_type,
                # 前台只需要最低价候选；抓完整店铺分页会让几个候选互相阻塞，
                # 严格实时核验反而在墙钟预算内一个结果都回不来。
                **search_options,
            )
            # 精确标题先保证最低价商品本身真实存在；随后只允许第一家已经
            # 返回有效库存的低价店补查一次宽词，把同店其他 Plus 商品一并
            # 带回。这样不再只有 1～2 条，同时也不会对每家店都重复请求。
            should_expand = False
            if (
                stop_after_first_available
                and exact_keyword.casefold() != origin_keywords.casefold()
                and any(
                    record_matches_effective_scope(record)
                    and _record_available(record)
                    for record in records
                )
            ):
                with expansion_lock:
                    if not expansion_claimed:
                        expansion_claimed = True
                        should_expand = True
            if should_expand:
                try:
                    expanded = candidate_api.search(
                        token,
                        origin_keywords,
                        goods_type,
                        **search_options,
                    )
                except Exception:  # noqa: BLE001 - 精确命中仍然可用
                    pass
                else:
                    records = list(
                        {
                            record.external_id: record
                            for record in [*records, *expanded]
                        }.values()
                    )
            return token, records
        except Exception:  # noqa: BLE001 - 某家店失败时继续核验其他店
            return token, []
        finally:
            candidate_api.fetcher.close()

    checked_items: list[tuple[str, ProductRecord]] = []
    unlisted_item_keys: list[str] = []
    closed_shop_item_keys: list[str] = []
    # 严格有货核验必须先取得本次 goodsList，才能挑“当前库存为正”的最低价
    # 商品做详情确认；预先核验旧快照只会把预算浪费在已缺货商品上。
    item_workers = (
        0
        if require_item_confirmation
        else max(1, min(settings.max_concurrency, len(item_keys))) if item_keys else 0
    )
    batches = (
        [item_keys[index::item_workers] for index in range(item_workers)]
        if item_workers
        else []
    )
    batch_results = []
    candidate_results = []
    network_task_count = len(batches) + len(candidates)
    if stats is not None:
        stats["network_task_count"] = network_task_count
    # 两个 worker 仍受全局主机节流，不会形成并发突发；它们只重叠响应等待。
    # 旧版强制单 worker 且首家有货立即停止，分类里只有一个商品时页面永远只
    # 有一条。这里允许两家交错核验，并以“结果数 + 店铺数”配额决定何时收工。
    worker_cap = 2
    network_workers = max(1, min(worker_cap, network_task_count))
    if network_task_count:
        # 逐店/逐商品核验都要经过 pay.ldxp.cn 的全局节流串行执行；给整批一个
        # 墙钟预算，超时就用已完成的部分，未完成的店留给后台索引与下次刷新。
        deadline = verify_budget_s
        executor = ThreadPoolExecutor(max_workers=network_workers)
        future_kinds: dict[object, str] = {}
        processed_futures: set[object] = set()

        available_ids_by_shop: dict[str, set[str]] = defaultdict(set)
        minimum_available_results = max(1, int(minimum_available_results))
        minimum_available_shops = max(1, int(minimum_available_shops))

        def collect_future(future: object) -> bool:
            processed_futures.add(future)
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 - 单个店铺失败不影响其他
                return False
            if future_kinds[future] == "item":
                batch_results.append(result)
                return False
            candidate_results.append(result)
            _token, records = result
            available = {
                record.external_id.casefold()
                for record in records
                if record_matches_effective_scope(record)
                and _record_available(record)
            }
            if available:
                available_ids_by_shop[_token.casefold()].update(available)
                if stats is not None:
                    stats["available_result_count"] = sum(
                        len(values) for values in available_ids_by_shop.values()
                    )
                    stats["available_shop_count"] = len(available_ids_by_shop)
            return bool(available)

        def available_quota_met() -> bool:
            return (
                sum(len(values) for values in available_ids_by_shop.values())
                >= minimum_available_results
                and len(available_ids_by_shop) >= minimum_available_shops
            )

        pending: set[object] = set()
        try:
            # 先启动 shop/ 候选核验，避免 goodsInfo 批处理占住全部 worker，
            # 导致已索引零售店在 8 秒预算结束前还没获得执行机会。
            for token in candidates:
                future = executor.submit(search_candidate, token)
                future_kinds[future] = "candidate"
            for batch in batches:
                future = executor.submit(check_item_batch, batch)
                future_kinds[future] = "item"
            pending = set(future_kinds)
            started_at = time.monotonic()
            found_available = False
            grace_deadline: float | None = None
            while pending:
                if worker_cancel_event.is_set():
                    break
                remaining = deadline - (time.monotonic() - started_at)
                if grace_deadline is not None:
                    remaining = min(remaining, grace_deadline - time.monotonic())
                if remaining <= 0:
                    break
                done, pending = wait(
                    pending,
                    timeout=min(0.1, remaining),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    found_available = collect_future(future) or found_available
                if (
                    stop_after_first_available
                    and found_available
                    and available_quota_met()
                ):
                    if first_available_grace_s <= 0:
                        break
                    if grace_deadline is None:
                        grace_deadline = time.monotonic() + first_available_grace_s
                # 同一批候选都访问 pay.ldxp.cn；任一请求触发滑块后继续等其他
                # worker 没有意义，只会把同一个拦截页再等一遍。
                if host_cooldown_remaining(RETAIL_BASE) > 0:
                    break
        finally:
            # 硬截止：旧实现的 shutdown(wait=True) 会在 9 秒预算结束后继续等
            # 正在执行的店铺，实际曾让一次 Plus 搜索卡 17 秒。这里立即返回，
            # 同时通知运行中的 worker 在下一次请求前退出。
            worker_cancel_event.set()
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        # 只补收截止瞬间已经完成的结果；绝不再等待运行中的任务。
        for future in future_kinds:
            if (
                future not in processed_futures
                and not future.cancelled()
                and future.done()
            ):
                collect_future(future)

    if require_item_confirmation and candidate_results:
        detail_keys: list[str] = []
        seen_detail_keys: set[str] = set()
        current_available = sorted(
            (
                record
                for _token, records in candidate_results
                for record in records
                if record_matches_effective_scope(record)
                and _record_available(record)
                and item_url_key(record.url)
            ),
            key=lambda record: (
                record.sale_price if record.sale_price > 0 else float("inf"),
                -record.stock,
            ),
        )
        for record in current_available:
            key = item_url_key(record.url)
            folded = key.casefold()
            if not key or folded in seen_detail_keys:
                continue
            seen_detail_keys.add(folded)
            detail_keys.append(key)
            if len(detail_keys) >= MAX_RETAIL_ITEM_CHECKS_PER_SEARCH:
                break
        if detail_keys:
            batch_results.append(check_item_batch(detail_keys))

    closed_folds: set[str] = set()
    if batch_results:
        for found, unlisted, closed in batch_results:
            checked_items.extend(found)
            unlisted_item_keys.extend(unlisted)
            closed_shop_item_keys.extend(closed)

        closed_tokens: set[str] = set()
        for item_key in closed_shop_item_keys:
            closed_tokens.update(
                _mark_retail_item_unavailable(
                    session,
                    item_key,
                    shop_closed=True,
                )
            )
        for item_key in unlisted_item_keys:
            _mark_retail_item_unavailable(
                session,
                item_key,
                shop_closed=False,
            )
        if closed_tokens:
            closed_folds = {token.casefold() for token in closed_tokens}

        for token, record in checked_items:
            if not record_matches_effective_scope(record):
                continue
            confirmed_items[token.casefold()].add(record.external_id.casefold())
            buckets[token][record.external_id] = record
            candidate_names[token] = record.merchant_name

    def persist_bucket(token: str) -> None:
        records_by_id = buckets.pop(token, {})
        records = list(records_by_id.values())
        if not records:
            return
        shop = _ensure_retail_shop(
            session,
            token,
            candidate_names.get(token) or records[0].merchant_name or token,
        )
        if not stop_after_first_available:
            ingest(session, shop, records)
            if shop.id is not None:
                verified_scopes[shop.id].update(
                    record.external_id for record in records
                )
            return
        known_inventory = [record for record in records if record.stock >= 0]
        unknown_inventory = [record for record in records if record.stock < 0]
        if unknown_inventory:
            ingest(session, shop, unknown_inventory, inventory_verified=False)
            if shop.id is not None:
                session.execute(
                    update(Product)
                    .where(
                        Product.shop_id == shop.id,
                        Product.external_id.in_(
                            [record.external_id for record in unknown_inventory]
                        ),
                    )
                    .values(
                        stock=-1,
                        status=ProductStatus.NORMAL,
                        inventory_verified_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.commit()
        if known_inventory:
            ingest(session, shop, known_inventory, inventory_verified=True)
        if shop.id is not None and known_inventory:
            verified_scopes[shop.id].update(
                record.external_id for record in known_inventory
            )

    def handle_candidate(result: tuple[str, list[ProductRecord]]) -> None:
        token, records = result
        try:
            for record in records:
                if not record_matches_effective_scope(record):
                    continue
                # goodsList 提供当前库存，goodsInfo 负责证明商品仍上架且店铺可访问。
                # 严格“仅看有货”时两者必须在本次搜索中同时成立。
                if (
                    require_item_confirmation
                    and record.external_id.casefold()
                    not in confirmed_items.get(token.casefold(), set())
                ):
                    continue
                buckets[token][record.external_id] = record
                candidate_names[token] = record.merchant_name
        except Exception:  # noqa: BLE001 - 防止异常记录影响其他店铺
            return
        # 当前页候选按价格顺序提交；第一家店完成后，前端轮询即可看到新价，
        # 不必等待所有候选店网络请求结束。
        persist_bucket(token)

    # 网络读取已并发完成；数据库写入仍在主线程串行执行。
    for result in candidate_results:
        if result[0].casefold() not in closed_folds:
            handle_candidate(result)

    for token in list(buckets):
        persist_bucket(token)
    return dict(verified_scopes)


def _search_manual_retail_matches(
    session: Session,
    keywords: str,
    goods_type: str,
    reference_tokens: set[str],
) -> dict[int, set[str]]:
    """联网查询用户明确收录、但不在参考目录中的店铺。"""
    if host_cooldown_remaining(RETAIL_BASE) > 0:
        return {}

    visible_products, _ = _db_search(
        session,
        keywords,
        goods_type,
        current=1,
        page_size=0,
        in_stock_only=False,
    )
    candidates: dict[int, tuple[Shop, Product]] = {}
    for product in visible_products:
        shop = session.get(Shop, product.shop_id)
        if (
            shop is None
            or shop.id is None
            or shop.kind != SourceKind.PUBLIC_SHOP
            or not shop.active
            or shop.note != "公开零售店"
            or shop.url.casefold() in reference_tokens
        ):
            continue
        previous = candidates.get(shop.id)
        if previous is None or (
            product.sale_price > 0
            and (
                previous[1].sale_price <= 0
                or product.sale_price < previous[1].sale_price
            )
        ):
            candidates[shop.id] = (shop, product)

    selected = sorted(
        candidates.values(),
        key=lambda entry: entry[0].created_at,
        reverse=True,
    )[:MAX_MANUAL_SHOPS_PER_SEARCH]
    if not selected:
        return {}

    scopes: dict[int, set[str]] = {}
    source = _origin_shop_api(timeout_s=REALTIME_ORIGIN_TIMEOUT_S)
    try:
        for shop, product in selected:
            try:
                records = source.search(
                    shop.url,
                    keywords,
                    goods_type,
                    shop_name=shop.name,
                    preferred_goods_type=_GOODS_TYPE_BY_CATEGORY.get(
                        product.category,
                        "",
                    ),
                    max_pages=1,
                )
            except Exception:  # noqa: BLE001 - 单个手动店失败不影响其他来源
                continue
            matched = [
                record
                for record in records
                if _record_matches(record, keywords, goods_type)
            ]
            if not matched:
                continue
            ingest(session, shop, matched)
            scopes[shop.id] = {record.external_id for record in matched}
    finally:
        source.fetcher.close()
    return scopes


def _retail_shop_search(
    session: Session,
    shop_id: int,
    goods_type: str,
    current: int,
    page_size: int,
    in_stock_only: bool,
    snapshot_external_ids: set[str] | None = None,
    sort: str = "sale_asc",
) -> tuple[list[Product], int]:
    category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
    matched = [
        product
        for product in session.exec(select(Product).where(Product.shop_id == shop_id)).all()
        if (snapshot_external_ids is None or product.external_id in snapshot_external_ids)
        and (not category or product.category.value == category)
    ]
    if in_stock_only:
        matched = [product for product in matched if _available(product)]
    if sort == "stock_desc":
        matched.sort(
            key=lambda product: (
                0 if _available(product) else 1,
                -product.stock,
                product.sale_price if product.sale_price > 0 else 1e12,
            )
        )
    else:
        matched.sort(
            key=lambda product: (
                0 if _available(product) else 1,
                product.sale_price if product.sale_price > 0 else 1e12,
            )
        )
    total = len(matched)
    if page_size <= 0:
        return matched, total
    start = (current - 1) * page_size
    return matched[start : start + page_size], total


def _mark_unlisted(session: Session, product: Product, now: datetime) -> None:
    """把完整店铺快照中已消失的商品标为不可售。"""
    product.status = ProductStatus.OFF
    product.stock = 0
    product.last_seen_at = now
    session.add(product)


def _merge_retail_with_source_inventory(retail: Product, source: Product) -> Product:
    """合并同一商品页的最新售价与货源接口明确暴露的库存。

    货源搜索的 ``price`` 与公开商品页价格一致，而且通常比完整店铺快照更新
    得更及时；因此两份记录价格不同时采用最后观察到的有效价格。公开零售接口
    的 status=1 只表示商品页仍上架，不提供库存数量。正库存仅在货源记录足够
    新时采用；缺货或下架信号则保守地直接生效，避免假阳性。
    """
    # PickAI 是聚合目录而不是货源权威接口。只要原店公开接口刚核验过，
    # 必须由原店覆盖 PickAI 的延迟价格/库存；同时保留 PickAI 的标准商品前缀。
    if source.external_id.startswith("p:"):
        merged = source.model_copy()
        retail_verified_at = retail.inventory_verified_at
        if (
            retail_verified_at is not None
            and _now() - retail_verified_at <= RETAIL_STOCK_FRESHNESS
        ):
            if retail.sale_price > 0:
                merged.sale_price = retail.sale_price
            merged.stock = retail.stock
            merged.status = retail.status
            merged.inventory_verified_at = retail_verified_at
            merged.last_seen_at = max(retail.last_seen_at, source.last_seen_at)
        return merged

    merged = retail.model_copy()
    if (
        source.sale_price > 0
        and source.last_seen_at > retail.last_seen_at
    ):
        merged.sale_price = source.sale_price
    if retail.status == ProductStatus.OFF:
        merged.status = ProductStatus.OFF
        merged.stock = 0
    elif source.status != ProductStatus.NORMAL or source.stock == 0:
        merged.status = (
            source.status
            if source.status != ProductStatus.NORMAL
            else ProductStatus.OUT
        )
        merged.stock = 0
    elif (
        source.stock > 0
        and _now() - source.last_seen_at <= (
            PICKAI_SNAPSHOT_FRESHNESS
            if source.external_id.startswith("p:")
            else SOURCE_STOCK_FRESHNESS
        )
    ):
        merged.status = ProductStatus.NORMAL
        merged.stock = source.stock
    else:
        merged.status = ProductStatus.NORMAL
        merged.stock = -1
    merged.last_seen_at = max(retail.last_seen_at, source.last_seen_at)
    return merged


def _db_search(
    session: Session,
    keywords: str,
    goods_type: str,
    current: int,
    page_size: int,
    in_stock_only: bool,
    source_shop_id: int | None = None,
    source_external_ids: set[str] | None = None,
    source_scopes: dict[int, set[str]] | None = None,
    snapshot_source_shop_ids: set[int] | None = None,
    retail_scopes: dict[int, set[str]] | None = None,
    sort: str = "sale_asc",
    platform: str = "all",
    expire_retail_stock: bool = True,
    require_pickai_origin: bool = False,
    verified_after: datetime | None = None,
) -> tuple[list[Product], int]:
    """在整个库里按关键词匹配：涵盖分销广场与自动发现的公开零售店。"""
    retail_shop_ids = set(
        session.exec(select(Shop.id).where(Shop.kind == SourceKind.PUBLIC_SHOP)).all()
    )

    # 先让 SQLite 用关键词缩小候选集。旧实现会把三万多条 ORM 对象全部载入
    # Python 再过滤，PyInstaller 版因此可能阻塞十几秒。
    normalized = _strict_origin_keywords(keywords).strip().lower()
    compact = re.sub(r"\s+", "", normalized)
    tokens = [token for token in normalized.split() if token]
    name_text = func.lower(func.coalesce(Product.name, ""))
    merchant_text = func.lower(func.coalesce(Product.merchant_name, ""))
    compact_text = func.replace(
        func.replace(
            func.replace(name_text + merchant_text, " ", ""),
            "\t",
            "",
        ),
        "\n",
        "",
    )
    text_clauses = []
    if compact:
        text_clauses.append(compact_text.contains(compact))
    if tokens:
        text_clauses.append(
            and_(
                *[
                    or_(name_text.contains(token), merchant_text.contains(token))
                    for token in tokens
                ]
            )
        )

    if not text_clauses:
        return [], 0

    category = _CATEGORY_BY_GOODS_TYPE.get(goods_type or "")
    if source_scopes is None and source_external_ids is not None:
        source_scopes = (
            {source_shop_id: source_external_ids}
            if source_shop_id is not None
            else {}
        )
    scope_clauses = None
    if (
        source_scopes is not None
        or snapshot_source_shop_ids is not None
        or retail_scopes is not None
    ):
        scope_clauses = []
        for scoped_shop_id, external_ids in (source_scopes or {}).items():
            if external_ids:
                scope_clauses.append(
                    and_(
                        Product.shop_id == scoped_shop_id,
                        Product.external_id.in_(external_ids),
                    )
                )
        if snapshot_source_shop_ids:
            scope_clauses.append(
                and_(
                    Product.shop_id.in_(snapshot_source_shop_ids),
                    Product.status != ProductStatus.OFF,
                )
            )
        if retail_scopes is not None:
            for scoped_shop_id, external_ids in retail_scopes.items():
                if external_ids:
                    scope_clauses.append(
                        and_(
                            Product.shop_id == scoped_shop_id,
                            Product.external_id.in_(external_ids),
                        )
                    )
        elif retail_shop_ids:
            scope_clauses.append(
                and_(
                    Product.shop_id.in_(retail_shop_ids),
                    Product.status != ProductStatus.OFF,
                )
            )
        if not scope_clauses:
            return [], 0

    def apply_structured_filters(statement):
        if category is not None:
            statement = statement.where(Product.category == category)
        if scope_clauses is not None:
            statement = statement.where(or_(*scope_clauses))
        return statement

    fallback_statement = apply_structured_filters(
        select(Product).where(or_(*text_clauses))
    )
    statement = fallback_statement
    uses_fts = len(compact) >= 3
    if uses_fts:
        def fts_match(value: str, parameter: str):
            escaped = value.replace('"', '""')
            ids = (
                text(
                    "SELECT rowid FROM product_fts "
                    f"WHERE product_fts MATCH :{parameter}"
                )
                .bindparams(**{parameter: f'"{escaped}"'})
                .columns(rowid=Integer)
                .subquery()
            )
            return Product.id.in_(select(ids.c.rowid))

        compact_condition = fts_match(compact, "fts_compact")
        token_conditions = [
            (
                fts_match(token, f"fts_token_{index}")
                if len(token) >= 3
                else or_(name_text.contains(token), merchant_text.contains(token))
            )
            for index, token in enumerate(tokens)
        ]
        fts_condition = (
            or_(compact_condition, and_(*token_conditions))
            if len(tokens) > 1
            else compact_condition
        )
        statement = apply_structured_filters(
            select(Product).where(fts_condition)
        )

    try:
        candidates = session.exec(statement).all()
    except OperationalError:
        candidates = session.exec(fallback_statement).all()

    strict_scope = strict_realtime_scope_for_query(keywords)
    strict_type_names = set(strict_type_names_for_query(keywords))
    strict_urls = _strict_catalog_urls(session, keywords) if strict_scope else set()

    def in_strict_catalog(product: Product) -> bool:
        if strict_scope is None:
            return True
        if require_pickai_origin and product.shop_id in (retail_scopes or {}):
            # 商品 URL 可能被复用成教程、提链或别的服务。本轮原店标题必须
            # 仍然属于所选快捷分类，历史 PickAI 分类不能充当放行凭据。
            return _strict_product_name_matches(product.name, keywords)
        if strict_scope == "k12":
            return is_k12_product_name(product.name)
        if not strict_urls:
            # 内存测试、首次建库和显式单商品核验可能还没有标准 URL 映射；
            # live_search 的 source/retail scope 仍会阻止无关缓存进入实时结果。
            return True
        return (
            _pickai_standard_name(product.name) in strict_type_names
            or product.url in strict_urls
            or (
                product.shop_id in (retail_scopes or {})
                and _strict_product_name_matches(product.name, keywords)
            )
        )

    def matches_effective_query(product: Product) -> bool:
        # 原店标题经常只写 ``Plus``，不会重复 PickAI 标准分类里的
        # ``ChatGPT Plus``。严格分类已有 URL 白名单时，URL 命中本身就是更强
        # 的相关性证据；继续要求原店标题同时包含 ChatGPT + Plus 会把已收录的
        # 1592 个候选错误过滤成 0，随后退化为又慢又少的 goodsInfo 探测。
        if (
            require_pickai_origin
            and strict_scope is not None
            and product.shop_id in (retail_scopes or {})
        ):
            category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
            return _strict_product_name_matches(product.name, keywords) and (
                not category or product.category.value == category
            )
        if strict_scope in {"chatgpt", "email", "openai_sms"} and strict_urls:
            category = _CAT_BY_GOODS_TYPE.get(goods_type or "")
            return product.url in strict_urls and (
                not category or product.category.value == category
            )
        return _matches_search(product, keywords, goods_type)

    matched = [
        product
        for product in candidates
        if matches_effective_query(product)
        and (platform == "all" or _product_platform(product) == platform)
        and in_strict_catalog(product)
    ]

    # 同一商品页可能同时来自货源接口与零售接口。零售接口提供买家售价，
    # 货源接口提供真实 stock_count；两者合并后才能正确判断是否有货。
    unique: dict[str, Product] = {}
    for product in matched:
        if expire_retail_stock and product.shop_id in retail_shop_ids:
            product = _retail_inventory_view(product)
        key = (
            product.url.strip().casefold()
            or f"{product.merchant_name.casefold()}|{product.name.casefold()}|{product.sale_price}"
        )
        previous = unique.get(key)
        if previous is None:
            unique[key] = product
            continue
        product_is_retail = product.shop_id in retail_shop_ids
        previous_is_retail = previous.shop_id in retail_shop_ids
        if product_is_retail != previous_is_retail:
            retail = product if product_is_retail else previous
            source = previous if product_is_retail else product
            unique[key] = _merge_retail_with_source_inventory(retail, source)
            continue
        product_rank = (
            _available(product),
            product.last_seen_at,
        )
        previous_rank = (
            _available(previous),
            previous.last_seen_at,
        )
        if product_rank > previous_rank:
            unique[key] = product
    matched = list(unique.values())

    if in_stock_only:
        matched = [product for product in matched if _available(product)]

    if require_pickai_origin:
        fresh_cutoff = verified_after or (_now() - RETAIL_STOCK_FRESHNESS)
        matched = [
            product
            for product in matched
            if product.inventory_verified_at is not None
            and product.inventory_verified_at >= fresh_cutoff
            and product.stock >= 0
        ]

    if sort == "stock_desc":
        matched.sort(
            key=lambda product: (
                0 if _available(product) else 1,
                0 if strict_scope else _search_relevance_rank(product, keywords),
                -product.stock,
                product.sale_price if product.sale_price > 0 else 1e12,
            )
        )
    else:
        # 有货优先 + 售价从低到高（最低价）
        matched.sort(
            key=lambda product: (
                0 if _available(product) else 1,
                0 if strict_scope else _search_relevance_rank(product, keywords),
                product.sale_price if product.sale_price > 0 else 1e12,
            )
        )
    total = len(matched)
    if page_size <= 0:
        return matched, total
    start = (current - 1) * page_size
    return matched[start : start + page_size], total


def cached_search(
    session: Session,
    keywords: str,
    goods_type: str = "",
    current: int = 1,
    page_size: int = 20,
    in_stock_only: bool = True,
    sort: str = "sale_asc",
    platform: str = "all",
    preserve_snapshot_stock: bool = False,
) -> tuple[list[Product], int]:
    """只读取本地索引；可由明确的极速模式保留快照库存语义。"""
    return _db_search(
        session,
        keywords,
        goods_type,
        current,
        page_size,
        in_stock_only,
        sort=sort,
        platform=platform,
        # 默认仍执行旧库存保护，避免其他调用方把过期正库存当成当前有货。
        # 只有前端明确选择“极速最低价”时保留最近索引快照，并标记为未实时核验。
        expire_retail_stock=not preserve_snapshot_stock,
    )


def _apply_origin_inventory(
    session: Session,
    product: Product,
    record: ProductRecord | None,
    *,
    unavailable_status: ProductStatus | None = None,
    mark_verified: bool = True,
) -> None:
    """把原店本次核验结果覆盖到 PickAI 商品，但保留标准商品名称。"""
    now = _now()
    values: dict[str, object] = {"last_seen_at": now}
    if mark_verified:
        values["inventory_verified_at"] = now
    else:
        # “库存未知”不能携带旧的核验时间，否则下一次聚合写入会把它
        # 误当成仍在 90 秒有效窗口内的原店结果。
        values["inventory_verified_at"] = None
    if unavailable_status is not None:
        values.update(stock=0, status=unavailable_status)
    elif record is not None:
        if record.sale_price > 0:
            values["sale_price"] = record.sale_price
        values.update(stock=record.stock, status=record.status)
    if product.id is None:
        for field, value in values.items():
            setattr(product, field, value)
        session.add(product)
        return
    # 核验过程中会创建/更新公开店铺并 commit；大型旧库的 identity map 中可能
    # 已有被 expire 的同 URL ORM 对象。直接按主键 UPDATE，避免过期对象阻断核验。
    session.execute(
        update(Product)
        .where(Product.id == product.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


def _verify_pickai_inventory(
    session: Session,
    keywords: str,
    goods_type: str,
    *,
    current: int = 1,
    page_size: int = 50,
    sort: str = "sale_asc",
    limit: int = MAX_PICKAI_ORIGIN_CHECKS_PER_SEARCH,
    candidate_external_ids: set[str] | None = None,
    force_refresh: bool = False,
) -> dict[str, object]:
    """按当前结果顺序向原店复核少量 PickAI 报价。

    PickAI 全量目录只负责快速发现。每次用户发起的实时搜索会核对
    当前页的低价候选；前端先显示本地候选，不再空白等待这些请求。
    """
    result: dict[str, object] = {
        "attempted": 0,
        "verified": 0,
        "unavailable": 0,
        "failed": 0,
        "challenge": 0,
        "challenge_external_ids": [],
    }
    if limit <= 0 or host_cooldown_remaining(RETAIL_BASE) > 0:
        return result

    pickai_shop = session.exec(
        select(Shop).where(
            Shop.name == PICKAI_SHOP_NAME,
            Shop.kind == SourceKind.SOURCE_SQUARE,
            Shop.active.is_(True),
        )
    ).first()
    if pickai_shop is None or pickai_shop.id is None:
        return result

    candidates, _ = _db_search(
        session,
        keywords,
        goods_type,
        current=1,
        page_size=max(max(1, current) * max(1, page_size), limit),
        in_stock_only=False,
        snapshot_source_shop_ids={pickai_shop.id},
        sort=sort,
        platform="ldxp",
        expire_retail_stock=False,
    )
    candidates = [
        product
        for product in candidates
        if product.shop_id == pickai_shop.id
        and product.external_id.startswith("p:")
        and item_url_key(product.url)
        and (
            candidate_external_ids is None
            or product.external_id in candidate_external_ids
        )
    ]
    if page_size > 0:
        start = (max(1, current) - 1) * page_size
        candidates = candidates[start : start + page_size]
    # 先轮换从未核验的可见项；同一价格顺序保持稳定。否则头部商品每 90 秒
    # 重新抢占名额，页面后半部分永远轮不到原店核验。
    candidates.sort(
        key=lambda product: product.inventory_verified_at or datetime.min
    )
    if not candidates:
        return result

    now = _now()
    fresh_cutoff = now - PICKAI_ORIGIN_VERIFICATION_FRESHNESS
    retail_shop_ids = set(
        session.exec(select(Shop.id).where(Shop.kind == SourceKind.PUBLIC_SHOP)).all()
    )
    overlays_by_url: dict[str, Product] = {}
    if retail_shop_ids:
        urls = {product.url for product in candidates if product.url}
        overlays = session.exec(
            select(Product).where(
                Product.shop_id.in_(retail_shop_ids),
                Product.url.in_(urls),
                Product.inventory_verified_at.is_not(None),
            )
        ).all()
        for overlay in overlays:
            previous = overlays_by_url.get(overlay.url)
            if previous is None or (
                overlay.inventory_verified_at or datetime.min
            ) > (previous.inventory_verified_at or datetime.min):
                overlays_by_url[overlay.url] = overlay

    stale: list[Product] = []
    for product in candidates:
        overlay = overlays_by_url.get(product.url)
        if (
            not force_refresh
            and
            overlay is not None
            and overlay.inventory_verified_at is not None
            and overlay.inventory_verified_at >= fresh_cutoff
        ):
            _apply_origin_inventory(session, product, overlay)
            result["verified"] += 1
            continue
        if (
            not force_refresh
            and
            product.inventory_verified_at is not None
            and product.inventory_verified_at >= fresh_cutoff
        ):
            continue
        stale.append(product)
        if len(stale) >= limit:
            break

    if not stale:
        session.commit()
        return result

    result["attempted"] = len(stale)
    records_cache: dict[tuple[str, str], list[ProductRecord] | Exception] = {}
    source = _origin_shop_api(
        timeout_s=REALTIME_ORIGIN_TIMEOUT_S,
        deadline_monotonic=time.monotonic() + RETAIL_VERIFY_BUDGET_S,
    )
    challenge_hit = False
    try:
        for product in stale:
            if challenge_hit:
                unknown = ProductRecord(
                    external_id=product.external_id,
                    name=product.name,
                    category=product.category,
                    merchant_name=product.merchant_name,
                    sale_price=product.sale_price,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url=product.url,
                )
                _apply_origin_inventory(
                    session,
                    product,
                    unknown,
                    mark_verified=False,
                )
                cast_ids = result["challenge_external_ids"]
                if isinstance(cast_ids, list):
                    cast_ids.append(product.external_id)
                continue
            key = item_url_key(product.url)
            try:
                found = source.item_record(key)
            except ShopClosedError:
                _apply_origin_inventory(
                    session,
                    product,
                    None,
                    unavailable_status=ProductStatus.OUT,
                )
                result["verified"] += 1
                result["unavailable"] += 1
            except JsonChallengeError:
                # 站点返回阿里云滑块时，不能把 PickAI 延迟库存冒充成有货。
                unknown = ProductRecord(
                    external_id=product.external_id,
                    name=product.name,
                    category=product.category,
                    merchant_name=product.merchant_name,
                    sale_price=product.sale_price,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url=product.url,
                )
                _apply_origin_inventory(session, product, unknown, mark_verified=False)
                result["failed"] += 1
                result["challenge"] += 1
                challenge_hit = True
                cast_ids = result["challenge_external_ids"]
                if isinstance(cast_ids, list):
                    cast_ids.append(product.external_id)
            except Exception:  # noqa: BLE001 - 单条失败留待下轮
                result["failed"] += 1
            else:
                if found is None:
                    _apply_origin_inventory(
                        session,
                        product,
                        None,
                        unavailable_status=ProductStatus.OFF,
                    )
                    result["verified"] += 1
                    result["unavailable"] += 1
                else:
                    token, detail = found
                    preferred_type = _GOODS_TYPE_BY_CATEGORY.get(detail.category, "")
                    cache_key = (token, preferred_type)
                    records_or_error = records_cache.get(cache_key)
                    if records_or_error is None:
                        try:
                            search_options = {
                                "shop_name": detail.merchant_name or token,
                                "preferred_goods_type": preferred_type,
                                "max_pages": 1,
                            }
                            if isinstance(source, ShopApi):
                                search_options["max_scoped_categories"] = 1
                            records_or_error = source.search(
                                token,
                                keywords,
                                goods_type,
                                **search_options,
                            )
                        except Exception as exc:  # noqa: BLE001 - 本轮保留未知
                            records_or_error = exc
                        records_cache[cache_key] = records_or_error

                    if isinstance(records_or_error, Exception):
                        # goodsInfo 已经确认商品页存在，但 goodsList 没有返回
                        # 可用的当前库存。无论是滑块还是临时网络错误，都不能
                        # 把 PickAI 聚合库存/旧库存写成“已核验有货”。
                        detail.stock = -1
                        detail.status = ProductStatus.NORMAL
                        _apply_origin_inventory(
                            session,
                            product,
                            detail,
                            mark_verified=False,
                        )
                        result["failed"] += 1
                        if isinstance(records_or_error, JsonChallengeError):
                            result["challenge"] += 1
                            challenge_hit = True
                            cast_ids = result["challenge_external_ids"]
                            if isinstance(cast_ids, list):
                                cast_ids.append(product.external_id)
                        session.commit()
                        continue

                    records = records_or_error
                    records_by_key = {
                        item_url_key(record.url): record
                        for record in records
                        if item_url_key(record.url)
                    }
                    shop = _ensure_retail_shop(
                        session,
                        token,
                        detail.merchant_name or token,
                        note="PickAI 候选的原店实时核验",
                    )
                    if records:
                        ingest(session, shop, records, inventory_verified=True)
                    exact = records_by_key.get(item_url_key(product.url))
                    if exact is None:
                        detail.stock = -1
                        detail.status = ProductStatus.NORMAL
                        _apply_origin_inventory(
                            session,
                            product,
                            detail,
                            mark_verified=False,
                        )
                        result["failed"] += 1
                    else:
                        _apply_origin_inventory(session, product, exact)
                        result["verified"] += 1
                        if exact.status != ProductStatus.NORMAL or exact.stock == 0:
                            result["unavailable"] += 1
            # 每件处理完就提交，前端读本地库时可逐条看到实时结果。
            session.commit()
    finally:
        source.fetcher.close()
        session.commit()
    return result


def live_search(
    session: Session,
    keywords: str,
    goods_type: str = "",
    current: int = 1,
    page_size: int = 20,
    in_stock_only: bool = True,
    sort: str = "sale_asc",
    platform: str = "all",
    warnings: list[str] | None = None,
    public_only: bool = False,
    refresh_pickai: bool = False,
) -> tuple[list[Product], int]:
    """实时搜索已配置平台，并自动发现和核验链动公开零售报价。"""
    search_started_at = _now()
    retail_token = shop_url_token(keywords)
    if retail_token:
        source = _origin_shop_api()
        try:
            shop = add_retail_shop(session, retail_token, source=source)
            snapshot_external_ids = sync_retail_shop(session, shop, source)
        finally:
            source.fetcher.close()
        return _retail_shop_search(
            session,
            shop.id,
            goods_type,
            current,
            page_size,
            in_stock_only,
            snapshot_external_ids,
            sort,
        )

    retail_item_key = item_url_key(keywords)
    if retail_item_key:
        source = _origin_shop_api()
        try:
            try:
                found = source.item_record(retail_item_key)
            except ShopClosedError:
                _mark_retail_item_unavailable(
                    session,
                    retail_item_key,
                    shop_closed=True,
                )
                return [], 0
            if found is None:
                _mark_retail_item_unavailable(
                    session,
                    retail_item_key,
                    shop_closed=False,
                )
                return [], 0
            token, record = found
            # goodsInfo 只确认页面仍存在，不提供库存。再用所属店铺的商品列表
            # 精确反查同一 goods_key，才能得到当前 stock_count 与真实售价。
            try:
                current_records = source.search(
                    token,
                    record.name,
                    "",
                    shop_name=record.merchant_name or token,
                    preferred_goods_type=_GOODS_TYPE_BY_CATEGORY.get(
                        record.category,
                        "",
                    ),
                )
                current_record = next(
                    (
                        candidate
                        for candidate in current_records
                        if item_url_key(candidate.url) == retail_item_key
                    ),
                    None,
                )
                if current_record is not None:
                    record = current_record
            except Exception:  # noqa: BLE001 - 详情价格仍可作为保守回退
                pass
            shop = _ensure_retail_shop(
                session,
                token,
                record.merchant_name or token,
                note="公开零售店",
            )
            ingest(session, shop, [record])
        finally:
            source.fetcher.close()
        return _retail_shop_search(
            session,
            shop.id,
            goods_type,
            current,
            page_size,
            in_stock_only,
            {record.external_id},
            sort,
        )

    source_scopes: dict[int, set[str]] = {}
    snapshot_source_shop_ids: set[int] = set()
    current_pickai_external_ids: set[str] = set()
    current_pickai_urls: set[str] = set()
    strict_candidate_total = 0
    pickai_live_refreshed = False
    ldxp_records: list[ProductRecord] = []
    catfk_records: list[ProductRecord] = []
    reference_records: list[ProductRecord] = []
    reference_scopes: dict[int, set[str]] = defaultdict(set)
    reference_tokens = set(REFERENCE_STORE_TOKENS)
    source_errors: list[Exception] = []
    successful_sources = 0

    # K12 复用本地专库；Plus/邮箱/接码每轮先读 PickAI 当前最低价页重新排店。
    # PickAI 只负责候选顺序，绝不承担最终价格或库存。
    strict_scope = strict_realtime_scope_for_query(keywords)
    strict_realtime = bool(refresh_pickai and strict_scope)

    # PickAI 的关键词接口会把 ``K12`` 错配成 ``Grok12``。四类快捷搜索都
    # 优先使用已筛选候选；K12 永远不调用那个宽泛关键词接口。
    if (
        strict_realtime
        and platform in {"all", "ldxp"}
        and strict_scope == "k12"
    ):
        current_pickai_urls = _strict_catalog_urls(session, keywords)
        strict_candidate_total = len(current_pickai_urls)
        if current_pickai_urls:
            pickai_shop = session.exec(
                select(Shop).where(
                    Shop.name == PICKAI_SHOP_NAME,
                    Shop.kind == SourceKind.SOURCE_SQUARE,
                    Shop.active.is_(True),
                )
            ).first()
            if pickai_shop is not None and pickai_shop.id is not None:
                local_pickai_products = session.exec(
                    select(Product).where(
                        Product.shop_id == pickai_shop.id,
                        Product.url.in_(current_pickai_urls),
                        Product.status != ProductStatus.OFF,
                    )
                ).all()
                current_pickai_external_ids = {
                    product.external_id for product in local_pickai_products
                }
                if current_pickai_external_ids:
                    source_scopes[pickai_shop.id] = set(
                        current_pickai_external_ids
                    )
            pickai_live_refreshed = True
            successful_sources += 1
        elif strict_scope == "k12" and warnings is not None:
            warnings.append("尚未收录 K12 候选店，本轮没有可核验入口。")

    if (
        refresh_pickai
        and platform in {"all", "ldxp"}
        and strict_scope != "k12"
    ):
        catalog = PickAICatalog(
            base_url=settings.pickai_base_url,
            retries=1,
            timeout_s=REALTIME_ORIGIN_TIMEOUT_S,
        )
        try:
            if strict_realtime:
                raw_quotes, declared_total = catalog.search_current_strict(
                    keywords,
                    max_pages=max(1, current),
                    page_size=50,
                )
                strict_candidate_total = declared_total
            else:
                raw_quotes, declared_total = catalog.search(
                    keywords,
                    max_pages=max(1, current),
                    page_size=50,
                )
                for quote in raw_quotes:
                    if not quote.get("product_type_names"):
                        quote["product_type_names"] = [keywords.strip()]
            current_snapshot = PickAISnapshot(
                fetched_at=datetime.now(timezone.utc).isoformat(),
                categories=[],
                product_types=[],
                quotes=raw_quotes,
                relay_providers={"items": []},
                declared_quotes=declared_total,
                duplicate_quotes=0,
                request_count=catalog.request_count,
            )
            records = current_snapshot.product_records()
            if strict_realtime:
                # 标准分类本身并不干净：Plus 里有邀请额度，接码里有教程。
                # 先在候选阶段剔除，避免四个原店核验名额被假低价商品占满。
                records = [
                    record
                    for record in records
                    if _strict_product_name_matches(record.name, keywords)
                ]
            pickai_shop = get_or_create_live_shop(
                session,
                PICKAI_SHOP_NAME,
                settings.pickai_base_url,
            )
            ingest(session, pickai_shop, records, inventory_verified=False)
            current_pickai_external_ids = {
                record.external_id for record in records
            }
            current_pickai_urls = {
                record.url for record in records if record.url
            }
            pickai_live_refreshed = True
            if pickai_shop.id is not None and current_pickai_external_ids:
                if strict_realtime:
                    # 严格实时模式只允许 PickAI 本次返回的标准分类报价进入范围，
                    # 不把历史快照中已经消失的老货重新混进来。
                    source_scopes[pickai_shop.id] = set(current_pickai_external_ids)
                else:
                    # 通用模式仍保留本地完整快照作为候选覆盖。
                    snapshot_source_shop_ids.add(pickai_shop.id)
            successful_sources += 1
        except Exception as exc:  # noqa: BLE001 - 仍可用本地全量快照回退
            source_errors.append(exc)
            if warnings is not None:
                warnings.append(f"PickAI 当前报价刷新失败，已保留本地候选：{exc}")
        finally:
            catalog.close()

    # PickAI 快照是完整公开目录，不应像一次官方关键词请求那样逐条构造 scope。
    # 只要完整快照仍在可用窗口内，就允许 SQLite 在该来源的全量商品中匹配。
    if platform in {"all", "ldxp"}:
        pickai_shop = session.exec(
            select(Shop).where(
                Shop.name == PICKAI_SHOP_NAME,
                Shop.kind == SourceKind.SOURCE_SQUARE,
                Shop.active.is_(True),
            )
        ).first()
        if (
            pickai_shop is not None
            and pickai_shop.id is not None
            and pickai_shop.last_synced_at is not None
            and _now() - pickai_shop.last_synced_at <= PICKAI_SNAPSHOT_FRESHNESS
            and not strict_realtime
        ):
            snapshot_source_shop_ids.add(pickai_shop.id)
        elif warnings is not None and not pickai_live_refreshed:
            warnings.append("PickAI 全量公开报价正在首次同步或快照已过期。")

    source_specs = []
    if (
        not strict_realtime
        and not public_only
        and platform in {"all", "ldxp"}
        and settings.merchant_token
    ):
        source_specs.append(
            (
                LIVE_SHOP_NAME,
                settings.base_url,
                lambda: SourceSquare(page_size=200),
                True,
            )
        )
    if (
        (not strict_realtime or platform == "catfk")
        and not public_only
        and platform in {"all", "catfk"}
        and settings.catfk_merchant_token
    ):
        source_specs.append(
            (
                CATFK_LIVE_SHOP_NAME,
                settings.catfk_base_url,
                lambda: SourceSquare(
                    page_size=200,
                    base_url=settings.catfk_base_url,
                    merchant_token=settings.catfk_merchant_token,
                    public_base_url=settings.catfk_base_url,
                ),
                False,
            )
        )
    elif (
        not public_only
        and platform == "catfk"
        and warnings is not None
    ):
        warnings.append("云猫寄售 Merchant-Token 未配置，本次未搜索云猫官方货源。")

    for shop_name, base_url, source_factory, discovers_retail in source_specs:
        src = None
        try:
            src = source_factory()
            source_keywords = (
                keywords
                if discovers_retail
                else _catfk_search_keywords(keywords)
            )
            # 云猫网页端默认固定查询“卡密”；接口传空 goods_type 会直接返回空列表。
            source_goods_type = goods_type or ("" if discovers_retail else "card")
            records = _search_variants(src, source_keywords, source_goods_type)
            live_shop = get_or_create_live_shop(session, shop_name, base_url)
            ingest(session, live_shop, records)
            if live_shop.id is not None:
                source_scopes[live_shop.id] = {
                    record.external_id for record in records
                }
            if discovers_retail:
                ldxp_records = records
            else:
                catfk_records = records
            successful_sources += 1
        except Exception as exc:  # noqa: BLE001 - 单个平台故障不应阻断另一个平台
            source_errors.append(exc)
            catfk_login_expired = (
                not discovers_retail
                and isinstance(exc, BlockedError)
                and "未登录" in str(exc)
            )
            if catfk_login_expired:
                settings.set_catfk_token("")
            if warnings is not None:
                source_name = "链动小铺" if discovers_retail else "云猫寄售"
                if catfk_login_expired:
                    warnings.append(
                        "云猫寄售登录已失效，已停止使用旧 Token；请在设置中重新登录。"
                    )
                else:
                    warnings.append(f"{source_name}官方搜索失败：{exc}")
        finally:
            if src is not None:
                src.fetcher.close()

    allows_public_retail = platform in {"all", "ldxp"}
    if not successful_sources and source_errors and not allows_public_retail:
        raise source_errors[0]
    if not successful_sources and not allows_public_retail:
        raise ValueError("尚未配置可用平台的 Merchant-Token")

    if allows_public_retail and not strict_realtime:
        reference = ReferenceCatalog()
        if hasattr(reference.fetcher, "timeout_s"):
            reference.fetcher.timeout_s = REALTIME_ORIGIN_TIMEOUT_S
        try:
            reference_records = reference.search(
                keywords,
                in_stock_only=in_stock_only,
                # 本次只会直接核验少量候选店，读取更多目录分页只会增加延迟，
                # 并不会增加最终可展示的实时结果。
                max_pages=1,
            )
            records_by_token: dict[str, list[ProductRecord]] = defaultdict(list)
            for record in reference_records:
                token = shop_token(record.merchant_link)
                if token and _record_matches(record, keywords, goods_type):
                    reference_tokens.add(token.casefold())
                    records_by_token[token].append(record)
            for token, records in records_by_token.items():
                shop = _ensure_retail_shop(
                    session,
                    token,
                    records[0].merchant_name or token,
                    note="参考目录收录的公开零售店",
                )
                ingest(session, shop, records, inventory_verified=False)
                if shop.id is not None:
                    reference_scopes[shop.id].update(
                        record.external_id for record in records
                    )
        except Exception as exc:  # noqa: BLE001 - 第三方目录失败时继续官方与公开店铺核验
            if warnings is not None:
                warnings.append(f"参考店铺目录暂不可用，已继续搜索其他来源：{exc}")
        finally:
            reference.fetcher.close()

    # 即使货源广场对该关键词零命中，也必须刷新当前页已有的公开零售店。
    # 零售店独有商品正是无法通过 ldxp_records 触达的那一类。
    # 参考目录的正库存可能比店铺接口慢一轮。只在“不限库存”时展示它的
    # 候选快照；“只看有货”必须由下面的 ShopApi 本次核验后才能进入结果。
    retail_scopes: dict[int, set[str]] = (
        {
            shop_id: set(external_ids)
            for shop_id, external_ids in reference_scopes.items()
        }
        if not in_stock_only and not strict_realtime
        else {}
    )
    should_verify_retail = platform in {"all", "ldxp"}
    if should_verify_retail:
        verified_retail_scopes: dict[int, set[str]] = {}
        strict_cancel_event = _begin_strict_search() if strict_realtime else None
        # 快捷分类采用 latest-request-wins：切换 K12 / Plus 时直接取消旧请求，
        # 不能再让一个已被前端丢弃的请求占全局锁 20 秒并把新结果伪装成 0 条。
        acquired_verify_slot = True if strict_realtime else (
            _LIVE_RETAIL_VERIFY_LOCK.acquire(blocking=False)
            if public_only
            else _LIVE_RETAIL_VERIFY_LOCK.acquire(timeout=USER_SEARCH_VERIFY_WAIT_S)
        )
        if acquired_verify_slot:
            try:
                if strict_realtime:
                    # 四个快捷分类先用已筛出的关联店铺直接查 goodsList，
                    # 避免宽泛关键词把其他接码或 Grok12 商品带进来。
                    discovery_stats: dict[str, int] = {}
                    verified_retail_scopes = _discover_retail_matches(
                        session,
                        keywords,
                        goods_type,
                        [*reference_records, *ldxp_records],
                        require_item_confirmation=False,
                        prioritize_source_records=bool(reference_records),
                        allowed_item_urls=(
                            current_pickai_urls
                            or _strict_catalog_urls(session, keywords)
                        ),
                        max_candidates=STRICT_REALTIME_SHOPS_PER_SEARCH,
                        request_timeout_s=STRICT_REALTIME_ORIGIN_TIMEOUT_S,
                        max_scoped_categories=1,
                        stats=discovery_stats,
                        allow_web_discovery=False,
                        verify_budget_s=STRICT_REALTIME_VERIFY_BUDGET_S,
                        stop_after_first_available=True,
                        first_available_grace_s=0.4,
                        minimum_available_results=STRICT_REALTIME_MIN_RESULTS,
                        minimum_available_shops=STRICT_REALTIME_MIN_SHOPS,
                        prefer_available_candidates=True,
                        cancel_event=strict_cancel_event,
                    )
                    pickai_verification = {
                        "attempted": 0,
                        "verified": 0,
                        "unavailable": 0,
                        "failed": 0,
                        "challenge": 0,
                        "challenge_external_ids": [],
                    }
                    # 首次使用尚无任何店铺映射时，只解析两个当前低价商品；
                    # 解析成功会把其原店加入本地专库，后续搜索直接查店铺。
                    if (
                        not verified_retail_scopes
                        and not discovery_stats.get("network_task_count", 0)
                        and host_cooldown_remaining(RETAIL_BASE) <= 0
                    ):
                        pickai_verification = _verify_pickai_inventory(
                            session,
                            keywords,
                            goods_type,
                            current=current,
                            page_size=page_size,
                            sort=sort,
                            limit=2,
                            candidate_external_ids=(
                                current_pickai_external_ids or None
                            ),
                            force_refresh=True,
                        )
                    elif (
                        not verified_retail_scopes
                        and discovery_stats.get("network_task_count", 0)
                        and warnings is not None
                    ):
                        candidate_note = (
                            f"候选目录当前发现约 {strict_candidate_total:,} 条报价；"
                            if strict_candidate_total
                            else "候选目录已有报价；"
                        )
                        challenge_cooldown = host_cooldown_remaining(RETAIL_BASE)
                        protection_note = (
                            "源站返回了验证页，程序仅做"
                            f"约 {max(1, int(challenge_cooldown))} 秒短保护；"
                            if challenge_cooldown > 0
                            else ""
                        )
                        warnings.append(
                            f"{candidate_note}{protection_note}原店本轮未返回可核验商品。"
                            "这里的 0 条表示源站无法核验，不表示市场没货。"
                        )
                else:
                    # 通用模式维持旧流程：先按 PickAI 当前项解析原店，再补零售店。
                    pickai_verification = _verify_pickai_inventory(
                        session,
                        keywords,
                        goods_type,
                        current=current,
                        page_size=page_size,
                        sort=sort,
                        candidate_external_ids=(
                            current_pickai_external_ids or None
                        ),
                    )
                    verified_retail_scopes = _discover_retail_matches(
                        session,
                        keywords,
                        goods_type,
                        [*reference_records, *ldxp_records],
                        require_item_confirmation=in_stock_only and not reference_records,
                        prioritize_source_records=bool(reference_records),
                    )
                challenged_ids = pickai_verification.get("challenge_external_ids", [])
                if isinstance(challenged_ids, list) and challenged_ids:
                    pickai_shop = session.exec(
                        select(Shop).where(
                            Shop.name == PICKAI_SHOP_NAME,
                            Shop.kind == SourceKind.SOURCE_SQUARE,
                        )
                    ).first()
                    if pickai_shop is not None and pickai_shop.id is not None:
                        session.execute(
                            update(Product)
                            .where(
                                Product.shop_id == pickai_shop.id,
                                Product.external_id.in_(challenged_ids),
                            )
                            .values(
                                stock=-1,
                                status=ProductStatus.NORMAL,
                                inventory_verified_at=None,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        session.commit()
                attempted = int(pickai_verification.get("attempted", 0) or 0)
                failed = int(pickai_verification.get("failed", 0) or 0)
                verified = int(pickai_verification.get("verified", 0) or 0)
                if (
                    attempted
                    and warnings is not None
                    and (failed > 0 or verified < attempted)
                ):
                    if pickai_verification.get("challenge"):
                        warnings.append(
                            "原店接口触发阿里云滑块，库存无法确认；"
                            "这不是商品缺货，受影响商品已标为库存未知。"
                        )
                    else:
                        warnings.append(
                            "PickAI 候选原店本轮未能完成核验，已将受影响商品标为库存未知。"
                        )
                cooldown = host_cooldown_remaining(RETAIL_BASE)
                if (
                    current_pickai_external_ids
                    and in_stock_only
                    and warnings is not None
                    and cooldown > 0
                    and not any(
                        "滑块" in warning
                        or "冷却" in warning
                        or "验证页" in warning
                        or "源站无法核验" in warning
                        for warning in warnings
                    )
                ):
                    candidate_note = (
                        f"候选目录当前发现约 {strict_candidate_total:,} 条报价；"
                        if strict_candidate_total
                        else ""
                    )
                    warnings.append(
                        f"{candidate_note}原店接口处于访问保护冷却"
                        f"（约 {max(1, int(cooldown))} 秒），"
                        "库存暂不可确认；短保护结束后会重新探测，不再锁三分钟。"
                    )
            finally:
                if strict_realtime:
                    assert strict_cancel_event is not None
                    _finish_strict_search(strict_cancel_event)
                else:
                    _LIVE_RETAIL_VERIFY_LOCK.release()
        elif in_stock_only and warnings is not None:
            warnings.append(
                "原店核验队列正忙，本次未返回未核验的缓存库存，请立即重试。"
            )
        for shop_id, external_ids in verified_retail_scopes.items():
            retail_scopes.setdefault(shop_id, set()).update(external_ids)
        if (
            reference_records
            and in_stock_only
            and not verified_retail_scopes
            and warnings is not None
        ):
            warnings.append(
                "参考目录候选未能完成店铺实时库存核验，已隐藏未经核验的库存。"
            )
    products, total = _db_search(
        session,
        keywords,
        goods_type,
        current,
        page_size,
        in_stock_only,
        source_scopes=source_scopes,
        snapshot_source_shop_ids=snapshot_source_shop_ids,
        retail_scopes=retail_scopes,
        sort=sort,
        platform=platform,
        expire_retail_stock=True,
        # 严格分类无论是否勾选“只看有货”，都只返回本轮原店响应。
        # PickAI/参考目录只负责筛店，不能作为价格或库存结果展示。
        require_pickai_origin=strict_realtime or (refresh_pickai and in_stock_only),
        verified_after=search_started_at if strict_realtime else None,
    )
    if (
        platform == "catfk"
        and in_stock_only
        and catfk_records
        and total == 0
        and warnings is not None
    ):
        warnings.append(
            f"云猫找到了 {len(catfk_records)} 条相关商品，但当前均缺货或未上架；"
            "关闭“只看有货”即可查看。"
        )
    return products, total


def ingest(
    session: Session,
    shop: Shop,
    records: list[ProductRecord],
    complete_snapshot: bool = False,
    inventory_verified: bool = True,
) -> int:
    """把归一化记录写入库；完整快照会把本次消失的旧商品标为未上架。

    ``inventory_verified=False`` 用于 PickAI/参考目录等聚合快照。聚合抓取时间
    不能冒充原店核验时间，并且不能覆盖 90 秒内刚从原店确认的价格和库存。
    """
    now = _now()
    seen_external_ids = {record.external_id for record in records}
    existing_products = session.exec(
        select(Product).where(Product.shop_id == shop.id)
    ).all()
    products_by_external_id = {
        product.external_id: product for product in existing_products
    }

    for rec in records:
        origin_category_id: int | None = None
        raw_category = rec.raw.get("category") if isinstance(rec.raw, dict) else None
        if inventory_verified and isinstance(raw_category, dict):
            try:
                parsed_category_id = int(raw_category.get("id"))
            except (TypeError, ValueError):
                pass
            else:
                if parsed_category_id >= 0:
                    origin_category_id = parsed_category_id
        product = products_by_external_id.get(rec.external_id)
        if product is None:
            product = Product(
                shop_id=shop.id,
                external_id=rec.external_id,
                name=rec.name,
                category=rec.category,
                merchant_name=rec.merchant_name,
                sale_price=rec.sale_price,
                agent_price=rec.agent_price,
                cost_price=rec.cost_price,
                stock=rec.stock,
                status=rec.status,
                is_linked=rec.is_linked,
                url=rec.url,
                first_seen_at=now,
                last_seen_at=now,
                inventory_verified_at=now if inventory_verified else None,
                origin_category_id=origin_category_id,
            )
            products_by_external_id[rec.external_id] = product
        else:
            product.name = rec.name
            product.category = rec.category
            product.merchant_name = rec.merchant_name
            preserve_origin_inventory = (
                not inventory_verified
                and product.inventory_verified_at is not None
                and now - product.inventory_verified_at
                <= PICKAI_ORIGIN_VERIFICATION_FRESHNESS
            )
            if not preserve_origin_inventory:
                product.sale_price = rec.sale_price
                product.agent_price = rec.agent_price
                product.cost_price = rec.cost_price
                preserve_known_out = (
                    rec.stock < 0
                    and rec.status == ProductStatus.NORMAL
                    and product.stock == 0
                    and product.status == ProductStatus.OUT
                )
            else:
                preserve_known_out = True
            if not preserve_known_out:
                product.stock = rec.stock
                product.status = rec.status
            product.is_linked = rec.is_linked
            product.url = rec.url
            product.last_seen_at = now
            if inventory_verified:
                product.inventory_verified_at = now
                if origin_category_id is not None:
                    product.origin_category_id = origin_category_id
        session.add(product)

    if complete_snapshot:
        for product in products_by_external_id.values():
            if product.external_id not in seen_external_ids:
                _mark_unlisted(session, product, now)

    shop.product_count = sum(
        product.status != ProductStatus.OFF
        for product in products_by_external_id.values()
    )
    if complete_snapshot:
        shop.last_synced_at = now
    session.add(shop)
    session.commit()
    return len(records)
