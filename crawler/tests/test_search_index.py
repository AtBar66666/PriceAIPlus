from __future__ import annotations

import unittest

from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import _ensure_search_index
from app.models import Product, ProductStatus, Shop, SourceKind
from app.service import cached_search


def memory_engine():
    db_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db_engine)
    return db_engine


class SearchIndexTests(unittest.TestCase):
    def test_trigram_index_supports_substrings_tokens_and_pagination(self) -> None:
        db_engine = memory_engine()
        with Session(db_engine) as session:
            shop = Shop(name="索引测试店", kind=SourceKind.PUBLIC_SHOP, url="fts-test")
            session.add(shop)
            session.commit()
            session.refresh(shop)
            session.add_all(
                [
                    Product(
                        shop_id=shop.id,
                        external_id="chatgpt",
                        name="ChatGPT Plus",
                        merchant_name="Alpha",
                        sale_price=20,
                        stock=5,
                        status=ProductStatus.NORMAL,
                    ),
                    Product(
                        shop_id=shop.id,
                        external_id="bug-team",
                        name="Bug Team Card",
                        merchant_name="Beta",
                        sale_price=10,
                        stock=4,
                        status=ProductStatus.NORMAL,
                    ),
                    Product(
                        shop_id=shop.id,
                        external_id="team-bug",
                        name="Team Bug Pack",
                        merchant_name="Gamma",
                        sale_price=12,
                        stock=3,
                        status=ProductStatus.NORMAL,
                    ),
                ]
            )
            session.commit()

        self.assertTrue(_ensure_search_index(db_engine))
        with db_engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT count(*) FROM product_fts")).scalar_one(),
                3,
            )

        with Session(db_engine) as session:
            gpt, gpt_total = cached_search(
                session,
                "GPT",
                page_size=1,
                in_stock_only=False,
            )
            self.assertEqual(gpt_total, 1)
            self.assertEqual([product.name for product in gpt], ["ChatGPT Plus"])

            first_page, total = cached_search(
                session,
                "team bug",
                current=1,
                page_size=1,
                in_stock_only=False,
            )
            second_page, second_total = cached_search(
                session,
                "team bug",
                current=2,
                page_size=1,
                in_stock_only=False,
            )
            self.assertEqual(total, 2)
            self.assertEqual(second_total, 2)
            self.assertEqual(len(first_page), 1)
            self.assertEqual(len(second_page), 1)
            self.assertNotEqual(first_page[0].id, second_page[0].id)

            shop = session.exec(
                text("SELECT id FROM shop WHERE url = 'fts-test'")
            ).scalar_one()
            session.add(
                Product(
                    shop_id=shop,
                    external_id="claude",
                    name="Claude Pro",
                    merchant_name="Delta",
                    sale_price=8,
                    stock=2,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()
            claude, claude_total = cached_search(
                session,
                "Claude",
                page_size=50,
                in_stock_only=False,
            )
            self.assertEqual(claude_total, 1)
            self.assertEqual([product.name for product in claude], ["Claude Pro"])


if __name__ == "__main__":
    unittest.main()
