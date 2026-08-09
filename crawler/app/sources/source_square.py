from __future__ import annotations

from typing import Any, Iterable

from ..config import BASE_URL, SOURCE_SQUARE_PATH
from ..fetcher import Fetcher

# The source_square endpoint returns JSON. Exact key names vary by site version,
# so we look through a list of likely keys instead of hardcoding one guess.
_KEYS = {
    "external_id": ("id", "goods_id", "product_id", "gid"),
    "name": ("name", "goods_name", "title", "product_name"),
    "category": ("category", "type", "cate_name", "class_name"),
    "shop_name": ("merchant", "shop_name", "seller", "store_name", "nickname"),
    "cost_price": ("cost_price", "my_cost_price", "cost", "purchase_price"),
    "agent_price": ("agent_price", "proxy_price", "agent", "price"),
    "stock": ("stock", "inventory", "num", "count", "quantity"),
    "connected": ("connected", "is_connected", "docked", "is_docked"),
    "status": ("status", "state", "status_text"),
}


def _pick(row: dict[str, Any], keys: tuple[str, ...], default=None):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _to_float(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_id": f"src-{_pick(row, _KEYS['external_id'], '')}",
        "name": _pick(row, _KEYS["name"], ""),
        "category": _pick(row, _KEYS["category"]),
        "shop_name": _pick(row, _KEYS["shop_name"]),
        "cost_price": _to_float(_pick(row, _KEYS["cost_price"])),
        "agent_price": _to_float(_pick(row, _KEYS["agent_price"])),
        "stock": _to_int(_pick(row, _KEYS["stock"])),
        "connected": bool(_pick(row, _KEYS["connected"], False)),
        "status": _pick(row, _KEYS["status"]),
        "source": "source_square",
        "url": None,
    }


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Dig the list of items out of whatever envelope the API wraps them in."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "list", "rows", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for inner in ("list", "rows", "items", "data"):
                    if isinstance(value.get(inner), list):
                        return value[inner]
    return []


def crawl(fetcher: Fetcher, max_pages: int = 5, page_size: int = 50) -> Iterable[dict[str, Any]]:
    url = f"{BASE_URL}{SOURCE_SQUARE_PATH}"
    referer = url
    for page in range(1, max_pages + 1):
        resp = fetcher.get(
            url,
            params={"page": page, "limit": page_size, "page_size": page_size},
            referer=referer,
        )
        payload = resp.json()
        rows = _extract_rows(payload)
        if not rows:
            break
        for row in rows:
            item = normalize(row)
            if item["external_id"] != "src-":
                yield item
        if len(rows) < page_size:
            break
