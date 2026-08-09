"""抓取适配器的公共结构。

每个数据源（登录货源广场 / 公开店铺页）都把原始数据归一化成
`ProductRecord`，落库逻辑只认这个结构，互不耦合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Category, ProductStatus


@dataclass
class ProductRecord:
    external_id: str
    name: str
    category: Category = Category.OTHER
    merchant_name: str = ""
    merchant_link: str = ""
    sale_price: float = 0.0
    agent_price: float = 0.0
    cost_price: float = 0.0
    stock: int = 0
    status: ProductStatus = ProductStatus.NORMAL
    is_linked: bool = False
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """从字典里按候选键名取第一个命中的值，容忍字段命名不确定。"""
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(str(value).replace("¥", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def guess_category(raw: Any) -> Category:
    text = str(raw or "")
    for cat in (Category.CARD, Category.KNOWLEDGE, Category.RESOURCE, Category.RIGHTS):
        if cat.value in text:
            return cat
    return Category.OTHER


def guess_status(raw: Any, stock: int) -> ProductStatus:
    text = str(raw or "")
    if "未上架" in text or "下架" in text:
        return ProductStatus.OFF
    if "缺货" in text or stock <= 0:
        return ProductStatus.OUT
    return ProductStatus.NORMAL


class Source:
    kind: str = "base"

    def fetch(self, target: Optional[str] = None) -> list[ProductRecord]:  # pragma: no cover
        raise NotImplementedError
