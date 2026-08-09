from __future__ import annotations

import unittest
from unittest.mock import patch

from curl_cffi.const import CurlOpt
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.api import _sanitize_public_clearance_cookie
from app.crawler.base import ProductRecord
from app.crawler.session import (
    Fetcher,
    HostThrottle,
    JsonChallengeError,
    solve_acw_challenge,
)
from app.crawler.shop_api import ShopApi, ShopClosedError, shop_token, shop_url_token
from app.models import Category, Product, ProductStatus, Shop, SourceKind
from app.service import add_retail_shop, live_search


SHOP_URL = "https://pay.ldxp.cn/shop/LV9C7XJE"


def memory_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class FakeResponse:
    def __init__(self, text: str, payload: dict | None = None) -> None:
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "application/json" if payload else "text/html"}
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeCookies:
    def __init__(self) -> None:
        self.values: list[tuple[str, str, str, str]] = []

    def set(self, name: str, value: str, domain: str, path: str) -> None:
        self.values.append((name, value, domain, path))


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.request_headers: list[dict[str, str]] = []
        self.cookies = FakeCookies()

    def request(self, method: str, url: str, **_kwargs):
        self.calls.append((method, url))
        self.request_headers.append(dict(_kwargs.get("headers") or {}))
        return self.responses.pop(0)


class NoWait:
    def wait(self) -> None:
        return None


class FakeShopFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def post_json(self, url: str, body: dict, extra_headers: dict) -> dict:
        self.calls.append((url, body, extra_headers))
        if url.endswith("/shopApi/Shop/info"):
            return {"code": 1, "data": {"nickname": "野站"}}
        if body["goods_type"] == "card":
            return {
                "code": 1,
                "data": {
                    "total": 1,
                    "list": [
                        {
                            "goods_key": "qudtro",
                            "goods_type": "card",
                            "name": "GPT Bug Team账号",
                            "price": 9,
                            "link": "https://pay.ldxp.cn/item/qudtro",
                        }
                    ],
                },
            }
        return {"code": 1, "data": {"total": 0, "list": []}}


class ClosedFlag:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRetailSource:
    instances: list["FakeRetailSource"] = []

    def __init__(self) -> None:
        self.fetcher = ClosedFlag()
        self.fetch_calls: list[tuple[str, str | None]] = []
        self.__class__.instances.append(self)

    def shop_name(self, token: str) -> str:
        if token != "LV9C7XJE":
            raise AssertionError(f"unexpected token: {token}")
        return "野站"

    def fetch(self, target: str, max_pages: int = 8, shop_name: str | None = None):
        self.fetch_calls.append((target, shop_name))
        return [
            ProductRecord(
                external_id="r:qudtro",
                name="GPT Bug Team账号",
                category=Category.CARD,
                merchant_name="野站",
                sale_price=9,
                stock=-1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/qudtro",
            )
        ]


class FakeItemInventorySource:
    def __init__(self) -> None:
        self.fetcher = ClosedFlag()
        self.search_calls: list[tuple[str, str]] = []

    def item_record(self, item_key: str):
        if item_key != "t0z53i":
            return None
        return (
            "Axship",
            ProductRecord(
                external_id="r:t0z53i",
                name="GPT Team K12 成品",
                category=Category.CARD,
                merchant_name="Axship星舰售票厅",
                sale_price=1.8,
                stock=-1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/t0z53i",
            ),
        )

    def search(
        self,
        token: str,
        keywords: str,
        _goods_type: str,
        **_kwargs,
    ) -> list[ProductRecord]:
        self.search_calls.append((token, keywords))
        return [
            ProductRecord(
                external_id="r:t0z53i",
                name="GPT Team K12 成品",
                category=Category.CARD,
                merchant_name="Axship星舰售票厅",
                sale_price=1.8,
                stock=0,
                status=ProductStatus.OUT,
                url="https://pay.ldxp.cn/item/t0z53i",
            )
        ]


class FakeClosedShopSource:
    def __init__(self) -> None:
        self.fetcher = ClosedFlag()

    def item_record(self, _item_key: str):
        raise ShopClosedError("店铺已打烊：")


class FakeClosedItemFetcher:
    def post_json(self, _url: str, _body: dict, extra_headers: dict) -> dict:
        del extra_headers
        return {"code": 0, "data": None, "msg": "店铺已打烊："}


