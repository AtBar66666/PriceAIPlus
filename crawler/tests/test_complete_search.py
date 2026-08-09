from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.crawler.base import ProductRecord
from app.crawler.reference_catalog import ReferenceCatalog
from app.crawler.retail_discovery import RetailDiscoveryResult
from app.crawler.shop_api import ShopApi
from app.crawler.source_square import SourceSquare
from app.models import Category, Product, ProductStatus, Shop, SourceKind
from app.service import _discover_retail_matches, cached_search, live_search


def raw_item(index: int, name: str = "GPT 商品") -> dict:
    return {
        "id": index,
        "name": f"{name} {index}",
        "goods_type": "card",
        "price": 10,
        "cost_price": 8,
        "stock_count": 1,
        "status": 1,
        "user": {"nickname": "测试商家"},
        "link": f"https://pay.ldxp.cn/item/{index}",
    }


class PagingFetcher:
    def __init__(self, pages: dict[int, list[dict]], total: int) -> None:
        self.pages = pages
        self.total = total
        self.currents: list[int] = []

    def post_json(self, _path: str, body: dict) -> dict:
        current = body["current"]
        self.currents.append(current)
        return {
            "code": 1,
            "data": {"list": self.pages.get(current, []), "total": self.total},
        }


class DirectoryPagingFetcher:
    def __init__(self) -> None:
        self.currents: list[int] = []
        self.pages = {
            1: [
                {
                    "goods_count": 3,
                    "user": {
                        "nickname": "中文店铺",
                        "link": "https://pay.ldxp.cn/shop/中文店",
                    },
                },
                {
                    "goods_count": 5,
                    "user": {
                        "nickname": "Alpha 店",
                        "link": "https://pay.ldxp.cn/shop/Alpha",
                    },
                },
            ],
            2: [
                {
                    "goods_count": 7,
                    "user": {
                        "nickname": "重复项",
                        "link": "https://pay.ldxp.cn/shop/alpha",
                    },
                },
                {
                    "goods_count": 99,
                    "user": {
                        "nickname": "外域伪造",
                        "link": "https://evil.example/shop/not-allowed",
                    },
                },
            ],
        }

    def post_json(self, path: str, body: dict) -> dict:
        self.assert_pool_request(path, body)
        current = body["current"]
        self.currents.append(current)
        return {
            "code": 1,
            "data": {"list": self.pages.get(current, []), "total": 4},
        }

    @staticmethod
    def assert_pool_request(path: str, body: dict) -> None:
        if path != "/merchantApi/GoodsPool/list" or body.get("tags_id") != 0:
            raise AssertionError(f"unexpected directory request: {path} {body}")


class ClosedFlag:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCompleteSource:
    instances: list["FakeCompleteSource"] = []

    def __init__(self, page_size: int = 200) -> None:
        self.page_size = page_size
        self.fetcher = ClosedFlag()
        self.__class__.instances.append(self)

    def search(
        self,
        keywords: str,
        goods_type: str = "",
        current: int = 1,
        page_size: int = 200,
    ):
        self.keywords = keywords
        self.goods_type = goods_type
        self.requested_page = current
        self.requested_page_size = page_size
        records = [
            ProductRecord(
                external_id="new",
                name="GPT 本次结果",
                category=Category.CARD,
                merchant_name="本次商家",
                sale_price=10,
                stock=1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/new",
            )
        ]
        return records, len(records)


class FakeFailingSource:
    def __init__(self, *_args, **_kwargs) -> None:
        self.fetcher = ClosedFlag()

    def search(self, *_args, **_kwargs):
        raise RuntimeError("令牌已失效")


class FakeMultiPlatformSource:
    instances: list["FakeMultiPlatformSource"] = []

    def __init__(self, page_size: int = 200, *, base_url: str | None = None, **_kwargs) -> None:
        self.page_size = page_size
        self.base_url = base_url
        self.fetcher = ClosedFlag()
        self.__class__.instances.append(self)

    def search(
        self,
        _keywords: str,
        _goods_type: str = "",
        current: int = 1,
        page_size: int = 200,
    ):
        del current, page_size
        is_catfk = self.base_url is not None
        records = [
            ProductRecord(
                external_id="catfk-result" if is_catfk else "ldxp-result",
                name="GPT 云猫结果" if is_catfk else "GPT 链动结果",
                category=Category.CARD,
                merchant_name="云猫商家" if is_catfk else "链动商家",
                sale_price=12 if is_catfk else 10,
                stock=3 if is_catfk else 2,
                status=ProductStatus.NORMAL,
                url=(
                    "https://catfk.com/item/catfk-result"
                    if is_catfk
                    else "https://pay.ldxp.cn/item/ldxp-result"
                ),
            )
        ]
        return records, len(records)


