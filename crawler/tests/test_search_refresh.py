from __future__ import annotations

import threading
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import (
    TokenImportIn,
    _startup,
    app,
    cached_search_ep,
    clear_ldxp_credentials,
    import_ldxp_token,
    live_search_ep,
)
from app.models import Category, Product, ProductStatus, Shop, SourceKind
from app.search_refresh import SearchRefreshCoordinator
from app.service import cached_search, live_search


def memory_engine():
    db_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db_engine)
    return db_engine


def cached_product(shop_id: int = 1) -> Product:
    return Product(
        id=1,
        shop_id=shop_id,
        external_id="cached",
        name="GPT 本地缓存商品",
        category=Category.CARD,
        merchant_name="缓存店铺",
        sale_price=8,
        stock=-1,
        status=ProductStatus.NORMAL,
        url="https://pay.ldxp.cn/item/cached",
    )


class SearchRefreshTests(unittest.TestCase):

    def test_clear_ldxp_credentials_keeps_public_mode_available(self) -> None:
        with (
            patch("app.config.Settings.set_token") as set_token,
            patch("app.config.Settings.set_cookie") as set_cookie,
        ):
            result = clear_ldxp_credentials()

        self.assertEqual(set_token.call_args.args[-1], "")
        self.assertEqual(set_cookie.call_args.args[-1], "")
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_token"])
        self.assertFalse(result["has_cookie"])
        self.assertIn("免登录", result["message"])
    def test_chatgpt_search_indexes_pickai_but_hides_it_without_origin_verification(self) -> None:
        class FakePickAICatalog:
            request_count = 1

            def __init__(self, *args, **kwargs) -> None:
                pass

            def search(self, keywords: str, **kwargs):
                self.keywords = keywords
                return ([
                    {
                        "id": 99,
                        "raw_name": "GPT Plus 实时报价",
                        "shop_name": "PickAI 店",
                        "price": "9.9",
                        "stock": "库存 12",
                        "item_url": "https://pay.ldxp.cn/item/live99",
                        "product_type_ids": [3],
                        "product_type_names": ["ChatGPT Plus"],
                        "catalog_categories": ["ChatGPT"],
                    }
                ], 1)

            def search_chatgpt(self, keywords: str, **kwargs):
                return self.search(keywords, **kwargs)

            def search_current_strict(self, keywords: str, **kwargs):
                return self.search(keywords, **kwargs)

            def close(self) -> None:
                pass

        class EmptyReference:
            fetcher = type("Fetcher", (), {"close": lambda _self: None})()

            def search(self, *args, **kwargs):
                return []

        db_engine = memory_engine()
        with Session(db_engine) as session:
            with (
                patch("app.service.PickAICatalog", FakePickAICatalog),
                patch("app.service.settings.merchant_token", "leftover-token"),
                patch(
                    "app.service.SourceSquare",
                    side_effect=AssertionError(
                        "strict public search must not use account credentials"
                    ),
                ),
                patch("app.service.ReferenceCatalog", EmptyReference),
                patch("app.service._discover_retail_matches", return_value={}),
                patch("app.service._verify_pickai_inventory", return_value={"attempted": 0, "verified": 0, "unavailable": 0, "failed": 0}),
            ):
                products, total = live_search(
                    session,
                    "GPT Plus",
                    page_size=20,
                    in_stock_only=False,
                    platform="ldxp",
                    refresh_pickai=True,
                )
                indexed = session.exec(
                    select(Product).where(Product.external_id == "p:live99")
                ).one()

        self.assertEqual(total, 0)
        self.assertEqual(products, [])
        self.assertEqual(indexed.sale_price, 9.9)
        self.assertEqual(indexed.stock, 12)
        self.assertIsNone(indexed.inventory_verified_at)

    def test_cached_endpoint_returns_before_any_live_search(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="PickAI · 公开报价索引", kind=SourceKind.SOURCE_SQUARE)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = cached_product(shop.id or 0)
            product.external_id = "p:cached"
            product.stock = 15
            session.add(product)
            session.commit()
            with (
                patch("app.service.cached_search", return_value=([product], 1)) as cached,
                patch("app.service.live_search", side_effect=AssertionError("live search used")),
                patch("app.api.retail_index.status", return_value={"state": "idle"}),
                patch("app.api.pickai_index.status", return_value={"state": "ready"}),
            ):
                result = cached_search_ep(
                    keywords="plus",
                    page=1,
                    page_size=50,
                    session=session,
                )

        cached.assert_called_once()
        self.assertEqual(result["mode"], "cache")
        self.assertTrue(result["complete"])
        self.assertFalse(result["refreshing"])
        self.assertFalse(result["items"][0]["verified"])

    def test_startup_never_starts_retail_index_automatically(self) -> None:
        with (
            patch("app.api.init_db") as init_db,
            patch("app.api.retail_index.start") as start_index,
            patch("app.api.pickai_index.bootstrap_bundled_snapshot") as bootstrap,
            patch("app.api.pickai_index.start") as pickai_start,
            patch.object(app.state, "bootstrap_pickai_snapshot", True, create=True),
            patch.object(app.state, "auto_pickai_sync", False, create=True),
        ):
            _startup()

        init_db.assert_called_once()
        start_index.assert_not_called()
        bootstrap.assert_called_once()
        pickai_start.assert_not_called()

    def test_login_capture_validates_before_persisting_token(self) -> None:
        with (
            patch("app.crawler.session.Fetcher") as fetcher_factory,
            patch("app.config.Settings.set_token") as set_token,
            patch("app.api.retail_index.start") as start_index,
        ):
            fetcher_factory.return_value.post_json.return_value = {
                "code": 1,
                "data": {"list": [], "total": 0},
            }
            result = import_ldxp_token(
                TokenImportIn(token="new-valid-token-value")
            )

        self.assertTrue(result["ok"])
        set_token.assert_called_once_with("new-valid-token-value")
        start_index.assert_not_called()
        fetcher_factory.return_value.close.assert_called_once()

    def test_login_capture_does_not_persist_rejected_token(self) -> None:
        with (
            patch("app.crawler.session.Fetcher") as fetcher_factory,
            patch("app.config.Settings.set_token") as set_token,
        ):
            fetcher_factory.return_value.post_json.return_value = {
                "code": 401,
                "msg": "请先登录",
                "data": None,
            }
            result = import_ldxp_token(
                TokenImportIn(token="expired-token-value")
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "api_error")
        set_token.assert_not_called()

    def test_cached_search_performs_no_network_work(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="缓存店铺", kind=SourceKind.PUBLIC_SHOP, url="cached-shop")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(cached_product(shop.id or 0))
            session.commit()

            with (
                patch("app.service.SourceSquare", side_effect=AssertionError("network source used")),
                patch("app.service.ShopApi", side_effect=AssertionError("shop API used")),
            ):
                products, total = cached_search(
                    session,
                    "GPT",
                    page_size=0,
                    in_stock_only=False,
                )

        self.assertEqual(total, 1)
        self.assertEqual([product.name for product in products], ["GPT 本地缓存商品"])

    def test_cached_search_keeps_last_known_positive_stock_as_snapshot(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="缓存店铺", kind=SourceKind.PUBLIC_SHOP, url="cached-shop")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = cached_product(shop.id or 0)
            product.stock = 6
            product.last_seen_at -= timedelta(hours=2)
            session.add(product)
            session.commit()

            products, total = cached_search(
                session,
                "GPT",
                page_size=20,
                in_stock_only=True,
                preserve_snapshot_stock=True,
            )

        self.assertEqual(total, 1)
        self.assertEqual(products[0].stock, 6)

    def test_refresh_deduplicates_normalized_query_and_honors_ttl(self) -> None:
        db_engine = memory_engine()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[tuple[str, str]] = []

        def refresh(_session, keywords: str, goods_type: str, **_kwargs):
            calls.append((keywords, goods_type))
            entered.set()
            release.wait(2)
            finished.set()
            return [], 0

        coordinator = SearchRefreshCoordinator(
            db_engine=db_engine,
            refresh_fn=refresh,
            ttl=timedelta(minutes=5),
        )
        first = coordinator.start("  GPT   Plus  ", "card")
        self.assertTrue(first["refreshing"])
        self.assertTrue(entered.wait(1))

        duplicate = coordinator.start("gpt plus", "CARD")
        self.assertTrue(duplicate["refreshing"])
        self.assertEqual(len(calls), 1)

        release.set()
        self.assertTrue(finished.wait(2))
        for _ in range(50):
            if not coordinator.status("gpt plus", "card")["refreshing"]:
                break
            time.sleep(0.01)

        completed = coordinator.status("gpt plus", "card")
        self.assertFalse(completed["refreshing"])
        self.assertIsNotNone(completed["refreshed_at"])
        coordinator.start("GPT PLUS", "card")
        self.assertEqual(len(calls), 1)

    def test_search_always_uses_live_pipeline(self) -> None:
        db_engine = memory_engine()
        product = cached_product()
        with Session(db_engine) as session:
            with (
                patch("app.api.settings.merchant_token", "ldxp-token"),
                patch("app.service.live_search", return_value=([product], 1)) as live,
                patch("app.api.retail_index.status", return_value={"state": "indexing"}),
                patch("app.api.retail_index.defer") as defer,
            ):
                result = live_search_ep(
                    keywords="GPT",
                    goods_type="",
                    in_stock=False,
                    page=1,
                    page_size=0,
                    session=session,
                )

        live.assert_called_once_with(
            session,
            "GPT",
            "",
            1,
            0,
            False,
            "sale_asc",
            "all",
            warnings=[],
            public_only=False,
            refresh_pickai=True,
        )
        defer.assert_called_once_with(45)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["refreshing"])
        self.assertTrue(result["complete"])
        self.assertIsNotNone(result["refreshed_at"])
        self.assertEqual(result["mode"], "verify")
        self.assertEqual(result["items"][0]["name"], "GPT 本地缓存商品")

    def test_search_returns_empty_when_nothing_was_confirmed(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="缓存店铺", kind=SourceKind.PUBLIC_SHOP, url="cached-shop")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(cached_product(shop.id or 0))
            session.commit()
            with (
                patch("app.api.settings.merchant_token", "ldxp-token"),
                patch("app.service.live_search", return_value=([], 0)) as live,
                patch("app.api.retail_index.status", return_value={"state": "indexing"}),
                patch("app.api.retail_index.defer"),
            ):
                result = live_search_ep(
                    keywords="GPT",
                    goods_type="",
                    in_stock=False,
                    page=1,
                    page_size=20,
                    session=session,
                )

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)

    def test_chatgpt_cooldown_never_returns_delayed_stock_candidates(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="缓存店铺", kind=SourceKind.PUBLIC_SHOP, url="cached-shop")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = cached_product(shop.id or 0)
            product.name = "ChatGPT Plus · GPT Plus 成品号"
            product.stock = 88
            session.add(product)
            session.commit()

            def blocked_live(*_args, warnings=None, **_kwargs):
                assert warnings is not None
                warnings.append(
                    "原店接口处于访问保护冷却（约 176 秒），库存暂不可确认。"
                )
                return [], 0

            with (
                patch("app.service.live_search", side_effect=blocked_live),
                patch("app.api.retail_index.status", return_value={"state": "idle"}),
                patch("app.api.pickai_index.status", return_value={"state": "ready"}),
                patch("app.api.retail_index.defer"),
            ):
                result = live_search_ep(
                    keywords="GPT",
                    goods_type="",
                    in_stock=True,
                    page=1,
                    page_size=20,
                    session=session,
                )

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["fallback_mode"])
        self.assertEqual(result["fallback_total"], 0)
        self.assertEqual(result["fallback_items"], [])

    def test_strict_shortcuts_never_fall_back_to_delayed_stock(self) -> None:
        for keyword in ("K12", "邮箱", "接码"):
            with self.subTest(keyword=keyword):
                db_engine = memory_engine()
                with Session(db_engine) as session:
                    shop = Shop(
                        name="缓存店铺",
                        kind=SourceKind.PUBLIC_SHOP,
                        url="cached-shop",
                    )
                    session.add(shop)
                    session.commit()
                    session.refresh(shop)
                    product = cached_product(shop.id or 0)
                    product.name = (
                        "GPT Team K12 成品"
                        if keyword == "K12"
                        else (
                            "Gmail / Google 邮箱 · Gmail 独享账号"
                            if keyword == "邮箱"
                            else "OpenAI/ChatGPT接码 · 短信验证码"
                        )
                    )
                    product.stock = 88
                    session.add(product)
                    session.commit()

                    def blocked_live(*_args, warnings=None, **_kwargs):
                        assert warnings is not None
                        warnings.append("原店库存暂无法确认。")
                        return [], 0

                    with (
                        patch("app.service.live_search", side_effect=blocked_live),
                        patch("app.service.cached_search") as cached,
                        patch("app.api.retail_index.status", return_value={"state": "idle"}),
                        patch("app.api.pickai_index.status", return_value={"state": "ready"}),
                        patch("app.api.retail_index.defer"),
                    ):
                        result = live_search_ep(
                            keywords=keyword,
                            goods_type="",
                            in_stock=True,
                            page=1,
                            page_size=20,
                            session=session,
                        )

                cached.assert_not_called()
                self.assertEqual(result["items"], [])
                self.assertIsNone(result["fallback_mode"])
                self.assertEqual(result["fallback_items"], [])

    def test_default_search_does_not_read_local_snapshot(self) -> None:
        db_engine = memory_engine()
        product = cached_product()
        with Session(db_engine) as session:
            with (
                patch("app.api.settings.merchant_token", ""),
                patch("app.api.settings.catfk_merchant_token", ""),
                patch("app.service.cached_search", return_value=([product], 1)) as cached,
                patch("app.service.live_search", return_value=([product], 1)) as live,
                patch("app.api.retail_index.status", return_value={"state": "idle"}),
                patch("app.api.retail_index.defer") as defer,
            ):
                result = live_search_ep(
                    keywords="GPT",
                    goods_type="",
                    in_stock=True,
                    page=1,
                    page_size=20,
                    session=session,
                )

        cached.assert_not_called()
        live.assert_called_once_with(
            session,
            "GPT",
            "",
            1,
            20,
            True,
            "sale_asc",
            "all",
            warnings=[],
            public_only=False,
            refresh_pickai=True,
        )
        defer.assert_called_once_with(45)
        self.assertEqual(result["mode"], "verify")
        self.assertIsNotNone(result["refreshed_at"])

    def test_catfk_only_live_search_skips_ldxp_index_deferral(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            with (
                patch("app.api.settings.catfk_merchant_token", "catfk-token"),
                patch("app.service.live_search", return_value=([], 0)) as live,
                patch("app.api.retail_index.status", return_value={"state": "idle"}),
                patch("app.api.retail_index.defer") as defer,
            ):
                live_search_ep(
                    keywords="GPT",
                    goods_type="",
                    in_stock=True,
                    page=1,
                    page_size=20,
                    platform="catfk",
                    session=session,
                )

        self.assertEqual(live.call_args.args[-1], "catfk")
        defer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
