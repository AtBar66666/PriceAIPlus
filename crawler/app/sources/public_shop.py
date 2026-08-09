from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from ..fetcher import Fetcher

# Public shop fronts are HTML. Selectors are kept in one place so they are easy
# to re-point when the markup shifts; nothing else in the codebase parses HTML.
ITEM_SELECTOR = ".goods-item, .product-item, li.item"
NAME_SELECTOR = ".title, .goods-name, .name, h3"
PRICE_SELECTOR = ".price, .goods-price, .sale-price"
STOCK_SELECTOR = ".stock, .inventory"
LINK_SELECTOR = "a"


def _text(node, selector: str) -> str | None:
    if node is None:
        return None
    found = node.css_first(selector)
    if found is None:
        return None
    text = found.text(strip=True)
    return text or None


def _price(raw: str | None):
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    try:
        return round(float(cleaned), 4)
    except ValueError:
        return None


def crawl(fetcher: Fetcher, shop_url: str, shop_name: str | None = None) -> Iterable[dict[str, Any]]:
    resp = fetcher.get(shop_url, referer=shop_url)
    tree = HTMLParser(resp.text)
    for idx, node in enumerate(tree.css(ITEM_SELECTOR)):
        name = _text(node, NAME_SELECTOR)
        if not name:
            continue
        link_node = node.css_first(LINK_SELECTOR)
        href = link_node.attributes.get("href") if link_node else None
        url = urljoin(shop_url, href) if href else shop_url
        yield {
            "external_id": f"pub-{shop_name or shop_url}-{href or idx}",
            "name": name,
            "category": None,
            "shop_name": shop_name,
            "cost_price": None,
            "agent_price": None,
            "sale_price": _price(_text(node, PRICE_SELECTOR)),
            "stock": None,
            "connected": False,
            "status": None,
            "source": "public_shop",
            "url": url,
        }
