from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.crawler.base import ProductRecord
from app.crawler.source_square import ShopDirectoryEntry
from app.models import Product, ProductStatus, Shop, SourceKind
from app.retail_index import RetailIndexCoordinator


def memory_engine():
    db_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(db_engine)
    return db_engine


class ClosedFlag:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeDirectory:
    instances: list["FakeDirectory"] = []
    entries: list[ShopDirectoryEntry] = []

    def __init__(self) -> None:
        self.fetcher = ClosedFlag()
        self.__class__.instances.append(self)

    def list_shops(self) -> list[ShopDirectoryEntry]:
        return list(self.entries)


class FakeIndexApi:
    instances: list["FakeIndexApi"] = []
    calls: list[str] = []
    failing_tokens: set[str] = set()
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.fetcher = ClosedFlag()
        self.__class__.instances.append(self)

    def fetch(self, token: str, shop_name: str | None = None) -> list[ProductRecord]:
        with self._lock:
            self.__class__.calls.append(token)
        if token in self.failing_tokens:
            raise RuntimeError("mock shop failure")
        return [
            ProductRecord(
                external_id=f"r:{token}",
                name=f"{shop_name or token} 商品",
                merchant_name=shop_name or token,
                stock=-1,
                status=ProductStatus.NORMAL,
                url=f"https://pay.ldxp.cn/item/item-{len(self.calls)}",
            )
        ]


class RetailIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeDirectory.instances.clear()
        FakeIndexApi.instances.clear()
        FakeIndexApi.calls.clear()
        FakeIndexApi.failing_tokens.clear()

    def test_automatic_upsert_resume_progress_and_fresh_snapshot_skip(self) -> None:
        db_engine = memory_engine()
        FakeDirectory.entries = [
            ShopDirectoryEntry("中文店", "中文目录店", 12),
            ShopDirectoryEntry("FRESH", "目录中的新名称", 4),
        ]
        with Session(db_engine) as session:
            fresh = Shop(
                name="旧名称",
                url="fresh",
                kind=SourceKind.PUBLIC_SHOP,
                last_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            stale = Shop(
                name="公开索引补充店",
                url="stale",
                kind=SourceKind.PUBLIC_SHOP,
            )
            session.add(fresh)
            session.add(stale)
            session.commit()
            session.refresh(fresh)
            session.add(
                Product(
                    shop_id=fresh.id,
                    external_id="r:fresh-existing",
                    name="仍有效的完整快照商品",
                    stock=-1,
                    status=ProductStatus.NORMAL,
                )
            )
            session.commit()

        coordinator = RetailIndexCoordinator(
            db_engine=db_engine,
            directory_factory=FakeDirectory,
            shop_api_factory=FakeIndexApi,
            token_provider=lambda: "test-token",
            max_concurrency=2,
        )
        started = coordinator.start()
        self.assertTrue(started["running"])
        self.assertTrue(coordinator.wait(5))

        status = coordinator.status()
        self.assertEqual(status["state"], "ready")
        self.assertFalse(status["running"])
        self.assertEqual(status["discovered_shops"], 3)
        self.assertEqual(status["indexed_shops"], 3)
        self.assertEqual(status["pending_shops"], 0)
        self.assertEqual(status["failed_shops"], 0)
        self.assertEqual(status["product_count"], 3)
        self.assertEqual(status["progress"], 100.0)
        self.assertIn("不代表全部", status["coverage_note"])
        self.assertEqual(set(FakeIndexApi.calls), {"中文店", "stale"})
        self.assertNotIn("fresh", FakeIndexApi.calls)
        self.assertTrue(FakeDirectory.instances[0].fetcher.closed)
        self.assertTrue(all(instance.fetcher.closed for instance in FakeIndexApi.instances))

        with Session(db_engine) as session:
            shops = session.exec(
                select(Shop).where(Shop.kind == SourceKind.PUBLIC_SHOP)
            ).all()
            self.assertEqual(len(shops), 3)
            by_token = {shop.url.casefold(): shop for shop in shops}
            self.assertEqual(by_token["fresh"].name, "目录中的新名称")
            self.assertEqual(by_token["fresh"].directory_refresh_time, 0)
            self.assertEqual(by_token["fresh"].directory_goods_count, 4)
            self.assertEqual(by_token["fresh"].directory_status, 1)
            self.assertIn("目录商品数 12", by_token["中文店"].note)
            self.assertTrue(all(shop.last_synced_at is not None for shop in shops))

        first_calls = list(FakeIndexApi.calls)
        coordinator.start()
        self.assertTrue(coordinator.wait(5))
        self.assertEqual(FakeIndexApi.calls, first_calls)
        self.assertEqual(coordinator.status()["indexed_shops"], 3)

    def test_changed_directory_fingerprint_reindexes_fresh_shop(self) -> None:
        db_engine = memory_engine()
        FakeDirectory.entries = [
            ShopDirectoryEntry(
                "changed-shop",
                "已更新店铺",
                8,
                refresh_time=200,
                status=1,
            )
        ]
        with Session(db_engine) as session:
            session.add(
                Shop(
                    name="旧店名",
                    url="changed-shop",
                    kind=SourceKind.PUBLIC_SHOP,
                    last_synced_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    directory_refresh_time=100,
                    directory_goods_count=7,
                    directory_status=1,
                )
            )
            session.commit()

        coordinator = RetailIndexCoordinator(
            db_engine=db_engine,
            directory_factory=FakeDirectory,
            shop_api_factory=FakeIndexApi,
            token_provider=lambda: "test-token",
            max_concurrency=1,
        )
        coordinator.start()
        self.assertTrue(coordinator.wait(5))

        self.assertEqual(FakeIndexApi.calls, ["changed-shop"])
        self.assertEqual(coordinator.status()["changed_shops"], 1)
        with Session(db_engine) as session:
            shop = session.exec(
                select(Shop).where(Shop.url == "changed-shop")
            ).one()
            self.assertEqual(shop.name, "已更新店铺")
            self.assertEqual(shop.directory_refresh_time, 200)
            self.assertEqual(shop.directory_goods_count, 8)
            self.assertIsNotNone(shop.last_synced_at)

    def test_failed_shop_remains_unsynced_and_resumes_next_run(self) -> None:
        db_engine = memory_engine()
        FakeDirectory.entries = [ShopDirectoryEntry("retry-shop", "重试店", 1)]
        FakeIndexApi.failing_tokens.add("retry-shop")
        first = RetailIndexCoordinator(
            db_engine=db_engine,
            directory_factory=FakeDirectory,
            shop_api_factory=FakeIndexApi,
            token_provider=lambda: "test-token",
            max_concurrency=1,
        )
        first.start()
        self.assertTrue(first.wait(5))
        failed_status = first.status()
        self.assertEqual(failed_status["state"], "error")
        self.assertEqual(failed_status["failed_shops"], 1)
        self.assertEqual(failed_status["pending_shops"], 1)
        self.assertEqual(failed_status["progress"], 0.0)
        self.assertIn("retry-shop", failed_status["failures"][0])

        with Session(db_engine) as session:
            shop = session.exec(select(Shop).where(Shop.url == "retry-shop")).one()
            self.assertIsNone(shop.last_synced_at)

        FakeIndexApi.failing_tokens.clear()
        second = RetailIndexCoordinator(
            db_engine=db_engine,
            directory_factory=FakeDirectory,
            shop_api_factory=FakeIndexApi,
            token_provider=lambda: "test-token",
            max_concurrency=1,
        )
        second.start()
        self.assertTrue(second.wait(5))
        self.assertEqual(second.status()["state"], "ready")
        with Session(db_engine) as session:
            shop = session.exec(select(Shop).where(Shop.url == "retry-shop")).one()
            self.assertIsNotNone(shop.last_synced_at)

    def test_duplicate_start_reuses_running_job(self) -> None:
        db_engine = memory_engine()
        entered = threading.Event()
        release = threading.Event()
        directory_calls = 0

        class BlockingDirectory:
            def list_shops(self) -> list[ShopDirectoryEntry]:
                nonlocal directory_calls
                directory_calls += 1
                entered.set()
                release.wait(5)
                return []

        coordinator = RetailIndexCoordinator(
            db_engine=db_engine,
            directory_factory=BlockingDirectory,
            shop_api_factory=FakeIndexApi,
            token_provider=lambda: "test-token",
        )
        try:
            first = coordinator.start()
            self.assertTrue(entered.wait(2))
            second = coordinator.start()
            self.assertTrue(first["running"])
            self.assertTrue(second["running"])
            self.assertEqual(first["started_at"], second["started_at"])
            self.assertEqual(directory_calls, 1)
        finally:
            release.set()
        self.assertTrue(coordinator.wait(5))
        self.assertEqual(coordinator.status()["state"], "ready")


if __name__ == "__main__":
    unittest.main()