class RetailShopTests(unittest.TestCase):
    def test_fetcher_forces_official_requests_to_use_direct_connection(self) -> None:
        with patch("app.crawler.session.cffi.Session") as session_factory:
            fetcher = Fetcher(credential_policy="public")

        session_factory.assert_called_once_with(
            impersonate=settings.impersonate,
            curl_options={CurlOpt.PROXY: ""},
        )
        self.assertIs(fetcher.session, session_factory.return_value)

    def test_host_throttle_uses_exponential_waf_cooldown(self) -> None:
        throttle = HostThrottle(0, 0)
        url = "https://pay.ldxp.cn/shopApi/Shop/info"

        throttle.penalize(url, 0.1, 1.0)
        first = throttle.cooldown_remaining(url)
        throttle.penalize(url, 0.1, 1.0)
        duplicate = throttle.cooldown_remaining(url)
        with throttle._lock:
            throttle._blocked_until_by_host["pay.ldxp.cn"] = 0
        throttle.penalize(url, 0.1, 1.0)
        second = throttle.cooldown_remaining(url)

        self.assertGreater(first, 0)
        self.assertLessEqual(duplicate, first + 0.01)
        self.assertGreater(second, first * 1.5)

    def test_public_html_challenge_only_sets_short_fixed_cooldown(self) -> None:
        fetcher = Fetcher.__new__(Fetcher)
        fetcher.credential_policy = "public"
        fetcher.base_url = "https://pay.ldxp.cn"
        fetcher.cookie = ""
        fetcher.merchant_token = ""
        fetcher.merchant_referer = "https://pay.ldxp.cn/"
        fetcher.timeout_s = 2.0
        fetcher.deadline_monotonic = None
        fetcher.cancel_event = None
        fetcher.throttle = NoWait()
        fetcher.host_throttle = HostThrottle(0, 0)
        fetcher.merchant_host_throttle = HostThrottle(0, 0)
        fetcher.session = FakeSession([FakeResponse("<html>滑动验证页面</html>")])

        with self.assertRaises(JsonChallengeError):
            fetcher.post_json(
                "https://pay.ldxp.cn/shopApi/Shop/goodsInfo",
                {"goods_key": "test", "trade_no": ""},
            )

        remaining = fetcher.host_throttle.cooldown_remaining(
            "https://pay.ldxp.cn/shopApi/Shop/goodsInfo"
        )
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, settings.public_waf_max_cooldown_s + 0.1)
        self.assertLess(remaining, settings.waf_cooldown_s)

    def test_unicode_shop_tokens_are_safe_single_path_segments(self) -> None:
        encoded = "https://pay.ldxp.cn/shop/%E4%B8%AD%E6%96%87%E5%BA%97"
        self.assertEqual(shop_url_token(encoded), "中文店")
        self.assertEqual(shop_token("中文店"), "中文店")
        self.assertEqual(shop_url_token("https://pay.ldxp.cn/shop/中文店?from=pool"), "中文店")

        rejected = [
            "https://evil.example/shop/中文店",
            "https://pay.ldxp.cn/item/中文店",
            "https://pay.ldxp.cn/shop/中文店/extra",
            "https://pay.ldxp.cn/shop/中文%2F店",
            "https://pay.ldxp.cn/shop/中文%5C店",
            "https://pay.ldxp.cn/shop/中文%252F店",
            "https://user@pay.ldxp.cn/shop/中文店",
            "中文/店",
            "中文?店",
            "中文\n店",
            "店" * 97,
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(shop_token(value), "")

    def test_shop_token_accepts_shop_url_and_rejects_non_shop_urls(self) -> None:
        self.assertEqual(shop_url_token(SHOP_URL), "LV9C7XJE")
        self.assertEqual(shop_token(SHOP_URL + "/?from=test#goods"), "LV9C7XJE")
        self.assertEqual(shop_token("LV9C7XJE"), "LV9C7XJE")
        self.assertEqual(shop_token("https://pay.ldxp.cn/item/qudtro"), "")
        self.assertEqual(shop_token("https://evil.example/shop/LV9C7XJE"), "")
        self.assertEqual(shop_token("https://pay.ldxp.cn/shop/"), "")

    def test_fetcher_solves_acw_challenge_and_replays_request_once(self) -> None:
        arg = "3BDC4DE35963425D24AB76E15F4F5AA8E5A15A6F"
        challenge = f"<html><script>var arg1='{arg}';document.cookie='acw_sc__v2='</script></html>"
        expected = "6a51fa5a952f946852c365e0178cd3ec035f5644"
        self.assertEqual(solve_acw_challenge(challenge), expected)

        fetcher = Fetcher.__new__(Fetcher)
        fetcher.cookie = ""
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [FakeResponse(challenge), FakeResponse('{"code":1}', {"code": 1})]
        )

        payload = fetcher.post_json(
            "https://pay.ldxp.cn/shopApi/Shop/info",
            {"token": "LV9C7XJE"},
        )

        self.assertEqual(payload, {"code": 1})
        self.assertEqual(len(fetcher.session.calls), 2)
        self.assertEqual(
            fetcher.session.cookies.values,
            [("acw_sc__v2", expected, "pay.ldxp.cn", "/")],
        )

    def test_fetcher_solves_same_challenge_on_main_search_domain(self) -> None:
        arg = "3BDC4DE35963425D24AB76E15F4F5AA8E5A15A6F"
        challenge = f"<html><script>var arg1='{arg}';document.cookie='acw_sc__v2='</script></html>"
        expected = "6a51fa5a952f946852c365e0178cd3ec035f5644"
        fetcher = Fetcher.__new__(Fetcher)
        fetcher.cookie = ""
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [FakeResponse(challenge), FakeResponse('{"code":1}', {"code": 1})]
        )

        payload = fetcher.post_json(
            "https://www.ldxp.cn/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 20},
        )

        self.assertEqual(payload, {"code": 1})
        self.assertEqual(
            fetcher.session.cookies.values,
            [("acw_sc__v2", expected, "www.ldxp.cn", "/")],
        )

    def test_public_shop_request_never_sends_merchant_credentials(self) -> None:
        with patch("app.crawler.shop_api.Fetcher") as fetcher_factory:
            ShopApi()
        fetcher_factory.assert_called_once_with(credential_policy="public")

        fetcher = Fetcher.__new__(Fetcher)
        fetcher.credential_policy = "public"
        fetcher.cookie = "merchant-cookie=secret"
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [FakeResponse('{"code":1}', {"code": 1})]
        )

        with (
            patch.object(settings, "merchant_token", "merchant-token-secret"),
            patch.object(settings, "public_clearance_cookie", ""),
            patch.object(settings, "public_clearance_user_agent", ""),
        ):
            payload = fetcher.post_json(
                "https://pay.ldxp.cn/shopApi/Shop/info",
                {"token": "中文店"},
                extra_headers={"Origin": "https://pay.ldxp.cn"},
            )

        self.assertEqual(payload, {"code": 1})
        sent = {key.lower(): value for key, value in fetcher.session.request_headers[0].items()}
        self.assertNotIn("merchant-token", sent)
        self.assertNotIn("cookie", sent)

    def test_public_clearance_is_scoped_to_pay_domain_only(self) -> None:
        fetcher = Fetcher.__new__(Fetcher)
        fetcher.credential_policy = "public"
        fetcher.cookie = "merchant-cookie=must-not-leak"
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [
                FakeResponse('{"code":1}', {"code": 1}),
                FakeResponse('{"code":1}', {"code": 1}),
            ]
        )
        clearance = "acw_tc=public-a; cdn_sec_tc=public-b"
        with (
            patch.object(settings, "public_clearance_cookie", clearance),
            patch.object(settings, "public_clearance_user_agent", "Mozilla/5.0 test"),
        ):
            fetcher.post_json(
                "https://pay.ldxp.cn/shopApi/Shop/info",
                {"token": "demo"},
            )
            fetcher.post_json("https://pickai.cc/api/public", {})

        pay_headers = {
            key.lower(): value for key, value in fetcher.session.request_headers[0].items()
        }
        other_headers = {
            key.lower(): value for key, value in fetcher.session.request_headers[1].items()
        }
        self.assertEqual(pay_headers.get("cookie"), clearance)
        self.assertEqual(pay_headers.get("user-agent"), "Mozilla/5.0 test")
        self.assertNotIn("cookie", other_headers)
        self.assertNotIn("user-agent", other_headers)

    def test_public_clearance_sanitizer_rejects_login_cookies(self) -> None:
        sanitized = _sanitize_public_clearance_cookie(
            "auth-token=secret; PHPSESSID=secret; acw_tc=abc123; cdn_sec_tc=def456"
        )
        self.assertEqual(sanitized, "acw_tc=abc123; cdn_sec_tc=def456")
        with self.assertRaises(ValueError):
            _sanitize_public_clearance_cookie("auth-token=secret; PHPSESSID=secret")

    def test_merchant_source_request_keeps_configured_credentials(self) -> None:
        fetcher = Fetcher.__new__(Fetcher)
        fetcher.credential_policy = "merchant"
        fetcher.cookie = "merchant-cookie=secret"
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [FakeResponse('{"code":1}', {"code": 1})]
        )

        with patch.object(settings, "merchant_token", "merchant-token-secret"):
            payload = fetcher.post_json(
                f"{settings.base_url}/merchantApi/GoodsPool/list",
                {"current": 1, "pageSize": 20},
            )

        self.assertEqual(payload, {"code": 1})
        sent = {key.lower(): value for key, value in fetcher.session.request_headers[0].items()}
        self.assertEqual(sent["merchant-token"], "merchant-token-secret")
        self.assertEqual(sent["cookie"], "merchant-cookie=secret")

    def test_catfk_credentials_are_scoped_to_catfk_host(self) -> None:
        fetcher = Fetcher.__new__(Fetcher)
        fetcher.credential_policy = "merchant"
        fetcher.base_url = "https://catfk.com"
        fetcher.merchant_token = "catfk-token-secret"
        fetcher.cookie = ""
        fetcher.throttle = NoWait()
        fetcher.session = FakeSession(
            [
                FakeResponse('{"code":1}', {"code": 1}),
                FakeResponse('{"code":1}', {"code": 1}),
            ]
        )

        fetcher.post_json(
            "https://catfk.com/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 20},
        )
        fetcher.post_json(
            "https://www.ldxp.cn/merchantApi/GoodsPool/list",
            {"current": 1, "pageSize": 20},
        )

        catfk_headers = {
            key.lower(): value
            for key, value in fetcher.session.request_headers[0].items()
        }
        ldxp_headers = {
            key.lower(): value
            for key, value in fetcher.session.request_headers[1].items()
        }
        self.assertEqual(catfk_headers["merchant-token"], "catfk-token-secret")
        self.assertNotIn("merchant-token", ldxp_headers)

    def test_shop_api_fetches_all_goods_types_and_maps_products(self) -> None:
        fetcher = FakeShopFetcher()
        records = ShopApi(fetcher=fetcher).fetch(SHOP_URL)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].external_id, "r:qudtro")
        self.assertEqual(records[0].merchant_name, "野站")
        self.assertEqual(records[0].sale_price, 9)
        self.assertEqual(records[0].stock, -1)
        self.assertEqual(records[0].url, "https://pay.ldxp.cn/item/qudtro")

        goods_calls = [call for call in fetcher.calls if call[0].endswith("/goodsList")]
        self.assertEqual(len(goods_calls), 4)
        self.assertEqual({call[1]["goods_type"] for call in goods_calls}, {
            "card", "article", "resource", "equity",
        })
        self.assertTrue(all(call[1]["token"] == "LV9C7XJE" for call in goods_calls))
        self.assertTrue(all(call[2]["Origin"] == "https://pay.ldxp.cn" for call in fetcher.calls))

    def test_shop_api_skips_empty_goods_types_from_shop_counts(self) -> None:
        class CategoryAwareFetcher(FakeShopFetcher):
            def post_json(self, url: str, body: dict, extra_headers: dict) -> dict:
                if url.endswith("/shopApi/Shop/info"):
                    self.calls.append((url, body, extra_headers))
                    return {
                        "code": 1,
                        "data": {
                            "nickname": "野站",
                            "goods_count": 1,
                            "card_count": 1,
                            "article_count": 0,
                            "resource_count": 0,
                            "equity_count": 0,
                        },
                    }
                return super().post_json(url, body, extra_headers)

        fetcher = CategoryAwareFetcher()
        records = ShopApi(fetcher=fetcher).fetch(SHOP_URL, shop_name="野站")
        goods_calls = [
            call for call in fetcher.calls if call[0].endswith("/goodsList")
        ]

        self.assertEqual(len(records), 1)
        self.assertEqual([call[1]["goods_type"] for call in goods_calls], ["card"])

    def test_shop_url_live_search_imports_snapshot_and_returns_products(self) -> None:
        FakeRetailSource.instances.clear()
        engine = memory_engine()
        with Session(engine) as session:
            with patch("app.service.ShopApi", FakeRetailSource):
                products, total = live_search(
                    session,
                    SHOP_URL,
                    in_stock_only=False,
                )

            self.assertEqual(total, 1)
            self.assertEqual([product.name for product in products], ["GPT Bug Team账号"])
            shop = session.exec(select(Shop)).one()
            stored = session.exec(select(Product)).one()
            self.assertEqual(shop.name, "野站")
            self.assertEqual(shop.url, "LV9C7XJE")
            self.assertEqual(shop.kind, SourceKind.PUBLIC_SHOP)
            self.assertEqual(shop.product_count, 1)
            self.assertEqual(stored.shop_id, shop.id)
            self.assertEqual(stored.merchant_name, "野站")
            self.assertEqual(FakeRetailSource.instances[0].fetch_calls, [("LV9C7XJE", "野站")])
            self.assertTrue(FakeRetailSource.instances[0].fetcher.closed)

    def test_item_url_live_search_uses_shop_list_inventory(self) -> None:
        engine = memory_engine()
        source = FakeItemInventorySource()
        with Session(engine) as session:
            with patch("app.service.ShopApi", return_value=source):
                products, total = live_search(
                    session,
                    "https://pay.ldxp.cn/item/t0z53i",
                    page_size=0,
                    in_stock_only=False,
                )

        self.assertEqual(total, 1)
        self.assertEqual(products[0].sale_price, 1.8)
        self.assertEqual(products[0].stock, 0)
        self.assertEqual(products[0].status, ProductStatus.OUT)
        self.assertEqual(source.search_calls, [("Axship", "GPT Team K12 成品")])
        self.assertTrue(source.fetcher.closed)

    def test_closed_shop_message_overrides_stale_positive_inventory(self) -> None:
        api = ShopApi(fetcher=FakeClosedItemFetcher())
        with self.assertRaisesRegex(ShopClosedError, "店铺已打烊"):
            api.item_status("q462rm")

        engine = memory_engine()
        source = FakeClosedShopSource()
        with Session(engine) as session:
            shop = Shop(name="畅用plus", url="ooopp", kind=SourceKind.PUBLIC_SHOP)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:q462rm",
                    name="无质保 bugTeam 子号",
                    stock=121,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/q462rm",
                )
            )
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:another",
                    name="同店其他商品",
                    stock=8,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()

            with patch("app.service.ShopApi", return_value=source):
                products, total = live_search(
                    session,
                    "https://pay.ldxp.cn/item/q462rm",
                    page_size=0,
                    in_stock_only=False,
                )

            stored = session.exec(
                select(Product).where(Product.shop_id == shop.id)
            ).all()

        self.assertEqual(products, [])
        self.assertEqual(total, 0)
        self.assertTrue(all(product.stock == 0 for product in stored))
        self.assertTrue(all(product.status == ProductStatus.OUT for product in stored))
        self.assertTrue(source.fetcher.closed)

    def test_add_retail_shop_rejects_item_url_before_writing(self) -> None:
        engine = memory_engine()
        with Session(engine) as session:
            with self.assertRaisesRegex(ValueError, "无效的店铺地址"):
                add_retail_shop(session, "https://pay.ldxp.cn/item/qudtro")
            self.assertEqual(session.exec(select(Shop)).all(), [])

    def test_shop_url_search_only_returns_current_snapshot(self) -> None:
        FakeRetailSource.instances.clear()
        engine = memory_engine()
        with Session(engine) as session:
            shop = Shop(name="野站", kind=SourceKind.PUBLIC_SHOP, url="LV9C7XJE")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id,
                    external_id="r:disappeared",
                    name="上次抓取后已消失的商品",
                    stock=-1,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()

            with patch("app.service.ShopApi", FakeRetailSource):
                products, total = live_search(session, SHOP_URL, page_size=0, in_stock_only=False)

            self.assertEqual(total, 1)
            self.assertEqual([product.external_id for product in products], ["r:qudtro"])
            disappeared = session.exec(
                select(Product).where(Product.external_id == "r:disappeared")
            ).one()
            self.assertEqual(disappeared.status, ProductStatus.OFF)


if __name__ == "__main__":
    unittest.main()
