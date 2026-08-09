"""数据库引擎与会话。"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine = create_engine(
    settings.db_url,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 5},
)


if engine.url.get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        """允许后台刷新写入时，前台搜索继续并发读取本地索引。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def init_db() -> None:
    # 导入以注册表结构
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _ensure_columns()
    _ensure_search_index()


def _ensure_columns() -> None:
    """保留旧库并修正不可售商品的库存不变量。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        shop_columns = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info(shop)")).all()
        }
        for column, definition in {
            "directory_refresh_time": "INTEGER",
            "directory_goods_count": "INTEGER",
            "directory_status": "INTEGER",
        }.items():
            if column not in shop_columns:
                conn.execute(
                    text(f"ALTER TABLE shop ADD COLUMN {column} {definition}")
                )

        product_columns = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info(product)")).all()
        }
        if "inventory_verified_at" not in product_columns:
            conn.execute(
                text("ALTER TABLE product ADD COLUMN inventory_verified_at DATETIME")
            )
        if "origin_category_id" not in product_columns:
            conn.execute(
                text("ALTER TABLE product ADD COLUMN origin_category_id INTEGER")
            )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_product_origin_category_id "
                "ON product (origin_category_id)"
            )
        )

        # 历史版本曾保留未上架商品的正库存，导致仅按库存筛选时误判有货。
        # 建立统一不变量：非正常状态的可售库存一律为 0。
        conn.execute(
            text(
                "UPDATE product SET stock = 0 "
                "WHERE status IN ('OFF', 'OUT') AND stock != 0"
            )
        )
        schema_version = int(
            conn.execute(text("PRAGMA user_version")).scalar_one() or 0
        )
        if schema_version < 1:
            # 旧版忽略了公开列表的 extend.stock_count。仅执行一次全量失效，
            # 让后台索引重新抓取各店并写入真实库存。
            conn.execute(
                text(
                    "UPDATE shop SET last_synced_at = NULL "
                    "WHERE kind = 'PUBLIC_SHOP'"
                )
            )
            conn.execute(text("PRAGMA user_version = 1"))
        conn.commit()


def _ensure_search_index(db_engine=engine) -> bool:
    """创建紧凑文本的 FTS5 trigram 索引；不可用时搜索层自动回退 LIKE。"""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    if db_engine.url.get_backend_name() != "sqlite":
        return False

    compact_sql = (
        "lower(replace(replace(replace("
        "coalesce(new.name, '') || coalesce(new.merchant_name, ''), "
        "' ', ''), char(9), ''), char(10), ''))"
    )
    try:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS product_fts "
                    "USING fts5(search_text, tokenize='trigram')"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS product_fts_ai "
                    "AFTER INSERT ON product BEGIN "
                    "INSERT INTO product_fts(rowid, search_text) "
                    f"VALUES (new.id, {compact_sql}); END"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS product_fts_au "
                    "AFTER UPDATE OF name, merchant_name ON product BEGIN "
                    "DELETE FROM product_fts WHERE rowid = old.id; "
                    "INSERT INTO product_fts(rowid, search_text) "
                    f"VALUES (new.id, {compact_sql}); END"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS product_fts_ad "
                    "AFTER DELETE ON product BEGIN "
                    "DELETE FROM product_fts WHERE rowid = old.id; END"
                )
            )

            product_count = int(
                conn.execute(text("SELECT count(*) FROM product")).scalar_one()
                or 0
            )
            fts_count = int(
                conn.execute(text("SELECT count(*) FROM product_fts")).scalar_one()
                or 0
            )
            schema_version = int(
                conn.execute(text("PRAGMA user_version")).scalar_one() or 0
            )
            if schema_version < 2 or product_count != fts_count:
                conn.execute(text("DELETE FROM product_fts"))
                conn.execute(
                    text(
                        "INSERT INTO product_fts(rowid, search_text) "
                        "SELECT id, "
                        "lower(replace(replace(replace("
                        "coalesce(name, '') || coalesce(merchant_name, ''), "
                        "' ', ''), char(9), ''), char(10), '')) "
                        "FROM product"
                    )
                )
                conn.execute(text("PRAGMA user_version = 2"))
        return True
    except OperationalError:
        return False


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
