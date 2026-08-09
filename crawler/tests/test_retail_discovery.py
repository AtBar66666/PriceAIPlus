from __future__ import annotations

import unittest

from app.crawler.retail_discovery import RetailDiscovery
from app.crawler.shop_api import ShopApi, item_url_key, shop_token
from app.models import ProductStatus


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeDiscoverySession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.timeouts: list[float] = []
        self.responses = [
            FakeResponse(
                "uddg=https%3A%2F%2Fpay.ldxp.cn%2Fshop%2FAlpha"
                "&next=https%3A%2F%2Fpay.ldxp.cn%2Fitem%2Fgoods1"
            ),
            FakeResponse(
                "https://pay.ldxp.cn/shop/dagou.vip "
                "https://pay.ldxp.cn/shop/Alpha "
                "https://pay.ldxp.cn/item/goods2"
            ),
        ]

    def get(self, _url: str, params: dict, **_kwargs):
        self.calls.append(params["q"])
        self.timeouts.append(_kwargs["timeout"])
        return self.responses.pop(0)


class FakeRetailFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, url: str, body: dict, extra_headers: dict) -> dict:
        self.calls.append((url, body))
        if url.endswith("/goodsInfo"):
            return {
                "code": 1,
                "data": {
                    "goods_key": "goods1",
                    "goods_type": "card",
                    "name": "GPT Bug Team",
                    "price": 8,
                    "status": 1,
                    "link": "https://pay.ldxp.cn/item/goods1",
                    "user": {
                        "nickname": "自动发现店铺",
                        "token": "dagou.vip",
                        "link": "https://pay.ldxp.cn/shop/dagou.vip",
                    },
                },
            }

        goods_type = body["goods_type"]
        keywords = body["keywords"]
        if goods_type == "card" and keywords == "bug":
            return {
                "code": 1,
                "data": {
                    "total": 1,
                    "list": [
                        {
                            "goods_key": "goods1",
                            "goods_type": "card",
                            "name": "GPT Bug Team",
                            "price": 8,
                            "status": 1,
                            "link": "https://pay.ldxp.cn/item/goods1",
                        }
                    ],
                },
            }
        return {"code": 1, "data": {"total": 0, "list": []}}


class CategoryStockFetcher(FakeRetailFetcher):
    def post_json(self, url: str, body: dict, extra_headers: dict) -> dict:
        self.calls.append((url, body))
        if not url.endswith("/goodsList") or body.get("goods_type") != "card":
            return {"code": 1, "data": {"total": 0, "list": []}}
        stock = 242 if body.get("category_id") == 123026 else 0
        return {
            "code": 1,
            "data": {
                "total": 1,
                "list": [
                    {
                        "goods_key": "dl1215",
                        "goods_type": "card",
                        "name": "GPT/Codex K12教师套餐",
                        "price": 1.23,
                        "status": 1,
                        "category": {"id": 123026, "name": "k12"},
                        "extend": {"stock_count": stock, "show_stock_type": 1},
                        "link": "https://pay.ldxp.cn/item/dl1215",
                    }
                ],
            },
        }


class ItemCategoryFetcher(FakeRetailFetcher):
    def post_json(self, url: str, body: dict, extra_headers: dict) -> dict:
        self.calls.append((url, body))
        if url.endswith("/goodsInfo"):
            return {
                "code": 1,
                "data": {
                    "goods_key": "plus-current",
                    "goods_type": "card",
                    "name": "Plus 当前候选",
                    "price": 10,
                    "status": 1,
                    "category": {"id": 130949, "name": "Plus"},
                    "link": "https://pay.ldxp.cn/item/plus-current",
                    "user": {
                        "nickname": "当前原店",
                        "token": "CURRENT88",
                        "link": "https://pay.ldxp.cn/shop/CURRENT88",
                    },
                },
            }
        if (
            url.endswith("/goodsList")
            and body.get("token") == "CURRENT88"
            and body.get("keywords") == "plus"
            and body.get("category_id") == 130949
            and body.get("goods_type") == "card"
        ):
            return {
                "code": 1,
                "data": {
                    "total": 1,
                    "list": [
                        {
                            "goods_key": "plus-live",
                            "goods_type": "card",
                            "name": "ChatGPT Plus 成品号 未接码",
                            "price": 9.45,
                            "status": 1,
                            "category": {"id": 130949, "name": "Plus"},
                            "extend": {"stock_count": 17, "show_stock_type": 1},
                            "link": "https://pay.ldxp.cn/item/plus-live",
                        }
                    ],
                },
            }
        return {"code": 1, "data": {"total": 0, "list": []}}


