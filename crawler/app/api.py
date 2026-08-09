"""FastAPI 本地服务：完整商品搜索与连接设置。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import Session, select

from .config import settings
from .crawler.pickai_catalog import PICKAI_SHOP_NAME, is_strict_realtime_query
from .db import get_session, init_db
from .direct_route import DIRECT_HOST_SUFFIXES, ensure_direct_proxy
from .models import Product, ProductStatus, Shop, SourceKind
from .pickai_index import pickai_index
from .retail_index import retail_index


app = FastAPI(title="PriceAIPlus API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if bool(getattr(app.state, "bootstrap_pickai_snapshot", False)):
        pickai_index.bootstrap_bundled_snapshot()
    if bool(getattr(app.state, "auto_pickai_sync", False)):
        # 仅显式开启时才联网；桌面版默认关闭，避免每次启动扫百余分页。
        pickai_index.start(force=False)


def _product_dict(
    product: Product,
    shop: Shop | None = None,
    *,
    verified_after: datetime | None = None,
) -> dict:
    host = (urlsplit(product.url).hostname or "").lower()
    platform = "catfk" if host == "catfk.com" else "ldxp"
    is_retail = shop is not None and shop.kind == SourceKind.PUBLIC_SHOP
    is_pickai = shop is not None and shop.name == PICKAI_SHOP_NAME
    observed_at = product.inventory_verified_at
    cutoff = verified_after
    if observed_at is not None and observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    if cutoff is not None and cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    verified = (
        cutoff is not None
        and observed_at is not None
        and observed_at >= cutoff
    )
    shop_url = (
        f"https://pay.ldxp.cn/shop/{shop.url.strip()}"
        if is_retail and shop.url.strip()
        else ""
    )
    return {
        "id": product.id,
        "shop_id": product.shop_id,
        "name": product.name,
        "category": product.category.value,
        "merchant_name": product.merchant_name,
        "sale_price": product.sale_price,
        "agent_price": product.agent_price,
        "cost_price": product.cost_price,
        "stock": product.stock,
        "status": product.status.value,
        "is_linked": product.is_linked,
        "url": product.url,
        "shop_url": shop_url,
        "source_kind": "retail" if is_retail else "pickai" if is_pickai else "source",
        "platform": platform,
        "margin": product.margin,
        "margin_pct": product.margin_pct,
        "last_seen_at": product.last_seen_at.isoformat(),
        "verified": verified,
        "verified_at": observed_at.isoformat() if observed_at is not None else None,
    }


_ORIGIN_UNAVAILABLE_MARKERS = (
    "滑块",
    "验证页",
    "访问保护冷却",
    "库存无法确认",
    "库存暂不可确认",
    "未能完成店铺实时库存核验",
)


def _origin_inventory_unavailable(warnings: list[str]) -> bool:
    """判断本次是否因原店保护而无法给出严格的实时库存结果。"""
    return any(
        marker in warning
        for warning in warnings
        for marker in _ORIGIN_UNAVAILABLE_MARKERS
    )


def _retail_shop_dict(shop: Shop) -> dict:
    token = shop.url.strip()
    return {
        "id": shop.id,
        "name": shop.name,
        "token": token,
        "url": f"https://pay.ldxp.cn/shop/{token}",
        "product_count": shop.product_count,
        "active": shop.active,
        "last_synced_at": shop.last_synced_at.isoformat() if shop.last_synced_at else None,
        "created_at": shop.created_at.isoformat(),
    }


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "has_cookie": bool(settings.cookie),
        "has_token": bool(settings.merchant_token),
        "has_catfk_token": bool(settings.catfk_merchant_token),
        "has_public_clearance": bool(settings.public_clearance_cookie),
    }


@app.get("/api/network-route")
def network_route() -> dict:
    """给隔离 Edge 提供仅监听本机的物理网卡 CONNECT 代理。"""
    result = ensure_direct_proxy()
    if result is None:
        return {
            "available": False,
            "proxy_port": 0,
            "mode": "system",
            "protected_suffixes": list(DIRECT_HOST_SUFFIXES),
        }
    port, route = result
    return {
        "available": True,
        "proxy_port": port,
        "mode": "physical_direct",
        "interface": route.interface_alias,
        "protected_suffixes": list(DIRECT_HOST_SUFFIXES),
    }


@app.get("/api/retail-index")
def retail_index_status() -> dict:
    return retail_index.status()


@app.post("/api/retail-index/refresh")
def refresh_retail_index() -> dict:
    retail_index.start()
    return retail_index.status()


@app.get("/api/pickai-index")
def pickai_index_status() -> dict:
    return pickai_index.status()


@app.post("/api/pickai-index/refresh")
def refresh_pickai_index() -> dict:
    return pickai_index.start(force=True)


class RetailShopIn(BaseModel):
    url: str


@app.get("/api/retail-shops")
def list_retail_shops(session: Session = Depends(get_session)) -> dict:
    shops = session.exec(
        select(Shop)
        .where(Shop.kind == SourceKind.PUBLIC_SHOP)
        .order_by(Shop.created_at.desc())
    ).all()
    return {
        "items": [_retail_shop_dict(shop) for shop in shops],
        "total": len(shops),
    }


@app.post("/api/retail-shops")
def create_retail_shop(body: RetailShopIn, session: Session = Depends(get_session)) -> dict:
    """添加一个公开零售店，并立即抓取整店商品。"""
    from .crawler.session import BlockedError
    from .service import _origin_shop_api, add_retail_shop, sync_retail_shop

    # 整店抓取始终使用公开匿名会话，不把 Merchant-Token 带进批量零售请求。
    source = _origin_shop_api()
    try:
        shop = add_retail_shop(session, body.url, source=source)
        count = len(sync_retail_shop(session, shop, source))
        session.refresh(shop)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except BlockedError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"零售店铺抓取失败：{exc}") from exc
    finally:
        source.fetcher.close()

    return {
        "ok": True,
        "message": f"已收录 {shop.name}，抓取 {count} 条商品。",
        "shop": _retail_shop_dict(shop),
    }


@app.post("/api/retail-shops/{shop_id}/refresh")
def refresh_retail_shop(shop_id: int, session: Session = Depends(get_session)) -> dict:
    """重新抓取一个已收录零售店的完整快照。"""
    from .crawler.session import BlockedError
    from .service import _origin_shop_api, sync_retail_shop

    shop = session.get(Shop, shop_id)
    if shop is None or shop.kind != SourceKind.PUBLIC_SHOP:
        raise HTTPException(404, "零售店铺不存在")

    source = _origin_shop_api()
    try:
        count = len(sync_retail_shop(session, shop, source))
        session.refresh(shop)
    except BlockedError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"店铺刷新失败：{exc}") from exc
    finally:
        source.fetcher.close()

    return {
        "ok": True,
        "message": f"{shop.name} 已刷新，共 {count} 条商品。",
        "shop": _retail_shop_dict(shop),
    }


@app.post("/api/retail-shops/refresh-all")
def refresh_all_retail_shops(session: Session = Depends(get_session)) -> dict:
    """顺序刷新全部零售店；单店失败不会阻断其他店铺。"""
    from .service import _origin_shop_api, sync_retail_shop

    shops = session.exec(
        select(Shop).where(
            Shop.kind == SourceKind.PUBLIC_SHOP,
            Shop.active == True,  # noqa: E712
        )
    ).all()
    source = _origin_shop_api()
    refreshed = 0
    errors: list[dict] = []
    try:
        for shop in shops:
            try:
                sync_retail_shop(session, shop, source)
                refreshed += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"shop_id": shop.id, "name": shop.name, "message": str(exc)})
    finally:
        source.fetcher.close()

    return {
        "ok": not errors,
        "message": f"已刷新 {refreshed}/{len(shops)} 个零售店铺。",
        "refreshed": refreshed,
        "total": len(shops),
        "errors": errors,
    }


@app.delete("/api/retail-shops/{shop_id}")
def delete_retail_shop(shop_id: int, session: Session = Depends(get_session)) -> dict:
    """删除零售店及其本地商品快照。"""
    shop = session.get(Shop, shop_id)
    if shop is None or shop.kind != SourceKind.PUBLIC_SHOP:
        raise HTTPException(404, "零售店铺不存在")

    products = session.exec(select(Product).where(Product.shop_id == shop_id)).all()
    for product in products:
        session.delete(product)
    session.delete(shop)
    session.commit()
    return {"ok": True, "message": f"已移除 {shop.name}。"}


@app.get("/api/live-search")
def live_search_ep(
    keywords: str,
    goods_type: str = "",
    in_stock: bool = True,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: Literal["sale_asc", "stock_desc"] = "sale_asc",
    platform: Literal["all", "ldxp", "catfk"] = "all",
    public_only: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """联网查询官方来源、参考店铺目录与公开零售报价。"""
    from .crawler.session import BlockedError
    from .service import cached_search, live_search

    query = keywords.strip()
    if not query:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "complete": False,
            "refreshing": False,
            "refreshed_at": None,
            "refresh_error": "",
            "warnings": [],
            "mode": "verify",
            "fallback_mode": None,
            "fallback_items": [],
            "fallback_total": 0,
            "verified_count": 0,
            "index_updated_at": None,
            "retail_index": retail_index.status(),
            "pickai_index": pickai_index.status(),
        }
    if platform == "catfk" and not settings.catfk_merchant_token:
        raise HTTPException(400, "未配置云猫寄售 Merchant-Token。")
    if public_only and platform == "catfk":
        raise HTTPException(400, "云猫寄售不支持后台自动轮询，请手动搜索。")

    try:
        search_warnings: list[str] = []
        if platform != "catfk":
            retail_index.defer(45)
        products, total = live_search(
            session,
            query,
            goods_type,
            page,
            page_size,
            in_stock,
            sort,
            platform,
            warnings=search_warnings,
            public_only=public_only,
            refresh_pickai=True,
        )
        refresh_state = {
            "refreshing": False,
            "refresh_started": False,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "refresh_error": "",
        }
    except BlockedError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"商品搜索失败：{exc}") from exc

    # 原店触发滑块/冷却时，严格的“只看有货”可能正确地返回 0 条。
    # 页面不能因此假装没有候选：自动附带本地聚合报价，但库存一律降级为未知。
    fallback_products: list[Product] = []
    fallback_total = 0
    fallback_mode: str | None = None
    if (
        in_stock
        and total == 0
        and not is_strict_realtime_query(query)
        and _origin_inventory_unavailable(search_warnings)
    ):
        all_candidates, _ = cached_search(
            session,
            query,
            goods_type,
            1,
            0,
            False,
            sort,
            platform,
            preserve_snapshot_stock=True,
        )
        eligible_candidates = [
            product
            for product in all_candidates
            if product.status != ProductStatus.OFF
        ]
        fallback_total = len(eligible_candidates)
        fallback_page_size = page_size if page_size > 0 else 20
        fallback_start = max(page - 1, 0) * fallback_page_size
        fallback_products = eligible_candidates[
            fallback_start : fallback_start + fallback_page_size
        ]
        if fallback_products:
            fallback_mode = "origin_unavailable"

    shop_ids = {
        product.shop_id
        for product in [*products, *fallback_products]
    }
    shops = (
        {
            shop.id: shop
            for shop in session.exec(select(Shop).where(Shop.id.in_(shop_ids))).all()
            if shop.id is not None
        }
        if shop_ids
        else {}
    )
    verified_after = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
    )
    items = [
        _product_dict(
            product,
            shops.get(product.shop_id),
            verified_after=verified_after,
        )
        for product in products
    ]
    fallback_items = [
        _product_dict(
            product,
            shops.get(product.shop_id),
            verified_after=verified_after,
        )
        for product in fallback_products
    ]
    for item in fallback_items:
        # PickAI/本地目录价格仍有参考价值，但原店本次不可达时绝不能沿用
        # 目录里的延迟库存，也不能显示旧的“缺货/有货”状态。
        item["stock"] = -1
        item["status"] = ProductStatus.NORMAL.value
        item["verified"] = False
        item["verified_at"] = None
    index_updated_at = (
        max(product.last_seen_at for product in products).isoformat()
        if products
        else None
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "complete": True,
        "warnings": search_warnings,
        "mode": "verify",
        "fallback_mode": fallback_mode,
        "fallback_items": fallback_items,
        "fallback_total": fallback_total,
        "verified_count": sum(1 for item in items if item["verified"]),
        "index_updated_at": index_updated_at,
        **refresh_state,
        "retail_index": retail_index.status(),
        "pickai_index": pickai_index.status(),
    }


@app.get("/api/cached-search")
def cached_search_ep(
    keywords: str,
    goods_type: str = "",
    in_stock: bool = True,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: Literal["sale_asc", "stock_desc"] = "sale_asc",
    platform: Literal["all", "ldxp", "catfk"] = "all",
    session: Session = Depends(get_session),
) -> dict:
    """只读本地索引；普通关键词搜索不会触发任何外部请求。"""
    from .service import cached_search

    query = keywords.strip()
    if not query:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "complete": False,
            "refreshing": False,
            "refreshed_at": None,
            "refresh_error": "",
            "warnings": [],
            "mode": "cache",
            "verified_count": 0,
            "index_updated_at": None,
            "retail_index": retail_index.status(),
            "pickai_index": pickai_index.status(),
        }

    try:
        products, total = cached_search(
            session,
            query,
            goods_type,
            page,
            page_size,
            in_stock,
            sort,
            platform,
            preserve_snapshot_stock=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"本地索引搜索失败：{exc}") from exc

    shop_ids = {product.shop_id for product in products}
    shops = (
        {
            shop.id: shop
            for shop in session.exec(select(Shop).where(Shop.id.in_(shop_ids))).all()
            if shop.id is not None
        }
        if shop_ids
        else {}
    )
    # 两分钟内来自原店的库存仍可标为已核验；PickAI 目录自己的同步时间
    # 不会写入 inventory_verified_at，因此永远不会冒充实时库存。
    verified_after = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
    )
    items = [
        _product_dict(
            product,
            shops.get(product.shop_id),
            verified_after=verified_after,
        )
        for product in products
    ]
    index_updated_at = (
        max(product.last_seen_at for product in products).isoformat()
        if products
        else None
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "complete": True,
        "refreshing": False,
        "refreshed_at": None,
        "refresh_error": "",
        "warnings": [],
        "mode": "cache",
        "verified_count": sum(1 for item in items if item["verified"]),
        "index_updated_at": index_updated_at,
        "retail_index": retail_index.status(),
        "pickai_index": pickai_index.status(),
    }


class SettingsIn(BaseModel):
    cookie: str | None = None
    merchant_token: str | None = None
    catfk_merchant_token: str | None = None


class TokenImportIn(BaseModel):
    token: str


class PublicClearanceIn(BaseModel):
    cookie: str
    user_agent: str = ""
    debug_port: int = 0


_PUBLIC_CLEARANCE_COOKIE_NAMES = {"acw_tc", "cdn_sec_tc", "acw_sc__v2"}


def _sanitize_public_clearance_cookie(raw: str) -> str:
    """只保留 ESA 通行 Cookie，拒绝任何账号/会话凭据。"""
    if not raw or len(raw) > 8192 or any(char in raw for char in "\r\n"):
        raise ValueError("真人验证 Cookie 格式无效。")
    accepted: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = (piece.strip() for piece in part.split("=", 1))
        lower = name.lower()
        allowed = (
            lower in _PUBLIC_CLEARANCE_COOKIE_NAMES
            or lower.startswith("aliyun_waf_")
            or lower.startswith("waf_")
            or lower.startswith("esa_")
        )
        if not allowed or not value or len(value) > 4096:
            continue
        if any(ord(char) < 0x21 or char in ";," for char in value):
            continue
        accepted[name] = value
    if not ({name.lower() for name in accepted} & {"acw_tc", "cdn_sec_tc"}):
        raise ValueError("浏览器尚未返回 ESA 真人验证 Cookie。")
    return "; ".join(f"{name}={value}" for name, value in accepted.items())


@app.get("/api/settings")
def get_settings() -> dict:
    def mask(value: str) -> str:
        return (value[:8] + "…" + value[-4:]) if len(value) > 14 else (value[:4] + "…") if value else ""

    return {
        "has_cookie": bool(settings.cookie),
        "cookie_preview": mask(settings.cookie),
        "has_token": bool(settings.merchant_token),
        "token_preview": mask(settings.merchant_token),
        "has_catfk_token": bool(settings.catfk_merchant_token),
        "catfk_token_preview": mask(settings.catfk_merchant_token),
        "has_public_clearance": bool(settings.public_clearance_cookie),
        "base_url": settings.base_url,
        "catfk_base_url": settings.catfk_base_url,
        "impersonate": settings.impersonate,
        "min_delay_ms": settings.min_delay_ms,
        "max_delay_ms": settings.max_delay_ms,
        "retail_index_concurrency": settings.retail_index_concurrency,
    }


@app.post("/api/public-clearance/import")
def import_public_clearance(body: PublicClearanceIn) -> dict:
    """接收隔离浏览器完成滑块后产生的匿名 ESA 通行状态。"""
    from .crawler.session import clear_host_cooldown

    try:
        cookie = _sanitize_public_clearance_cookie(body.cookie)
    except ValueError as exc:
        return {"ok": False, "reason": "invalid_cookie", "message": str(exc)}
    user_agent = body.user_agent.strip()
    if (
        len(user_agent) > 512
        or any(ord(char) < 0x20 for char in user_agent)
        or "mozilla/" not in user_agent.lower()
    ):
        return {
            "ok": False,
            "reason": "invalid_user_agent",
            "message": "真人验证浏览器标识格式无效。",
        }
    settings.set_public_clearance(cookie, user_agent)
    settings.public_browser_debug_port = (
        body.debug_port if 1024 <= body.debug_port <= 65535 else 0
    )
    clear_host_cooldown("https://pay.ldxp.cn/")
    return {
        "ok": True,
        "message": "原站真人验证已接管，正在自动重搜实时价格和库存。",
    }


@app.delete("/api/public-clearance")
def clear_public_clearance() -> dict:
    settings.set_public_clearance("", "")
    settings.public_browser_debug_port = 0
    return {"ok": True, "message": "已清除原站真人验证状态。"}


@app.post("/api/ldxp-token/import")
def import_ldxp_token(body: TokenImportIn) -> dict:
    """验证应用内登录捕获的 Token；验证成功后才替换本机凭据。"""
    from .crawler.session import BlockedError, Fetcher

    token = body.token.strip()
    if (
        not 8 <= len(token) <= 2048
        or any(ord(char) < 0x20 for char in token)
    ):
        return {
            "ok": False,
            "reason": "invalid_token",
            "message": "登录页返回的 Token 格式无效，请重新登录。",
        }

    fetcher = Fetcher(
        base_url=settings.base_url,
        merchant_token=token,
        merchant_referer=f"{settings.base_url}/merchant/",
    )
    try:
        payload = fetcher.post_json(
            "/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 1, "tags_id": 0},
        )
    except BlockedError as exc:
        return {"ok": False, "reason": "blocked", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "error",
            "message": f"验证登录状态失败：{exc}",
        }
    finally:
        fetcher.close()

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 1:
        message = payload.get("msg") if isinstance(payload, dict) else "非预期响应"
        return {
            "ok": False,
            "reason": "api_error",
            "message": f"登录尚未生效：{message}",
        }

    settings.set_token(token)
    return {
        "ok": True,
        "message": "链动小铺登录成功，Merchant-Token 已保存；不会自动启动后台索引。",
    }


@app.post("/api/catfk-token/import")
def import_catfk_token(body: TokenImportIn) -> dict:
    """验证应用内登录捕获的云猫 Token；验证成功后才替换本机凭据。"""
    from .crawler.session import BlockedError, Fetcher

    token = body.token.strip()
    if (
        not 8 <= len(token) <= 2048
        or any(ord(char) < 0x20 for char in token)
    ):
        return {
            "ok": False,
            "reason": "invalid_token",
            "message": "登录页返回的 Token 格式无效，请重新登录。",
        }

    fetcher = Fetcher(
        base_url=settings.catfk_base_url,
        merchant_token=token,
        merchant_referer=f"{settings.catfk_base_url}/merchant/",
    )
    try:
        payload = fetcher.post_json(
            "/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 1, "tags_id": 0},
        )
    except BlockedError as exc:
        return {"ok": False, "reason": "blocked", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "error",
            "message": f"验证登录状态失败：{exc}",
        }
    finally:
        fetcher.close()

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 1:
        message = payload.get("msg") if isinstance(payload, dict) else "非预期响应"
        return {
            "ok": False,
            "reason": "api_error",
            "message": f"登录尚未生效：{message}",
        }

    settings.set_catfk_token(token)
    return {
        "ok": True,
        "message": "云猫寄售登录成功，Merchant-Token 已自动保存。",
    }


@app.put("/api/settings")
def update_settings(body: SettingsIn) -> dict:
    if body.cookie is not None:
        settings.set_cookie(body.cookie)
    if body.merchant_token is not None:
        settings.set_token(body.merchant_token)
    if body.catfk_merchant_token is not None:
        settings.set_catfk_token(body.catfk_merchant_token)
    return {
        "ok": True,
        "has_cookie": bool(settings.cookie),
        "has_token": bool(settings.merchant_token),
        "has_catfk_token": bool(settings.catfk_merchant_token),
    }


@app.delete("/api/settings/ldxp-credentials")
def clear_ldxp_credentials() -> dict:
    """停用并删除本机链动账号凭据；公开匿名搜索不受影响。"""
    settings.set_token("")
    settings.set_cookie("")
    return {
        "ok": True,
        "has_token": False,
        "has_cookie": False,
        "message": "链动账号凭据已从本机删除，当前保持公开免登录模式。",
    }


@app.post("/api/test-connection")
def test_connection(
    platform: str = Query(default="ldxp", pattern="^(ldxp|catfk)$"),
) -> dict:
    """用当前令牌请求一次货源池列表。"""
    from .crawler.session import BlockedError, Fetcher

    is_catfk = platform == "catfk"
    token = (
        settings.catfk_merchant_token
        if is_catfk
        else settings.merchant_token
    )
    platform_name = "云猫寄售" if is_catfk else "链动小铺"
    if not token:
        return {
            "ok": False,
            "reason": "no_token",
            "message": f"尚未配置{platform_name} Merchant-Token，请先保存。",
        }

    fetcher = (
        Fetcher(
            base_url=settings.catfk_base_url,
            merchant_token=settings.catfk_merchant_token,
            merchant_referer=f"{settings.catfk_base_url}/merchant/",
        )
        if is_catfk
        else Fetcher()
    )
    try:
        payload = fetcher.post_json(
            "/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 20, "tags_id": 0},
        )
    except BlockedError as exc:
        return {"ok": False, "reason": "blocked", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "error", "message": f"请求异常：{exc}"}
    finally:
        fetcher.close()

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 1:
        message = payload.get("msg") if isinstance(payload, dict) else "非预期响应"
        return {
            "ok": False,
            "reason": "api_error",
            "message": f"接口返回 code={code}：{message}",
            "preview": json.dumps(payload, ensure_ascii=False)[:1000],
        }

    data = payload.get("data") or {}
    items = data.get("list") or []
    total = data.get("total", 0)
    return {
        "ok": True,
        "message": f"{platform_name}连接成功，货源池共 {total} 家供货商。",
        "total": total,
        "sample_keys": list(items[0].keys()) if items else [],
    }
