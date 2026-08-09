from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.crawler.base import ProductRecord
from app.crawler.pickai_catalog import (
    PickAICatalog,
    export_snapshot,
    is_chatgpt_plus_product_name,
    is_email_product_name,
    is_k12_product_name,
    is_openai_sms_product_name,
    strict_realtime_scope_for_query,
)
from app.crawler.session import JsonChallengeError
from app.models import Category, Product, ProductStatus, Shop, SourceKind
from app.pickai_index import PickAIIndexCoordinator
from app.service import _db_search, _verify_pickai_inventory, ingest


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def get_json(self, path: str, params: dict | None = None):
        self.calls.append((path, params))
        if path.endswith("/categories"):
            return [{"id": 1, "name": "ChatGPT"}]
        if path.endswith("/product-types"):
            return [
                {"id": 1, "name": "ChatGPT 普号", "category": "ChatGPT"},
                {"id": 3, "name": "ChatGPT Plus", "category": "ChatGPT"},
            ]
        if path.endswith("/relay-providers"):
            return {"items": [{"id": "relay"}], "suppliers": []}
        if path.endswith("/quotes"):
            type_id = int((params or {})["product_type_id"])
            page = int((params or {})["page"])
            if type_id == 1 and page == 1:
                return {
                    "items": [
                        {
                            "id": 10,
                            "shop_name": "甲店",
                            "raw_name": "GPT Free",
                            "price": "¥0.2",
                            "stock": "库存 12",
                            "item_url": "https://pay.ldxp.cn/item/free1",
                            "updated_at": "2026-08-05T01:00:00",
                        }
                    ],
                    "total": 2,
                    "has_more": True,
                }
            if type_id == 1 and page == 2:
                return {
                    "items": [
                        {
                            "id": 11,
                            "shop_name": "乙店",
                            "raw_name": "GPT Plus",
                            "price": "¥12",
                            "stock": "库存 3",
                            "item_url": "https://pay.ldxp.cn/item/plus1",
                            "updated_at": "2026-08-05T01:00:00",
                        }
                    ],
                    "total": 2,
                    "has_more": False,
                }
            return {
                # 同一商品跨标准类型时只保留一条，并合并类型信息。
                "items": [
                    {
                        "id": 11,
                        "shop_name": "乙店",
                        "raw_name": "GPT Plus 新标题",
                        "price": "¥11.8",
                        "stock": "库存 4",
                        "item_url": "https://pay.ldxp.cn/item/plus1",
                        "updated_at": "2026-08-05T02:00:00",
                    }
                ],
                "total": 1,
                "has_more": False,
            }
        raise AssertionError(path)


def memory_engine():
    db_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db_engine)
    return db_engine