class RetailDiscoveryTests(unittest.TestCase):
    def test_public_index_links_are_discovered_and_deduplicated(self) -> None:
        session = FakeDiscoverySession()
        result = RetailDiscovery(session=session).discover("bug team")

        self.assertEqual(result.shop_tokens, ("Alpha", "dagou.vip"))
        self.assertEqual(result.item_keys, ("goods1", "goods2"))
        self.assertEqual(
            session.calls,
            ["pay.ldxp.cn/shop bug team", "pay.ldxp.cn/item bug team"],
        )
        self.assertEqual(session.timeouts, [2.5, 2.5])

    def test_dotted_shop_tokens_and_item_urls_are_supported(self) -> None:
        self.assertEqual(shop_token("https://pay.ldxp.cn/shop/dagou.vip"), "dagou.vip")
        self.assertEqual(item_url_key("https://pay.ldxp.cn/item/goods1?from=search"), "goods1")

    def test_item_record_resolves_its_parent_shop(self) -> None:
        source = ShopApi(fetcher=FakeRetailFetcher())
        found = source.item_record("goods1")

        self.assertIsNotNone(found)
        token, record = found or ("", None)
        self.assertEqual(token, "dagou.vip")
        self.assertEqual(record.name, "GPT Bug Team")
        self.assertEqual(record.stock, -1)
        self.assertEqual(record.status, ProductStatus.NORMAL)

    def test_shop_search_uses_short_fallback_for_space_insensitive_match(self) -> None:
        fetcher = FakeRetailFetcher()
        records = ShopApi(fetcher=fetcher).search(
            "dagou.vip",
            "bugteam",
            shop_name="自动发现店铺",
        )

        self.assertEqual([record.name for record in records], ["GPT Bug Team"])
        self.assertIn("bug", {body["keywords"] for _, body in fetcher.calls})

    def test_shop_search_limits_fast_candidate_check_to_preferred_type(self) -> None:
        fetcher = FakeRetailFetcher()
        records = ShopApi(fetcher=fetcher).search(
            "dagou.vip",
            "missing",
            shop_name="自动发现店铺",
            preferred_goods_type="card",
        )

        self.assertEqual(records, [])
        self.assertEqual(
            {body["goods_type"] for url, body in fetcher.calls if url.endswith("/goodsList")},
            {"card"},
        )

    def test_shop_search_rechecks_category_scope_for_real_inventory(self) -> None:
        fetcher = CategoryStockFetcher()

        records = ShopApi(fetcher=fetcher).search(
            "PLUS123",
            "k12",
            shop_name="ai小头",
            preferred_goods_type="card",
            max_pages=1,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].external_id, "r:dl1215")
        self.assertEqual(records[0].stock, 242)
        self.assertEqual(records[0].status, ProductStatus.NORMAL)
        goods_calls = [
            body for url, body in fetcher.calls if url.endswith("/goodsList")
        ]
        self.assertEqual(
            [body.get("category_id") for body in goods_calls],
            [None, 123026],
        )

    def test_item_category_search_uses_current_origin_scope_and_inventory(self) -> None:
        fetcher = ItemCategoryFetcher()

        found = ShopApi(fetcher=fetcher).category_records_for_item(
            "https://pay.ldxp.cn/item/plus-current",
            "plus",
            "card",
        )

        self.assertIsNotNone(found)
        token, records = found or ("", [])
        self.assertEqual(token, "CURRENT88")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].external_id, "r:plus-live")
        self.assertEqual(records[0].sale_price, 9.45)
        self.assertEqual(records[0].stock, 17)
        goods_calls = [
            body for url, body in fetcher.calls if url.endswith("/goodsList")
        ]
        self.assertEqual(len(goods_calls), 1)
        self.assertEqual(goods_calls[0]["category_id"], 130949)
        self.assertEqual(goods_calls[0]["keywords"], "plus")


if __name__ == "__main__":
    unittest.main()