class FakeCaseSensitiveCatfkSource:
    instances: list["FakeCaseSensitiveCatfkSource"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.fetcher = ClosedFlag()
        self.calls: list[tuple[str, str]] = []
        self.__class__.instances.append(self)

    def search(
        self,
        keywords: str,
        goods_type: str = "",
        current: int = 1,
        page_size: int = 200,
    ):
        del current, page_size
        self.calls.append((keywords, goods_type))
        if keywords != "K12" or goods_type != "card":
            return [], 0
        records = [
            ProductRecord(
                external_id="catfk-k12-out",
                name="ChatGPT K12 缺货商品",
                category=Category.CARD,
                merchant_name="云猫商家",
                sale_price=1.5,
                stock=0,
                status=ProductStatus.OUT,
                url="https://catfk.com/item/catfk-k12-out",
            )
        ]
        return records, len(records)


class FakeMerchantHintSource(FakeCompleteSource):
    instances: list["FakeMerchantHintSource"] = []

    def search(
        self,
        keywords: str,
        goods_type: str = "",
        current: int = 1,
        page_size: int = 200,
    ):
        self.keywords = keywords
        self.goods_type = goods_type
        self.requested_page = current
        self.requested_page_size = page_size
        records = [
            ProductRecord(
                external_id="hinted",
                name="GPT 商家链接结果",
                category=Category.CARD,
                merchant_name="中文候选店",
                merchant_link="https://pay.ldxp.cn/shop/中文候选",
                sale_price=10,
                stock=1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/hinted",
            )
        ]
        return records, len(records)


class FakeEmptyDiscovery:
    def discover(self, _keywords: str) -> RetailDiscoveryResult:
        return RetailDiscoveryResult()

    def close(self) -> None:
        return None


class FakeEmptyReferenceCatalog:
    def __init__(self) -> None:
        self.fetcher = ClosedFlag()

    def search(self, *_args, **_kwargs) -> list[ProductRecord]:
        return []


class FakePlusReferenceCatalog(FakeEmptyReferenceCatalog):
    def search(self, *_args, **_kwargs) -> list[ProductRecord]:
        return [
            ProductRecord(
                external_id="r:dl1215",
                name="GPT/Codex K12教师套餐",
                category=Category.CARD,
                merchant_name="ai小头",
                merchant_link="https://pay.ldxp.cn/shop/PLUS123",
                sale_price=1.23,
                stock=85,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/dl1215",
                raw={"_source": "reference_catalog"},
            )
        ]


class FakeStaleReferenceCatalog(FakeEmptyReferenceCatalog):
    def search(self, *_args, **_kwargs) -> list[ProductRecord]:
        return [
            ProductRecord(
                external_id="r:p7bd4t",
                name="Plus K12 商品",
                category=Category.CARD,
                merchant_name="bestcodex",
                merchant_link="https://pay.ldxp.cn/shop/5CF1CBYF",
                sale_price=5.89,
                stock=99,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/p7bd4t",
                raw={"_source": "reference_catalog"},
            )
        ]


class FakeOutOfStockReferenceShop:
    instances: list["FakeOutOfStockReferenceShop"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.fetcher = ClosedFlag()
        self.search_calls: list[tuple[str, str]] = []
        self.__class__.instances.append(self)

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
                external_id="r:p7bd4t",
                name="Plus K12 商品",
                category=Category.CARD,
                merchant_name="bestcodex",
                merchant_link="https://pay.ldxp.cn/shop/5CF1CBYF",
                sale_price=5.89,
                stock=0,
                status=ProductStatus.OUT,
                url="https://pay.ldxp.cn/item/p7bd4t",
            )
        ]


class FakeRetailDiscovery(FakeEmptyDiscovery):
    def discover(self, _keywords: str) -> RetailDiscoveryResult:
        return RetailDiscoveryResult(shop_tokens=("auto-shop",))


class FakeRetailSearch:
    instances: list["FakeRetailSearch"] = []

    def __init__(self) -> None:
        self.fetcher = ClosedFlag()
        self.search_calls: list[tuple[str, str]] = []
        self.__class__.instances.append(self)

    def item_record(self, _item_key: str):
        return None

    def search(
        self,
        token: str,
        keywords: str,
        _goods_type: str,
        *,
        shop_name: str | None = None,
        preferred_goods_type: str = "",
        max_pages: int = 4,
    ) -> list[ProductRecord]:
        del max_pages
        self.search_calls.append((token, keywords))
        return [
            ProductRecord(
                external_id="r:auto",
                name="GPT 自动发现零售商品",
                category=Category.CARD,
                merchant_name=shop_name or "自动零售店",
                sale_price=7,
                stock=-1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/auto",
            )
        ]


