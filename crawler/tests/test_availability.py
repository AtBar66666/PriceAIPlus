from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.crawler.base import ProductRecord
from app.crawler.shop_api import ShopApi
from app.crawler.source_square import SourceSquare
from app.models import Product, ProductStatus, Shop, SourceKind
from app.service import cached_search, ingest


class AvailabilityTests(unittest.TestCase):
    def test_partial_ingest_does_not_mark_shop_as_fully_synced(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            shop = Shop(name="关键词候选店", url="partial", kind=SourceKind.PUBLIC_SHOP)
            session.add(shop)
            session.commit()
            session.refresh(shop)

            ingest(
                session,
                shop,
                [ProductRecord(external_id="r:partial", name="关键词命中商品", stock=-1)],
            )
            session.refresh(shop)
            self.assertIsNone(shop.last_synced_at)

            ingest(session, shop, [], complete_snapshot=True)
            session.refresh(shop)
            self.assertIsNotNone(shop.last_synced_at)

    def test_source_square_unlisted_item_never_keeps_positive_stock(self) -> None:
        record = SourceSquare._map(
            {
                "id": 1,
                "name": "已下架商品",
                "status": 0,
                "stock_count": 122,
                "price": 9.9,
            }
        )

        self.assertEqual(record.status, ProductStatus.OFF)
        self.assertEqual(record.stock, 0)

    def test_retail_mapper_respects_explicit_unlisted_status(self) -> None:
        record = ShopApi._map(
            {
                "goods_key": "gone",
                "name": "已下架商品",
                "status": 0,
                "price": 9.9,
            },
            "测试店铺",
        )

        self.assertEqual(record.status, ProductStatus.OFF)
        self.assertEqual(record.stock, 0)

    def test_retail_mapper_reads_nested_list_inventory(self) -> None:
        available = ShopApi._map(
            {
                "goods_key": "available",
                "name": "有库存商品",
                "extend": {"stock_count": 12},
            },
            "测试店铺",
        )
        sold_out = ShopApi._map(
            {
                "goods_key": "sold-out",
                "name": "无库存商品",
                "extend": {"stock_count": 0},
            },
            "测试店铺",
        )

        self.assertEqual(available.status, ProductStatus.NORMAL)
        self.assertEqual(available.stock, 12)
        self.assertEqual(sold_out.status, ProductStatus.OUT)
        self.assertEqual(sold_out.stock, 0)

    def test_unknown_retail_stock_does_not_count_as_in_stock(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            shop = Shop(name="库存未知店", kind=SourceKind.PUBLIC_SHOP)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:unknown",
                    name="GPT 库存未知商品",
                    stock=-1,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()

            products, total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
            )

        self.assertEqual(products, [])
        self.assertEqual(total, 0)

    def test_stale_positive_retail_stock_expires_to_unknown(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            shop = Shop(name="过期库存店", kind=SourceKind.PUBLIC_SHOP)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="stale-stock",
                    name="GPT 过期库存",
                    sale_price=1,
                    stock=9,
                    status=ProductStatus.NORMAL,
                    last_seen_at=(
                        datetime.now(timezone.utc).replace(tzinfo=None)
                        - timedelta(minutes=5)
                    ),
                )
            )
            session.commit()

            all_products, all_total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=False,
            )
            available, available_total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
            )

        self.assertEqual(all_total, 1)
        self.assertEqual(all_products[0].stock, -1)
        self.assertEqual(available, [])
        self.assertEqual(available_total, 0)

    def test_unknown_detail_does_not_erase_known_out_of_stock(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            shop = Shop(name="明确缺货店", kind=SourceKind.PUBLIC_SHOP)
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add(
                Product(
                    shop_id=shop.id or 0,
                    external_id="r:item",
                    name="GPT 商品",
                    sale_price=1.6,
                    stock=0,
                    status=ProductStatus.OUT,
                )
            )
            session.commit()
            ingest(
                session,
                shop,
                [
                    ProductRecord(
                        external_id="r:item",
                        name="GPT 商品",
                        sale_price=1.8,
                        stock=-1,
                        status=ProductStatus.NORMAL,
                    )
                ],
            )
            product = session.exec(
                select(Product).where(Product.external_id == "r:item")
            ).one()

        self.assertEqual(product.sale_price, 1.8)
        self.assertEqual(product.stock, 0)
        self.assertEqual(product.status, ProductStatus.OUT)

    def test_source_zero_stock_overrides_duplicate_retail_listing(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            source_shop = Shop(name="货源广场", kind=SourceKind.SOURCE_SQUARE)
            retail_shop = Shop(name="公开零售店", kind=SourceKind.PUBLIC_SHOP)
            session.add(source_shop)
            session.add(retail_shop)
            session.commit()
            session.refresh(source_shop)
            session.refresh(retail_shop)
            url = "https://pay.ldxp.cn/item/out"
            session.add(
                Product(
                    shop_id=source_shop.id or 0,
                    external_id="source-out",
                    name="GPT 重复商品",
                    sale_price=10,
                    stock=0,
                    status=ProductStatus.OUT,
                    url=url,
                )
            )
            session.add(
                Product(
                    shop_id=retail_shop.id or 0,
                    external_id="r:out",
                    name="GPT 重复商品",
                    sale_price=8,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url=url,
                )
            )
            session.commit()

            products, total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=False,
            )
            available, available_total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
            )

        self.assertEqual(total, 1)
        self.assertEqual(products[0].sale_price, 8)
        self.assertEqual(products[0].stock, 0)
        self.assertEqual(products[0].status, ProductStatus.OUT)
        self.assertEqual(available, [])
        self.assertEqual(available_total, 0)

    def test_fresh_source_stock_enriches_duplicate_retail_price(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            source_shop = Shop(name="货源广场", kind=SourceKind.SOURCE_SQUARE)
            retail_shop = Shop(name="公开零售店", kind=SourceKind.PUBLIC_SHOP)
            session.add(source_shop)
            session.add(retail_shop)
            session.commit()
            session.refresh(source_shop)
            session.refresh(retail_shop)
            url = "https://pay.ldxp.cn/item/available"
            session.add(
                Product(
                    shop_id=source_shop.id or 0,
                    external_id="source-available",
                    name="GPT 有货商品",
                    sale_price=10,
                    stock=7,
                    status=ProductStatus.NORMAL,
                    url=url,
                )
            )
            session.add(
                Product(
                    shop_id=retail_shop.id or 0,
                    external_id="r:available",
                    name="GPT 有货商品",
                    sale_price=8,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url=url,
                )
            )
            session.commit()

            products, total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
            )

        self.assertEqual(total, 1)
        self.assertEqual(products[0].sale_price, 8)
        self.assertEqual(products[0].stock, 7)
        self.assertEqual(products[0].status, ProductStatus.NORMAL)

    def test_newer_source_price_replaces_stale_retail_snapshot(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)
        observed_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1)
        )

        with Session(engine) as session:
            source_shop = Shop(name="货源广场", kind=SourceKind.SOURCE_SQUARE)
            retail_shop = Shop(name="公开零售店", kind=SourceKind.PUBLIC_SHOP)
            session.add(source_shop)
            session.add(retail_shop)
            session.commit()
            session.refresh(source_shop)
            session.refresh(retail_shop)
            url = "https://pay.ldxp.cn/item/changed-price"
            session.add(
                Product(
                    shop_id=retail_shop.id or 0,
                    external_id="r:changed-price",
                    name="GPT 调价商品",
                    sale_price=196,
                    stock=-1,
                    status=ProductStatus.NORMAL,
                    url=url,
                    last_seen_at=observed_at,
                )
            )
            session.add(
                Product(
                    shop_id=source_shop.id or 0,
                    external_id="source-changed-price",
                    name="GPT 调价商品",
                    sale_price=189,
                    stock=6,
                    status=ProductStatus.NORMAL,
                    url=url,
                    last_seen_at=observed_at + timedelta(hours=2),
                )
            )
            session.commit()

            products, total = cached_search(
                session,
                "GPT",
                page_size=0,
                in_stock_only=True,
            )

        self.assertEqual(total, 1)
        self.assertEqual(products[0].sale_price, 189)
        self.assertEqual(products[0].stock, 6)

    def test_complete_retail_snapshot_marks_disappeared_items_unlisted(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            shop = Shop(
                name="测试店铺",
                url="test",
                kind=SourceKind.PUBLIC_SHOP,
                product_count=1,
            )
            session.add(shop)
            session.commit()
            session.refresh(shop)

            product = Product(
                shop_id=shop.id,
                external_id="r:gone",
                name="已消失商品",
                status=ProductStatus.NORMAL,
                stock=-1,
            )
            session.add(product)
            session.commit()
            session.refresh(product)
            product_id = product.id

            ingest(session, shop, [], complete_snapshot=True)

            refreshed = session.get(Product, product_id)
            refreshed_shop = session.get(Shop, shop.id)
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.status, ProductStatus.OFF)
            self.assertEqual(refreshed.stock, 0)
            self.assertEqual(refreshed_shop.product_count, 0)


if __name__ == "__main__":
    unittest.main()