class PickAICatalogTests(unittest.TestCase):
    def test_curated_scope_mapping_keeps_four_shortcuts_separate(self) -> None:
        self.assertEqual(strict_realtime_scope_for_query("ChatGPT Plus"), "chatgpt")
        self.assertEqual(strict_realtime_scope_for_query("K12"), "k12")
        self.assertEqual(strict_realtime_scope_for_query("邮箱"), "email")
        self.assertEqual(strict_realtime_scope_for_query("Gmail"), "email")
        self.assertEqual(strict_realtime_scope_for_query("OpenAI 接码"), "openai_sms")
        self.assertTrue(is_k12_product_name("GPT Team K12 成品"))
        self.assertFalse(is_k12_product_name("Super Grok12 个月年卡"))
        self.assertFalse(is_k12_product_name("Claude K12 教师版"))

    def test_plus_and_sms_name_filters_keep_shortcuts_separate(self) -> None:
        self.assertTrue(is_chatgpt_plus_product_name("Plus 成品号，未接码"))
        self.assertTrue(is_chatgpt_plus_product_name("ChatGPT Plus 官方充值月卡"))
        self.assertFalse(is_chatgpt_plus_product_name("Plus/Codex 短效接码"))
        self.assertFalse(is_chatgpt_plus_product_name("普通微软邮箱，不含 Plus"))
        self.assertFalse(is_chatgpt_plus_product_name("普通邮箱，开 Plus 专用"))
        self.assertFalse(
            is_chatgpt_plus_product_name("Plus gpt号接码并导入 sub2api 教程")
        )
        self.assertFalse(
            is_chatgpt_plus_product_name("支付提链 10 次，开通 Plus 专用")
        )
        self.assertFalse(is_chatgpt_plus_product_name("Plus Pro 邀请额度增加"))
        self.assertTrue(is_openai_sms_product_name("OpenAI GPT Plus 单次接码"))
        self.assertFalse(
            is_openai_sms_product_name("【教程】未接码 Plus GPT 号接码并导入教程")
        )
        self.assertFalse(is_openai_sms_product_name("GPT Free · Codex 未接码账号"))
        self.assertFalse(is_openai_sms_product_name("Plus 已接码成品号"))
        self.assertFalse(is_openai_sms_product_name("GPT Plus 成品，没接码，可网页使用"))
        self.assertFalse(is_openai_sms_product_name("Claude 单次接码"))

    def test_email_name_filter_rejects_ai_products_that_only_mention_email(self) -> None:
        self.assertTrue(is_email_product_name("Outlook / Hotmail 邮箱账号"))
        self.assertTrue(is_email_product_name("Gmail 老号，支持 IMAP"))
        self.assertTrue(is_email_product_name("iCloud 邮箱独享"))
        self.assertFalse(is_email_product_name("ChatGPT Plus 发邮箱 URL"))
        self.assertFalse(is_email_product_name("OpenAI 邮箱验证码接码"))
        self.assertFalse(is_email_product_name("邮箱注册教程"))

    def test_chatgpt_plus_search_uses_standard_type_and_excludes_code_service(self) -> None:
        class ScopeFetcher:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict | None]] = []

            def get_json(self, path: str, params: dict | None = None):
                self.calls.append((path, params))
                if path.endswith("/product-types"):
                    return [
                        {"id": 3, "name": "ChatGPT Plus", "category": "ChatGPT"},
                        {
                            "id": 29,
                            "name": "OpenAI/ChatGPT接码",
                            "category": "接码",
                        },
                    ]
                if path.endswith("/quotes"):
                    self.assert_not_code_service(params)
                    return {
                        "items": [
                            {
                                "id": 300,
                                "shop_name": "Plus 店铺",
                                "raw_name": "ChatGPT Plus 成品号",
                                "price": "¥25",
                                "stock": "库存 6",
                                "item_url": "https://pay.ldxp.cn/item/plus300",
                            }
                        ],
                        "total": 1,
                        "has_more": False,
                    }
                raise AssertionError(path)

            @staticmethod
            def assert_not_code_service(params: dict | None) -> None:
                if int((params or {})["product_type_id"]) == 29:
                    raise AssertionError("接码分类不应被请求")

        fetcher = ScopeFetcher()
        catalog = PickAICatalog(fetcher, retries=1)
        quotes, total = catalog.search_chatgpt("plus")

        self.assertEqual(total, 1)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["product_type_ids"], [3])
        self.assertEqual(quotes[0]["product_type_names"], ["ChatGPT Plus"])
        self.assertEqual(quotes[0]["catalog_categories"], ["ChatGPT"])
        quote_calls = [params for path, params in fetcher.calls if path.endswith("/quotes")]
        self.assertEqual([params["product_type_id"] for params in quote_calls], [3])

    def test_current_strict_search_fetches_quotes_in_one_request(self) -> None:
        class FastFetcher:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict | None]] = []

            def get_json(self, path: str, params: dict | None = None):
                self.calls.append((path, params))
                if not path.endswith("/quotes"):
                    raise AssertionError("热路径不应额外请求 product-types")
                return {
                    "items": [
                        {
                            "id": 301,
                            "shop_name": "当前低价店",
                            "raw_name": "Plus 当前低价",
                            "price": "¥9",
                            "stock": "库存 76",
                            "item_url": "https://pay.ldxp.cn/item/current301",
                        }
                    ],
                    "total": 1188,
                    "has_more": True,
                }

        fetcher = FastFetcher()
        quotes, total = PickAICatalog(fetcher, retries=1).search_current_strict(
            "ChatGPT Plus",
            max_pages=1,
        )

        self.assertEqual(total, 1188)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["product_type_ids"], [3])
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(fetcher.calls[0][1]["product_type_id"], 3)

    def test_openai_sms_search_only_requests_exact_standard_type(self) -> None:
        class SmsFetcher:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict | None]] = []

            def get_json(self, path: str, params: dict | None = None):
                self.calls.append((path, params))
                if path.endswith("/product-types"):
                    return [
                        {"id": 26, "name": "通用接码", "category": "接码"},
                        {
                            "id": 29,
                            "name": "OpenAI/ChatGPT接码",
                            "category": "接码",
                        },
                        {"id": 30, "name": "真人/KYC验证", "category": "接码"},
                    ]
                if path.endswith("/quotes"):
                    if int((params or {})["product_type_id"]) != 29:
                        raise AssertionError("只允许请求 OpenAI/ChatGPT 接码类型 29")
                    return {
                        "items": [
                            {
                                "id": 290,
                                "shop_name": "OpenAI 接码店",
                                "raw_name": "ChatGPT 验证码",
                                "price": "¥0.2",
                                "stock": "库存 8",
                                "item_url": "https://pay.ldxp.cn/item/sms290",
                            }
                        ],
                        "total": 1,
                        "has_more": False,
                    }
                raise AssertionError(path)

        fetcher = SmsFetcher()
        quotes, total = PickAICatalog(fetcher, retries=1).search_openai_sms()

        self.assertEqual(total, 1)
        self.assertEqual([item["product_type_ids"] for item in quotes], [[29]])
        self.assertEqual(
            [item["product_type_names"] for item in quotes],
            [["OpenAI/ChatGPT接码"]],
        )

    def test_email_strict_search_requests_all_five_email_types_without_catalog_call(self) -> None:
        class EmailFetcher:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict | None]] = []

            def get_json(self, path: str, params: dict | None = None):
                self.calls.append((path, params))
                if not path.endswith("/quotes"):
                    raise AssertionError("邮箱热路径不应额外请求 product-types")
                type_id = int((params or {})["product_type_id"])
                return {
                    "items": [
                        {
                            "id": 2100 + type_id,
                            "shop_name": "邮箱店",
                            "raw_name": f"邮箱商品 {type_id}",
                            "price": f"¥{type_id / 10}",
                            "stock": "库存 8",
                            "item_url": f"https://pay.ldxp.cn/item/mail{type_id}",
                        }
                    ],
                    "total": 1,
                    "has_more": False,
                }

        fetcher = EmailFetcher()
        quotes, total = PickAICatalog(fetcher, retries=1).search_current_strict("邮箱")

        self.assertEqual(total, 5)
        self.assertEqual(len(quotes), 5)
        quote_calls = [params for path, params in fetcher.calls if path.endswith("/quotes")]
        self.assertEqual([params["product_type_id"] for params in quote_calls], [21, 22, 23, 24, 25])
        self.assertEqual(
            [item["product_type_names"][0] for item in quotes],
            [
                "Outlook / Hotmail 邮箱",
                "iCloud 邮箱",
                "Gmail / Google 邮箱",
                "教育邮箱",
                "其他邮箱",
            ],
        )

    def test_origin_challenge_never_marks_aggregate_stock_verified(self) -> None:
        class FakeOriginShop:
            def __init__(self) -> None:
                self.fetcher = type("Fetcher", (), {"close": lambda _self: None})()

            def item_record(self, _key: str):
                return "origin", ProductRecord(
                    external_id="r:plus1",
                    name="GPT Plus 原店商品",
                    category=Category.CARD,
                    merchant_name="原店",
                    sale_price=13,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/plus1",
                )

            def search(self, *_args, **_kwargs):
                raise JsonChallengeError("滑块")

        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(
                name="PickAI · 公开报价索引",
                kind=SourceKind.SOURCE_SQUARE,
                active=True,
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = Product(
                shop_id=shop.id,
                external_id="p:plus1",
                name="GPT Plus 聚合商品",
                category=Category.CARD,
                merchant_name="聚合目录",
                sale_price=12,
                stock=15,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/plus1",
            )
            session.add(product)
            session.commit()

            with (
                patch("app.service.ShopApi", FakeOriginShop),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                result = _verify_pickai_inventory(session, "plus", "", limit=1)
            session.refresh(product)

        self.assertEqual(result["challenge"], 1)
        self.assertEqual(product.stock, -1)
        self.assertIsNone(product.inventory_verified_at)

    def test_origin_inventory_overrides_delayed_pickai_stock(self) -> None:
        class FakeOriginShop:
            def __init__(self) -> None:
                self.fetcher = type("Fetcher", (), {"close": lambda _self: None})()

            def item_record(self, _key: str):
                return "origin", ProductRecord(
                    external_id="r:plus1",
                    name="GPT Plus 独享账号",
                    category=Category.CARD,
                    merchant_name="原店",
                    sale_price=13,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/plus1",
                )

            def search(self, *_args, **_kwargs):
                return [
                    ProductRecord(
                        external_id="r:plus1",
                        name="GPT Plus 独享账号",
                        category=Category.CARD,
                        merchant_name="原店",
                        sale_price=13,
                        stock=0,
                        status=ProductStatus.OUT,
                        url="https://pay.ldxp.cn/item/plus1",
                    )
                ]

        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(
                name="PickAI · 公开报价索引",
                kind=SourceKind.SOURCE_SQUARE,
                active=True,
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = Product(
                shop_id=shop.id,
                external_id="p:plus1",
                name="ChatGPT Plus · GPT Plus 独享账号",
                category=Category.CARD,
                merchant_name="聚合目录店名",
                sale_price=12,
                stock=15,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/plus1",
            )
            session.add(product)
            session.commit()

            with (
                patch("app.service.ShopApi", FakeOriginShop),
                patch("app.service.host_cooldown_remaining", return_value=0),
            ):
                result = _verify_pickai_inventory(session, "plus", "", limit=1)
            session.refresh(product)

        self.assertEqual(result["verified"], 1)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(product.sale_price, 13)
        self.assertEqual(product.stock, 0)
        self.assertEqual(product.status, ProductStatus.OUT)
        self.assertIsNotNone(product.inventory_verified_at)

    def test_pickai_refresh_does_not_overwrite_recent_origin_verification(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="PickAI · 公开报价索引", kind=SourceKind.SOURCE_SQUARE)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            ingest(
                session,
                shop,
                [
                    ProductRecord(
                        external_id="p:plus1",
                        name="ChatGPT Plus · 商品",
                        category=Category.CARD,
                        sale_price=13,
                        stock=0,
                        status=ProductStatus.OUT,
                        url="https://pay.ldxp.cn/item/plus1",
                    )
                ],
                inventory_verified=True,
            )
            ingest(
                session,
                shop,
                [
                    ProductRecord(
                        external_id="p:plus1",
                        name="ChatGPT Plus · 商品",
                        category=Category.CARD,
                        sale_price=12,
                        stock=15,
                        status=ProductStatus.NORMAL,
                        url="https://pay.ldxp.cn/item/plus1",
                    )
                ],
                inventory_verified=False,
            )
            product = session.exec(
                select(Product).where(Product.external_id == "p:plus1")
            ).one()

        self.assertEqual(product.sale_price, 13)
        self.assertEqual(product.stock, 0)
        self.assertEqual(product.status, ProductStatus.OUT)
    def test_full_snapshot_pages_deduplicates_and_maps_records(self) -> None:
        fake = FakeFetcher()
        catalog = PickAICatalog(fake, retries=1)
        snapshot = catalog.full_snapshot(workers=6)

        self.assertEqual(len(snapshot.categories), 1)
        self.assertEqual(len(snapshot.product_types), 2)
        self.assertEqual(snapshot.declared_quotes, 3)
        self.assertEqual(len(snapshot.quotes), 2)
        self.assertEqual(snapshot.duplicate_quotes, 1)
        plus = next(item for item in snapshot.quotes if item["id"] == 11)
        self.assertEqual(plus["price"], "¥11.8")
        self.assertEqual(plus["product_type_ids"], [1, 3])

        records = snapshot.product_records()
        plus_record = next(record for record in records if record.external_id == "p:plus1")
        self.assertEqual(plus_record.stock, 4)
        self.assertEqual(plus_record.sale_price, 11.8)
        self.assertEqual(plus_record.status, ProductStatus.NORMAL)

    def test_exports_json_and_csv(self) -> None:
        snapshot = PickAICatalog(FakeFetcher(), retries=1).full_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "snapshot.json"
            csv_path = Path(directory) / "quotes.csv"
            export_snapshot(snapshot, json_path, csv_path)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["quotes"], 2)
            self.assertIn("GPT Plus 新标题", csv_path.read_text(encoding="utf-8-sig"))

    def test_bundled_snapshot_path_is_found_in_onefile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            seed_dir = Path(directory) / "seed"
            seed_dir.mkdir()
            expected = seed_dir / "pickai_snapshot.json"
            expected.write_text("{}", encoding="utf-8")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", directory, create=True),
            ):
                self.assertEqual(
                    PickAIIndexCoordinator._bundled_snapshot_path(),
                    expected,
                )

    def test_snapshot_source_scope_is_searchable(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(
                name="PickAI · 公开报价索引",
                kind=SourceKind.SOURCE_SQUARE,
                last_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)
            product = Product(
                shop_id=shop.id,
                external_id="p:plus1",
                name="ChatGPT Plus · GPT Plus 独享账号",
                merchant_name="乙店",
                sale_price=11.8,
                stock=4,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/plus1",
            )
            recharge = Product(
                shop_id=shop.id,
                external_id="p:recharge1",
                name="ChatGPT Plus 代充值 · 便宜代充",
                merchant_name="甲店",
                sale_price=5.2,
                stock=10,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/recharge1",
            )
            session.add(product)
            session.add(recharge)
            session.add(
                Product(
                    shop_id=shop.id,
                    external_id="p:sms1",
                    name="OpenAI/ChatGPT接码 · ChatGPT 短信验证码",
                    merchant_name="接码店",
                    sale_price=0.2,
                    stock=99,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/sms1",
                )
            )
            session.commit()

            products, total = _db_search(
                session,
                "GPT Plus",
                "",
                1,
                50,
                True,
                source_scopes={},
                snapshot_source_shop_ids={shop.id},
                retail_scopes={},
            )
            self.assertEqual(total, 1)
            # 搜 Plus 只进标准 Plus 分类，不能混入更便宜的代充或接码服务。
            self.assertEqual([item.external_id for item in products], ["p:plus1"])

            recharge_products, recharge_total = _db_search(
                session,
                "GPT Plus 代充值",
                "",
                1,
                50,
                True,
                source_scopes={},
                snapshot_source_shop_ids={shop.id},
                retail_scopes={},
            )
            self.assertEqual(recharge_total, 1)
            self.assertEqual(
                [item.external_id for item in recharge_products],
                ["p:recharge1"],
            )

            sms_products, sms_total = _db_search(
                session,
                "接码",
                "",
                1,
                50,
                True,
                source_scopes={},
                snapshot_source_shop_ids={shop.id},
                retail_scopes={},
            )
            self.assertEqual(sms_total, 1)
            self.assertEqual([item.external_id for item in sms_products], ["p:sms1"])

    def test_k12_search_rejects_grok12_false_positive(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="实时原店", kind=SourceKind.PUBLIC_SHOP, url="live")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            for external_id, name in (
                ("gpt-k12", "GPT Team K12 成品"),
                ("grok12", "Super Grok12 个月年卡"),
                ("claude-k12", "Claude K12 教师版"),
            ):
                session.add(
                    Product(
                        shop_id=shop.id,
                        external_id=external_id,
                        name=name,
                        category=Category.CARD,
                        sale_price=1,
                        stock=1,
                        status=ProductStatus.NORMAL,
                        url=f"https://pay.ldxp.cn/item/{external_id}",
                    )
                )
            session.commit()

            products, total = _db_search(session, "K12", "", 1, 50, True)

        self.assertEqual(total, 1)
        self.assertEqual([item.external_id for item in products], ["gpt-k12"])

    def test_strict_search_rejects_verification_from_previous_request(self) -> None:
        db_engine = memory_engine()
        verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        with Session(db_engine) as session:
            shop = Shop(name="PickAI · 公开报价索引", kind=SourceKind.SOURCE_SQUARE)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id,
                    external_id="p:plus-old",
                    name="ChatGPT Plus · 上一次搜索结果",
                    category=Category.CARD,
                    sale_price=8,
                    stock=9,
                    status=ProductStatus.NORMAL,
                    url="https://pay.ldxp.cn/item/plus-old",
                    inventory_verified_at=verified_at,
                )
            )
            session.commit()

            products, total = _db_search(
                session,
                "ChatGPT Plus",
                "",
                1,
                20,
                True,
                source_scopes={shop.id: {"p:plus-old"}},
                retail_scopes={},
                require_pickai_origin=True,
                verified_after=verified_at + timedelta(milliseconds=1),
            )

        self.assertEqual(total, 0)
        self.assertEqual(products, [])


if __name__ == "__main__":
    unittest.main()
