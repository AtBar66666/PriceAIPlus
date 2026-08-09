from __future__ import annotations

import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.crawler.base import ProductRecord
from app.crawler.shop_api import ShopApi, shop_token, shop_url_token
from app.models import Category, ProductStatus, SourceKind
from app.service import _db_search, add_retail_shop, sync_retail_shop


class FakeShopApi(ShopApi):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def shop_name(self, token: str) -> str:
        return "测试零售店"

    def shop_info(self, token: str) -> dict:
        return {"nickname": "测试零售店"}

    def category_ids(self, token: str, goods_type: str) -> list[int]:
        return []

    def _post(self, path: str, body: dict) -> dict:
        self.assert_goods_list(path)
        goods_type = str(body["goods_type"])
        current = int(body["current"])
        self.calls.append((goods_type, current))

        if goods_type != "card":
            return {"code": 1, "data": {"list": [], "total": 0}}

        pages = {
            1: [
                {"goods_key": "a", "goods_type": "card", "name": "Bug Team A", "price": 9},
                {"goods_key": "b", "goods_type": "card", "name": "Bug Team B", "price": 12},
            ],
            2: [
                {"goods_key": "c", "goods_type": "card", "name": "Claude Pro", "price": 15},
            ],
        }
        return {"code": 1, "data": {"list": pages.get(current, []), "total": 3}}

    @staticmethod
    def assert_goods_list(path: str) -> None:
        if path != "/shopApi/Shop/goodsList":
            raise AssertionError(f"unexpected path: {path}")


class StaticRecordsShopApi:
    def shop_name(self, token: str) -> str:
        return "测试零售店"

    def fetch(self, target: str, max_pages: int | None = None, shop_name: str | None = None) -> list[ProductRecord]:
        return [
            ProductRecord(
                external_id="r:bug-team",
                name="GPT Bug Team 零售商品",
                category=Category.CARD,
                merchant_name=shop_name or "测试零售店",
                sale_price=8.8,
                stock=-1,
                status=ProductStatus.NORMAL,
                url="https://pay.ldxp.cn/item/test",
            )
        ]


class RetailShopTests(unittest.TestCase):
    def test_shop_url_accepts_uppercase_token_and_rejects_item_url(self) -> None:
        url = "https://pay.ldxp.cn/shop/LV9C7XJE/?from=test#goods"
        self.assertEqual(shop_url_token(url), "LV9C7XJE")
        self.assertEqual(shop_token(url), "LV9C7XJE")
        self.assertEqual(shop_token("LV9C7XJE"), "LV9C7XJE")
        self.assertEqual(shop_url_token("https://pay.ldxp.cn/item/qudtro"), "")
        self.assertEqual(shop_token("https://example.com/shop/LV9C7XJE"), "")

    def test_fetch_reads_every_retail_page_and_category(self) -> None:
        source = FakeShopApi()
        records = source.fetch("LV9C7XJE", shop_name="测试零售店")

        self.assertEqual([record.external_id for record in records], ["r:a", "r:b", "r:c"])
        self.assertIn(("card", 2), source.calls)
        self.assertTrue(all(record.status == ProductStatus.NORMAL for record in records))
        self.assertTrue(all(record.stock == -1 for record in records))

    def test_synced_retail_products_join_keyword_search_as_unknown_stock(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            source = StaticRecordsShopApi()
            shop = add_retail_shop(session, "https://pay.ldxp.cn/shop/LV9C7XJE", source=source)  # type: ignore[arg-type]
            synced = sync_retail_shop(session, shop, source)  # type: ignore[arg-type]
            products, total = _db_search(
                session,
                keywords="bug team",
                goods_type="",
                current=1,
                page_size=20,
                in_stock_only=False,
            )

            self.assertEqual(shop.kind, SourceKind.PUBLIC_SHOP)
            self.assertEqual(synced, {"r:bug-team"})
            self.assertEqual(total, 1)
            self.assertEqual(products[0].merchant_name, "测试零售店")
            self.assertEqual(products[0].name, "GPT Bug Team 零售商品")
            self.assertEqual(products[0].stock, -1)


if __name__ == "__main__":
    unittest.main()