class FakeCurrentPagePriceRefresh(FakeRetailSearch):
    instances: list["FakeCurrentPagePriceRefresh"] = []

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
        *,
        shop_name: str | None = None,
        preferred_goods_type: str = "",
        max_pages: int = 4,
    ) -> list[ProductRecord]:
        del max_pages
        self.search_calls.append((token, keywords))
        if token != "Axship":
            return []
        return [
            ProductRecord(
                external_id="r:t0z53i",
                name="GPT Team K12 成品",
                category=Category.CARD,
                merchant_name=shop_name or "Axship星舰售票厅",
                sale_price=1.8,
                stock=-1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/t0z53i",
            )
        ]


class FakeManualRetailSearch(FakeRetailSearch):
    instances: list["FakeManualRetailSearch"] = []

    def search(
        self,
        token: str,
        keywords: str,
        _goods_type: str,
        *,
        shop_name: str | None = None,
        preferred_goods_type: str = "",
        max_pages: int = 4,
    ) -> list[ProductRecord]:
        del preferred_goods_type, max_pages
        self.search_calls.append((token, keywords))
        if token != "manual-target":
            return []
        return [
            ProductRecord(
                external_id="r:manual-k12",
                name="手动店铺 K12 有货商品",
                category=Category.CARD,
                merchant_name=shop_name or "手动店铺",
                sale_price=1.4,
                stock=171,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/manual-k12",
            )
        ]


