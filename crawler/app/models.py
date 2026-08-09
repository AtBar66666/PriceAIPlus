"""商品搜索缓存的数据模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SourceKind(str, Enum):
    SOURCE_SQUARE = "source_square"  # 登录后的货源广场 JSON 接口
    PUBLIC_SHOP = "public_shop"      # 公开的店铺页面（HTML）


class Category(str, Enum):
    CARD = "卡密"
    KNOWLEDGE = "知识"
    RESOURCE = "资源"
    RIGHTS = "权益"
    OTHER = "其他"


class ProductStatus(str, Enum):
    NORMAL = "正常"
    OFF = "未上架"
    OUT = "缺货"


class Shop(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str = ""
    kind: SourceKind = SourceKind.SOURCE_SQUARE
    active: bool = True
    note: str = ""
    product_count: int = 0
    # GoodsPool 目录指纹；目录值变化时才使完整店铺快照失效。
    directory_refresh_time: Optional[int] = Field(default=None, index=True)
    directory_goods_count: Optional[int] = None
    directory_status: Optional[int] = None
    last_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)


class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    shop_id: int = Field(foreign_key="shop.id", index=True)
    external_id: str = Field(index=True, default="")
    name: str = Field(index=True)
    category: Category = Field(default=Category.OTHER, index=True)
    merchant_name: str = Field(default="", index=True)

    # 价格（人民币元）
    sale_price: float = 0.0     # 对外售价
    agent_price: float = 0.0    # 代理价
    cost_price: float = 0.0     # 我的成本价

    stock: int = 0
    status: ProductStatus = Field(default=ProductStatus.NORMAL, index=True)
    is_linked: bool = False      # 是否已对接到我的店铺
    url: str = ""

    first_seen_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now, index=True)
    # 聚合目录的抓取时间不等于原店库存核验时间。两者必须分开保存，
    # 否则 PickAI 刚同步完就会被错误标成“实时有货”。
    inventory_verified_at: Optional[datetime] = Field(default=None, index=True)
    # 原店分类 ID 来自 goodsInfo/goodsList。缓存它以后，同一店后续实时搜索
    # 可直接做一次分类 goodsList，不必每次重复 goodsInfo + goodsList 两连请求。
    origin_category_id: Optional[int] = Field(default=None, index=True)

    # 旧数据库中的 NOT NULL 兼容列；搜索版不再计算或对外展示变化趋势。
    price_delta: float = 0.0
    stock_delta: int = 0

    @property
    def margin(self) -> float:
        """卖价与成本的毛利。"""
        if self.sale_price and self.cost_price:
            return round(self.sale_price - self.cost_price, 2)
        return 0.0

    @property
    def margin_pct(self) -> float:
        if self.cost_price:
            return round((self.sale_price - self.cost_price) / self.cost_price * 100, 1)
        return 0.0