class CompleteSearchTests(unittest.TestCase):

    def test_strict_discovery_reuses_saved_origin_category_in_one_request(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        category_calls: list[tuple[str, int]] = []

        class CachedCategorySource(ShopApi):
            def __init__(self) -> None:
                self.fetcher = ClosedFlag()

            def category_records(
                self,
                token: str,
                category_id: int,
                _keywords: str,
                _goods_type: str,
                **_options,
            ) -> list[ProductRecord]:
                category_calls.append((token, category_id))
                return [
                    ProductRecord(
                        external_id="r:cached-category-live",
                        name="ChatGPT Plus 当前成品号",
                        category=Category.CARD,
                        merchant_name="分类缓存店",
                        sale_price=9,
                        stock=8,
                        status=ProductStatus.NORMAL,
                        url="https://pay.ldxp.cn/item/cached-category-live",
                        raw={"category": {"id": category_id}},
                    )
                ]

            def category_records_for_item(self, *_args, **_kwargs):
                raise AssertionError("saved category should skip goodsInfo")

        with Session(engine) as session:
            shop = Shop(
                name="分类缓存店",
                kind=SourceKind.PUBLIC_SHOP,
                url="cached-category-shop",
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            candidate_url = "https://pay.ldxp.cn/item/cached-category-candidate"
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:cached-category-candidate",
                    name="ChatGPT Plus 候选",
                    category=Category.CARD,
                    merchant_name=shop.name,
                    sale_price=10,
                    stock=2,
                    status=ProductStatus.NORMAL,
                    url=candidate_url,
                    origin_category_id=130949,
                )
            )
            session.commit()

            with (
                patch("app.service.host_cooldown_remaining", return_value=0),
                patch(
                    "app.service._origin_shop_api",
                    side_effect=lambda **_kwargs: CachedCategorySource(),
                ),
            ):
                scopes = _discover_retail_matches(
                    session,
                    "ChatGPT Plus",
                    "card",
                    [],
                    allowed_item_urls={candidate_url},
                    max_candidates=1,
                    allow_web_discovery=False,
                    verify_budget_s=1,
                    stop_after_first_available=True,
                )

            stored = session.exec(
                select(Product).where(
                    Product.external_id == "r:cached-category-live"
                )
            ).one()

        self.assertEqual(category_calls, [("cached-category-shop", 130949)])
        self.assertEqual(len(scopes), 1)
        self.assertEqual(stored.origin_category_id, 130949)

    def test_strict_discovery_collects_multiple_shops_before_stopping(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        class MultiShopSource:
            def __init__(self) -> None:
                self.fetcher = ClosedFlag()

            def search(
                self,
                token: str,
                _keywords: str,
                _goods_type: str,
                **_options,
            ) -> list[ProductRecord]:
                return [
                    ProductRecord(
                        external_id=f"r:live-{token}",
                        name=f"ChatGPT Plus 成品号 {token}",
                        category=Category.CARD,
                        merchant_name=f"店铺 {token}",
                        sale_price=10,
                        stock=5,
                        status=ProductStatus.NORMAL,
                        url=f"https://pay.ldxp.cn/item/live-{token}",
                    )
                ]

        with Session(engine) as session:
            allowed_urls: set[str] = set()
            for index in range(3):
                token = f"multi{index}"
                shop = Shop(
                    name=f"店铺 {token}",
                    kind=SourceKind.PUBLIC_SHOP,
                    url=token,
                )
                session.add(shop)
                session.commit()
                session.refresh(shop)
                url = f"https://pay.ldxp.cn/item/candidate-{token}"
                allowed_urls.add(url)
                session.add(
                    Product(
                        shop_id=shop.id or 0,
                        external_id=f"r:candidate-{token}",
                        name=f"ChatGPT Plus 候选 {token}",
                        category=Category.CARD,
                        merchant_name=shop.name,
                        sale_price=10 + index,
                        stock=1,
                        status=ProductStatus.NORMAL,
                        url=url,
                    )
                )
            session.commit()
            stats: dict[str, int] = {}

            with (
                patch("app.service.host_cooldown_remaining", return_value=0),
                patch(
                    "app.service._origin_shop_api",
                    side_effect=lambda **_kwargs: MultiShopSource(),
                ),
            ):
                scopes = _discover_retail_matches(
                    session,
                    "ChatGPT Plus",
                    "card",
                    [],
                    allowed_item_urls=allowed_urls,
                    max_candidates=3,
                    allow_web_discovery=False,
                    verify_budget_s=2,
                    stop_after_first_available=True,
                    first_available_grace_s=0,
                    minimum_available_results=2,
                    minimum_available_shops=2,
                    stats=stats,
                )

        self.assertGreaterEqual(len(scopes), 2)
        self.assertGreaterEqual(stats["available_result_count"], 2)
        self.assertGreaterEqual(stats["available_shop_count"], 2)
    def test_goods_pool_directory_paginates_deduplicates_and_keeps_unicode(self) -> None:
        fetcher = DirectoryPagingFetcher()

        shops = SourceSquare(fetcher=fetcher).list_shops(page_size=2)

        self.assertEqual(fetcher.currents, [1, 2])
        self.assertEqual(
            [(shop.token, shop.name, shop.goods_count) for shop in shops],
            [
                ("中文店", "中文店铺", 3),
                ("Alpha", "Alpha 店", 7),
            ],
        )

    def test_source_square_reads_every_page_until_total(self) -> None:
        pages = {
            1: [raw_item(index) for index in range(1, 201)],
            2: [raw_item(index) for index in range(201, 401)],
            3: [raw_item(index) for index in range(401, 451)],
        }
        fetcher = PagingFetcher(pages, total=450)

        records = SourceSquare(fetcher=fetcher, page_size=200).search_all("GPT")

        self.assertEqual(len(records), 450)
        self.assertEqual(fetcher.currents, [1, 2, 3])
        self.assertEqual(records[-1].external_id, "450")

    def test_source_square_continues_when_server_caps_page_size(self) -> None:
        pages = {
            1: [raw_item(index) for index in range(1, 51)],
            2: [raw_item(index) for index in range(51, 101)],
            3: [raw_item(index) for index in range(101, 121)],
        }
        fetcher = PagingFetcher(pages, total=120)

        records = SourceSquare(fetcher=fetcher, page_size=200).search_all("GPT")

        self.assertEqual(len(records), 120)
        self.assertEqual(fetcher.currents, [1, 2, 3])

    def test_source_square_rejects_repeated_page(self) -> None:
        first_page = [raw_item(1), raw_item(2)]
        fetcher = PagingFetcher({1: first_page, 2: first_page}, total=3)

        with self.assertRaisesRegex(RuntimeError, "重复"):
            SourceSquare(fetcher=fetcher, page_size=2).search_all("GPT")

    def test_live_search_excludes_all_unverified_cached_results(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeCompleteSource.instances.clear()

        with Session(engine) as session:
            live_shop = Shop(name="货源广场 · 实时搜索", kind=SourceKind.SOURCE_SQUARE)
            retail_shop = Shop(name="已知零售店", kind=SourceKind.PUBLIC_SHOP, url="known")
            session.add(live_shop)
            session.add(retail_shop)
            session.commit()
            session.refresh(live_shop)
            session.refresh(retail_shop)
            session.add(
                Product(
                    shop_id=live_shop.id,
                    external_id="old",
                    name="GPT 旧缓存",
                    stock=1,
                    status=ProductStatus.NORMAL,
                )
            )
            session.add(
                Product(
                    shop_id=retail_shop.id,
                    external_id="r:known",
                    name="GPT 已知零售商品",
                    merchant_name="已知零售店",
                    stock=-1,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()

            with (
                patch("app.service.settings.merchant_token", "ldxp-token"),
                patch("app.service.SourceSquare", FakeCompleteSource),
                patch("app.service.RetailDiscovery", FakeEmptyDiscovery),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(session, "GPT", page_size=0, in_stock_only=False)

            self.assertEqual(total, 1)
            self.assertEqual([product.name for product in products], ["GPT 本次结果"])
            self.assertTrue(FakeCompleteSource.instances[0].fetcher.closed)

    def test_live_search_combines_ldxp_and_catfk_sources(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", "ldxp-token"),
                patch("app.service.settings.catfk_merchant_token", "catfk-token"),
                patch("app.service.SourceSquare", FakeMultiPlatformSource),
                patch("app.service.RetailDiscovery", FakeEmptyDiscovery),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(
                    session,
                    "GPT",
                    page_size=0,
                    in_stock_only=True,
                )

            self.assertEqual(total, 2)
            self.assertEqual(
                {product.name for product in products},
                {"GPT 链动结果", "GPT 云猫结果"},
            )
            self.assertEqual(
                {
                    shop.name
                    for shop in session.exec(
                        select(Shop).where(Shop.kind == SourceKind.SOURCE_SQUARE)
                    ).all()
                },
                {"货源广场 · 实时搜索", "云猫寄售 · 实时搜索"},
            )

    def test_catfk_filter_skips_ldxp_requests_and_cached_results(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeMultiPlatformSource.instances.clear()

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", "ldxp-token"),
                patch("app.service.settings.catfk_merchant_token", "catfk-token"),
                patch("app.service.SourceSquare", FakeMultiPlatformSource),
                patch(
                    "app.service.RetailDiscovery",
                    side_effect=AssertionError("catfk-only search touched ldxp retail discovery"),
                ),
            ):
                products, total = live_search(
                    session,
                    "GPT",
                    page_size=0,
                    in_stock_only=True,
                    platform="catfk",
                )

            cached, cached_total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
                platform="catfk",
            )

        self.assertEqual(total, 1)
        self.assertEqual(cached_total, 1)
        self.assertEqual([product.name for product in products], ["GPT 云猫结果"])
        self.assertEqual([product.name for product in cached], ["GPT 云猫结果"])
        self.assertEqual(
            [source.base_url for source in FakeMultiPlatformSource.instances],
            ["https://catfk.com"],
        )

    def test_catfk_normalizes_lowercase_and_explains_empty_in_stock_result(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeCaseSensitiveCatfkSource.instances.clear()
        warnings: list[str] = []

        with Session(engine) as session:
            with (
                patch("app.service.settings.catfk_merchant_token", "catfk-token"),
                patch("app.service.SourceSquare", FakeCaseSensitiveCatfkSource),
            ):
                products, total = live_search(
                    session,
                    "k12",
                    page_size=0,
                    in_stock_only=True,
                    platform="catfk",
                    warnings=warnings,
                )

        self.assertEqual(products, [])
        self.assertEqual(total, 0)
        self.assertEqual(
            FakeCaseSensitiveCatfkSource.instances[0].calls,
            [("K12", "card")],
        )
        self.assertIn("找到了 1 条相关商品", warnings[0])
        self.assertIn("均缺货或未上架", warnings[0])

    def test_live_search_automatically_discovers_retail_shops(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeCompleteSource.instances.clear()
        FakeRetailSearch.instances.clear()

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", "ldxp-token"),
                patch("app.service.SourceSquare", FakeCompleteSource),
                patch("app.service.RetailDiscovery", FakeRetailDiscovery),
                patch("app.service.ShopApi", FakeRetailSearch),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(session, "GPT", page_size=0, in_stock_only=False)

            self.assertEqual(total, 2)
            self.assertEqual(
                {product.name for product in products},
                {"GPT 本次结果", "GPT 自动发现零售商品"},
            )
            retail_shop = session.exec(
                select(Shop).where(Shop.kind == SourceKind.PUBLIC_SHOP)
            ).one()
            self.assertEqual(retail_shop.url, "auto-shop")
            self.assertEqual(retail_shop.note, "自动发现的公开零售店")
            self.assertEqual(
                FakeRetailSearch.instances[0].search_calls,
                [("auto-shop", "GPT")],
            )
            self.assertTrue(FakeRetailSearch.instances[0].fetcher.closed)

    def test_live_search_consumes_source_merchant_link_as_shop_candidate(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeMerchantHintSource.instances.clear()
        FakeRetailSearch.instances.clear()

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", "ldxp-token"),
                patch("app.service.SourceSquare", FakeMerchantHintSource),
                patch(
                    "app.service.RetailDiscovery",
                    side_effect=AssertionError(
                        "known merchant candidate should skip slow web discovery"
                    ),
                ),
                patch("app.service.ShopApi", FakeRetailSearch),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(session, "GPT", page_size=0, in_stock_only=False)

            self.assertEqual(total, 2)
            self.assertEqual(
                FakeRetailSearch.instances[0].search_calls,
                [("中文候选", "GPT")],
            )
            retail_shop = session.exec(
                select(Shop).where(Shop.kind == SourceKind.PUBLIC_SHOP)
            ).one()
            self.assertEqual(retail_shop.url, "中文候选")
            self.assertIsNone(retail_shop.last_synced_at)

    def test_indexed_retail_match_is_not_starved_by_source_links(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeRetailSearch.instances.clear()

        source_records = [
            ProductRecord(
                external_id=f"source-{index}",
                name=f"K12 货源 {index}",
                category=Category.CARD,
                merchant_name=f"货源店 {index}",
                merchant_link=f"https://pay.ldxp.cn/shop/source{index:02d}",
                sale_price=10 + index,
                stock=1,
                status=ProductStatus.NORMAL,
                url=f"https://pay.ldxp.cn/item/source{index:02d}",
            )
            for index in range(20)
        ]

        with Session(engine) as session:
            retail_shop = Shop(
                name="K12 独有零售店",
                kind=SourceKind.PUBLIC_SHOP,
                url="localtarget",
            )
            session.add(retail_shop)
            session.commit()
            session.refresh(retail_shop)
            session.add(
                Product(
                    shop_id=retail_shop.id or 0,
                    external_id="r:localtarget",
                    name="K12 零售独有商品",
                    category=Category.CARD,
                    merchant_name=retail_shop.name,
                    sale_price=1,
                    stock=10,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/localtarget",
                )
            )
            session.commit()

            with (
                patch("app.service.ShopApi", FakeRetailSearch),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                _discover_retail_matches(session, "K12", "", source_records)

        searched_tokens = {
            token
            for instance in FakeRetailSearch.instances
            for token, _keywords in instance.search_calls
        }
        self.assertIn("localtarget", searched_tokens)

    def test_live_search_refreshes_shops_from_current_cached_page_first(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeCompleteSource.instances.clear()
        FakeCurrentPagePriceRefresh.instances.clear()

        with Session(engine) as session:
            retail_shop = Shop(
                name="Axship星舰售票厅",
                kind=SourceKind.PUBLIC_SHOP,
                url="Axship",
            )
            session.add(retail_shop)
            session.commit()
            session.refresh(retail_shop)
            session.add(
                Product(
                    shop_id=retail_shop.id or 0,
                    external_id="r:t0z53i",
                    name="GPT Team K12 成品",
                    category=Category.CARD,
                    merchant_name=retail_shop.name,
                    sale_price=1.6,
                    stock=42,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/t0z53i",
                )
            )
            session.commit()

            with (
                patch("app.service.SourceSquare", FakeCompleteSource),
                patch("app.service.RetailDiscovery", FakeEmptyDiscovery),
                patch("app.service.ShopApi", FakeCurrentPagePriceRefresh),
                patch("app.service.host_cooldown_remaining", return_value=0),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(
                    session,
                    "GPT Team K12",
                    page_size=0,
                    in_stock_only=False,
                )

        self.assertEqual(total, 1)
        refreshed = next(
            product
            for product in products
            if product.url == "https://pay.ldxp.cn/item/t0z53i"
        )
        self.assertEqual(
            [
                call
                for instance in FakeCurrentPagePriceRefresh.instances
                for call in instance.search_calls
            ],
            [("Axship", "GPT Team K12")],
        )
        self.assertEqual(refreshed.sale_price, 1.8)

    def test_stale_matching_shop_is_not_starved_by_low_price_candidate_cap(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeRetailSearch.instances.clear()
        now = datetime.now()

        with Session(engine) as session:
            for index in range(12):
                shop = Shop(
                    name=f"低价店 {index}",
                    kind=SourceKind.PUBLIC_SHOP,
                    url=f"cheap-{index}",
                )
                session.add(shop)
                session.commit()
                session.refresh(shop)
                session.add(
                    Product(
                        shop_id=shop.id or 0,
                        external_id=f"r:cheap-{index}",
                        name="GPT K12 商品",
                        category=Category.CARD,
                        merchant_name=shop.name,
                        sale_price=1 + index / 10,
                        stock=1,
                        status=ProductStatus.NORMAL,
                        url=f"https://pay.ldxp.cn/item/cheap-{index}",
                        last_seen_at=now,
                    )
                )

            stale_shop = Shop(
                name="旧价目标店",
                kind=SourceKind.PUBLIC_SHOP,
                url="stale-target",
            )
            session.add(stale_shop)
            session.commit()
            session.refresh(stale_shop)
            session.add(
                Product(
                    shop_id=stale_shop.id or 0,
                    external_id="r:stale-target",
                    name="GPT K12 商品",
                    category=Category.CARD,
                    merchant_name=stale_shop.name,
                    sale_price=99,
                    stock=1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/stale-target",
                    last_seen_at=now - timedelta(days=10),
                )
            )
            session.commit()

            with (
                patch("app.service.ShopApi", FakeRetailSearch),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                _discover_retail_matches(session, "K12", "", [])

        searched_tokens = {
            token
            for instance in FakeRetailSearch.instances
            for token, _keywords in instance.search_calls
        }
        self.assertIn("stale-target", searched_tokens)

    def test_expired_ldxp_source_falls_back_to_public_retail_with_warning(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeRetailSearch.instances.clear()

        with Session(engine) as session:
            shop = Shop(
                name="公开零售店",
                kind=SourceKind.PUBLIC_SHOP,
                url="public-fallback",
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:cached",
                    name="GPT K12 商品",
                    category=Category.CARD,
                    merchant_name=shop.name,
                    sale_price=8,
                    stock=1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/cached",
                )
            )
            session.commit()
            warnings: list[str] = []

            with (
                patch("app.service.settings.merchant_token", "expired-token"),
                patch("app.service.SourceSquare", FakeFailingSource),
                patch("app.service.ShopApi", FakeRetailSearch),
                patch("app.service.host_cooldown_remaining", return_value=0),
                patch("app.service.ReferenceCatalog", FakeEmptyReferenceCatalog),
            ):
                products, total = live_search(
                    session,
                    "GPT",
                    page_size=0,
                    in_stock_only=False,
                    platform="ldxp",
                    warnings=warnings,
                )

        self.assertEqual(total, 1)
        self.assertEqual([product.name for product in products], ["GPT 自动发现零售商品"])
        self.assertTrue(any("链动小铺官方搜索失败" in warning for warning in warnings))

    def test_reference_positive_stock_is_only_an_unverified_candidate(self) -> None:
        record = ReferenceCatalog._map(
            {
                "externalId": "p7bd4t",
                "name": "Plus K12 商品",
                "storeName": "bestcodex",
                "storeUrl": "https://pay.ldxp.cn/shop/5CF1CBYF",
                "url": "https://pay.ldxp.cn/item/p7bd4t",
                "productType": "card",
                "currentPriceCents": 589,
                "active": True,
                "inStock": True,
                "stockCount": 99,
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.stock, -1)
        self.assertEqual(record.status, ProductStatus.NORMAL)

    def test_unverified_reference_stock_is_hidden_during_host_cooldown(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", ""),
                patch("app.service.settings.catfk_merchant_token", ""),
                patch("app.service.ReferenceCatalog", FakePlusReferenceCatalog),
                patch("app.service.host_cooldown_remaining", return_value=60),
            ):
                products, total = live_search(
                    session,
                    "k12",
                    page_size=0,
                    in_stock_only=True,
                    platform="ldxp",
                )

        self.assertEqual(total, 0)
        self.assertEqual(products, [])

    def test_direct_shop_stock_overrides_stale_positive_reference_stock(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeOutOfStockReferenceShop.instances.clear()

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", ""),
                patch("app.service.settings.catfk_merchant_token", ""),
                patch("app.service.ReferenceCatalog", FakeStaleReferenceCatalog),
                patch("app.service.ShopApi", FakeOutOfStockReferenceShop),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                products, total = live_search(
                    session,
                    "plus",
                    page_size=0,
                    in_stock_only=True,
                    platform="ldxp",
                )
            stored = session.exec(
                select(Product).where(Product.external_id == "r:p7bd4t")
            ).one()

        self.assertEqual(total, 0)
        self.assertEqual(products, [])
        self.assertEqual(stored.stock, 0)
        self.assertEqual(stored.status, ProductStatus.OUT)
        self.assertIn(
            ("5CF1CBYF", "plus"),
            [
                call
                for instance in FakeOutOfStockReferenceShop.instances
                for call in instance.search_calls
            ],
        )

    def test_manual_shop_is_searched_even_when_reference_has_results(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        FakeManualRetailSearch.instances.clear()

        with Session(engine) as session:
            shop = Shop(
                name="手动店铺",
                kind=SourceKind.PUBLIC_SHOP,
                url="manual-target",
                note="公开零售店",
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:manual-k12",
                    name="手动店铺 K12 旧快照",
                    category=Category.CARD,
                    merchant_name=shop.name,
                    sale_price=1.5,
                    stock=1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/manual-k12",
                )
            )
            session.commit()

            with (
                patch("app.service.settings.merchant_token", ""),
                patch("app.service.settings.catfk_merchant_token", ""),
                patch("app.service.ReferenceCatalog", FakePlusReferenceCatalog),
                patch("app.service.ShopApi", FakeManualRetailSearch),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                products, total = live_search(
                    session,
                    "k12",
                    page_size=0,
                    in_stock_only=True,
                    platform="ldxp",
                )

        self.assertEqual(total, 1)
        manual = next(
            product
            for product in products
            if product.url == "https://pay.ldxp.cn/item/manual-k12"
        )
        self.assertEqual(manual.stock, 171)
        self.assertEqual(manual.sale_price, 1.4)
        self.assertIn(
            ("manual-target", "k12"),
            [
                call
                for instance in FakeManualRetailSearch.instances
                for call in instance.search_calls
            ],
        )

    def test_strict_plus_uses_current_pickai_title_for_origin_lookup(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        searched_terms: list[str] = []

        class StrictPlusShop:
            def __init__(self) -> None:
                self.fetcher = ClosedFlag()

            def search(
                self,
                _token: str,
                keywords: str,
                _goods_type: str,
                **_options,
            ) -> list[ProductRecord]:
                searched_terms.append(keywords)
                return [
                    ProductRecord(
                        external_id="r:new-plus",
                        name="Plus 成品号，未接码",
                        category=Category.CARD,
                        merchant_name="实时 Plus 店",
                        sale_price=9,
                        stock=7,
                        status=ProductStatus.NORMAL,
                        url="https://pay.ldxp.cn/item/new-plus",
                    )
                ]

        class StrictPlusCatalog:
            request_count = 1

            def __init__(self, *_args, **_kwargs) -> None:
                return None

            def search_current_strict(self, _keywords: str, **_options):
                return ([
                    {
                        "id": 999,
                        "raw_name": "Plus 旧候选",
                        "shop_name": "实时 Plus 店",
                        "price": "¥10",
                        "stock": "库存 1",
                        "item_url": "https://pay.ldxp.cn/item/old-plus",
                        "product_type_ids": [3],
                        "product_type_names": ["ChatGPT Plus"],
                        "catalog_categories": ["ChatGPT"],
                    }
                ], 1)

            def close(self) -> None:
                return None

        with Session(engine) as session:
            pickai = Shop(
                name="PickAI · 公开报价索引",
                kind=SourceKind.SOURCE_SQUARE,
                url="https://pickai.cc",
                last_synced_at=datetime.now(),
            )
            retail = Shop(
                name="实时 Plus 店",
                kind=SourceKind.PUBLIC_SHOP,
                url="strict-plus-shop",
            )
            session.add(pickai)
            session.add(retail)
            session.commit()
            session.refresh(pickai)
            session.refresh(retail)
            session.add(
                Product(
                    shop_id=pickai.id or 0,
                    external_id="p:old-plus",
                    name="ChatGPT Plus · 旧候选",
                    category=Category.CARD,
                    merchant_name="实时 Plus 店",
                    sale_price=10,
                    stock=1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/old-plus",
                )
            )
            session.add(
                Product(
                    shop_id=retail.id or 0,
                    external_id="r:known-other-plus",
                    name="Plus 已知旧链接",
                    category=Category.CARD,
                    merchant_name=retail.name,
                    sale_price=10,
                    stock=1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/known-other-plus",
                )
            )
            session.commit()
            warnings: list[str] = []

            with (
                patch("app.service.settings.merchant_token", ""),
                patch("app.service.settings.catfk_merchant_token", ""),
                patch("app.service.host_cooldown_remaining", return_value=0),
                patch("app.service.PickAICatalog", StrictPlusCatalog),
                patch(
                    "app.service._origin_shop_api",
                    side_effect=lambda **_kwargs: StrictPlusShop(),
                ),
            ):
                products, total = live_search(
                    session,
                    "ChatGPT Plus",
                    page_size=0,
                    in_stock_only=True,
                    platform="ldxp",
                    warnings=warnings,
                    refresh_pickai=True,
                )

        self.assertEqual(searched_terms, ["Plus 旧候选", "plus"])
        self.assertEqual(total, 1)
        self.assertEqual(products[0].url, "https://pay.ldxp.cn/item/new-plus")
        self.assertEqual(products[0].sale_price, 9)
        self.assertEqual(products[0].stock, 7)
        self.assertEqual(warnings, [])

    def test_public_only_refresh_never_uses_merchant_sources(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            with (
                patch("app.service.settings.merchant_token", "ldxp-secret"),
                patch("app.service.settings.catfk_merchant_token", "catfk-secret"),
                patch(
                    "app.service.SourceSquare",
                    side_effect=AssertionError("automatic refresh used merchant credentials"),
                ),
                patch("app.service.ReferenceCatalog", FakePlusReferenceCatalog),
                patch("app.service.host_cooldown_remaining", return_value=60),
            ):
                products, total = live_search(
                    session,
                    "k12",
                    page_size=0,
                    in_stock_only=True,
                    platform="all",
                    public_only=True,
                )

        self.assertEqual(total, 0)
        self.assertEqual(products, [])


if __name__ == "__main__":
    unittest.main()
