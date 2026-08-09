"""PickAI 公开报价目录适配器。

PickAI 前台使用无需登录的 JSON 接口。这个适配器完整读取分类、标准商品、
每个标准商品下的全部报价以及中转 API 供应商，并把卡网报价归一化成项目的
``ProductRecord``。请求只携带公开访客头，不会转发链动或云猫凭据。
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from ..models import Category, ProductStatus
from .base import ProductRecord, to_float
from .session import Fetcher
from .shop_api import item_url_key


PICKAI_BASE = "https://pickai.cc"
PICKAI_SHOP_NAME = "PickAI · 公开报价索引"
PICKAI_PAGE_SIZE = 50
_NUMBER_RE = re.compile(r"-?\d[\d,]*")

# 只把 PickAI 官方分类为 ChatGPT 的标准商品纳入当前专库。
# ID 29（OpenAI/ChatGPT 接码）属于“接码”分类，故意不在这里。
CHATGPT_PRODUCT_TYPES: dict[int, str] = {
    1: "ChatGPT 普号",
    2: "ChatGPT Team/ Business",
    3: "ChatGPT Plus",
    4: "ChatGPT Pro 20x",
    5: "ChatGPT Pro 5x",
    6: "ChatGPT Go",
    7: "ChatGPT Plus 代充值",
    8: "Codex/ChatGPT 周边服务",
}
_CHATGPT_DEFAULT_TYPE_IDS = (1, 2, 3, 4, 5, 6, 7)
OPENAI_SMS_PRODUCT_TYPE_ID = 29
OPENAI_SMS_PRODUCT_TYPE_NAME = "OpenAI/ChatGPT接码"
EMAIL_PRODUCT_TYPES: dict[int, str] = {
    21: "Outlook / Hotmail 邮箱",
    22: "iCloud 邮箱",
    23: "Gmail / Google 邮箱",
    24: "教育邮箱",
    25: "其他邮箱",
}
EMAIL_PRODUCT_TYPE_IDS = tuple(EMAIL_PRODUCT_TYPES)

ProgressCallback = Callable[[dict[str, Any]], None]


def is_k12_query(keywords: str) -> bool:
    """K12 是独立快捷分类，不能落入宽泛的 ChatGPT 默认分类。"""
    compact = re.sub(r"\s+", "", (keywords or "").casefold())
    return "k12" in compact


def is_openai_sms_query(keywords: str) -> bool:
    """当前接码入口只对应 PickAI 的 OpenAI/ChatGPT 接码标准商品。"""
    compact = re.sub(r"\s+", "", (keywords or "").casefold())
    return "接码" in compact or "smscode" in compact or "smsverify" in compact


def is_email_query(keywords: str) -> bool:
    """邮箱快捷入口覆盖 PickAI 的五个邮箱标准商品。"""
    compact = re.sub(r"\s+", "", (keywords or "").casefold())
    return any(
        marker in compact
        for marker in (
            "邮箱",
            "email",
            "mail",
            "gmail",
            "outlook",
            "hotmail",
            "icloud",
        )
    )


def is_k12_product_name(name: str) -> bool:
    """排除 ``Grok12`` 等只是字符串相邻的误召回。"""
    compact = re.sub(r"\s+", "", (name or "").casefold())
    return (
        "k12" in compact
        and not any(marker in compact for marker in ("grok", "claude", "gemini"))
    )


def is_chatgpt_plus_product_name(name: str) -> bool:
    """识别原店当前 Plus 商品；允许新上架、尚未被 PickAI 收录的链接。"""
    compact = re.sub(r"\s+", "", (name or "").casefold())
    if "plus" not in compact:
        return False
    # 这些只是教程、普通号或升级素材，不是可购买的 Plus 商品。
    if any(
        marker in compact
        for marker in (
            "非plus",
            "不是plus",
            "不含plus",
            "无plus",
            "plus教程",
            "教程",
            "获取plus方式",
            "开plus专用",
            "开通plus专用",
            "升级plus专用",
            "提链",
            "代开plus",
            "plus代开",
            "邀请额度",
            "pro邀请",
        )
    ):
        return False
    # “未接码/已接码”是 Plus 账号状态；单纯出售号码、验证码的则属于左侧
    # OpenAI 接码分类，不能混进 Plus 最低价里抢到 ¥1 的假榜首。
    if (
        any(marker in compact for marker in ("接码", "验证码", "sms"))
        and not any(marker in compact for marker in ("未接码", "已接码"))
        and not any(
            marker in compact
            for marker in ("账号", "成品", "独享", "月卡", "充值", "账密", "卡密")
        )
    ):
        return False
    return True


def is_openai_sms_product_name(name: str) -> bool:
    """只保留 OpenAI/ChatGPT 相关接码，排除其他平台接码。"""
    compact = re.sub(r"\s+", "", (name or "").casefold())
    # PickAI 的接码标准分类里也会混入“接码教程”。教程不是可调用的接码
    # 服务，即使原店当前有库存也不能占据最低价结果。
    if any(marker in compact for marker in ("教程", "攻略", "使用说明")):
        return False
    # “未接码/已接码”只是账号状态，不等于商家正在卖接码服务。先剥掉
    # 状态词，再要求标题仍明确包含接码/SMS/验证码能力。
    service_text = compact
    for status_text in ("没有接码", "未接码", "已接码", "没接码", "不接码"):
        service_text = service_text.replace(status_text, "")
    if not any(marker in service_text for marker in ("接码", "sms", "验证码")):
        return False
    if any(marker in compact for marker in ("grok", "claude", "gemini")):
        return False
    return any(
        marker in compact
        for marker in ("openai", "chatgpt", "gpt", "codex", "plus")
    )


def is_email_product_name(name: str) -> bool:
    """保留真正的邮箱商品，排除标题里顺带出现“发邮箱链接”的 AI 商品。"""
    compact = re.sub(r"\s+", "", (name or "").casefold())
    if any(marker in compact for marker in ("教程", "攻略", "使用说明")):
        return False
    if any(marker in compact for marker in ("邮箱url", "邮箱链接", "发邮箱url")):
        return False
    if any(
        marker in compact
        for marker in (
            "chatgpt",
            "openai",
            "gptplus",
            "codex",
            "claude",
            "gemini",
            "grok",
            "接码",
            "验证码",
        )
    ):
        return False
    return any(
        marker in compact
        for marker in (
            "邮箱",
            "email",
            "gmail",
            "googlemail",
            "outlook",
            "hotmail",
            "icloud",
        )
    )


def chatgpt_type_ids_for_query(keywords: str) -> tuple[int, ...]:
    """把用户关键词映射到 PickAI 的 ChatGPT 标准商品，排除接码分类。"""
    text = " ".join((keywords or "").casefold().split())
    compact = re.sub(r"\s+", "", text)
    if not text:
        return ()
    if is_k12_query(keywords) or is_email_query(keywords) or is_openai_sms_query(keywords):
        return ()

    has_chatgpt_marker = any(
        marker in compact
        for marker in (
            "chatgpt",
            "gpt",
            "plus",
            "team",
            "business",
            "pro",
            "codex",
            "普号",
            "代充",
            "充值",
        )
    )
    # “go” 太常见，只在明确带 ChatGPT/GPT 时才作为产品名处理。
    has_go = bool(re.search(r"(?:chat\s*gpt|gpt)\s*go\b", text))
    if not has_chatgpt_marker and not has_go:
        return ()

    if any(marker in compact for marker in ("代充", "代充值", "充值")):
        return (7,)
    if "team" in compact or "business" in compact:
        return (2,)
    if "codex" in compact or "周边" in compact:
        return (8,)
    if "pro" in compact:
        if re.search(r"(?:20\s*x|20倍|pro20)", text):
            return (4,)
        if re.search(r"(?:5\s*x|5倍|pro5)", text):
            return (5,)
        return (4, 5)
    if has_go:
        return (6,)
    if "plus" in compact:
        return (3,)
    if "普号" in compact or "free" in compact:
        return (1,)
    return _CHATGPT_DEFAULT_TYPE_IDS


def chatgpt_type_names_for_query(keywords: str) -> tuple[str, ...]:
    return tuple(
        CHATGPT_PRODUCT_TYPES[type_id]
        for type_id in chatgpt_type_ids_for_query(keywords)
    )


def strict_realtime_scope_for_query(
    keywords: str,
) -> Literal["chatgpt", "k12", "email", "openai_sms"] | None:
    if is_k12_query(keywords):
        return "k12"
    if is_email_query(keywords):
        return "email"
    if is_openai_sms_query(keywords):
        return "openai_sms"
    if chatgpt_type_ids_for_query(keywords):
        return "chatgpt"
    return None


def is_strict_realtime_query(keywords: str) -> bool:
    return strict_realtime_scope_for_query(keywords) is not None


def strict_type_ids_for_query(keywords: str) -> tuple[int, ...]:
    if is_email_query(keywords):
        return EMAIL_PRODUCT_TYPE_IDS
    if is_openai_sms_query(keywords):
        return (OPENAI_SMS_PRODUCT_TYPE_ID,)
    return chatgpt_type_ids_for_query(keywords)


def strict_type_names_for_query(keywords: str) -> tuple[str, ...]:
    if is_email_query(keywords):
        return tuple(EMAIL_PRODUCT_TYPES.values())
    if is_openai_sms_query(keywords):
        return (OPENAI_SMS_PRODUCT_TYPE_NAME,)
    return chatgpt_type_names_for_query(keywords)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stock(value: Any) -> int:
    """解析 ``库存 54``；无法确认数量时保留 -1（未知），不伪造有货。"""
    text = str(value or "").strip()
    match = _NUMBER_RE.search(text)
    if match is not None:
        try:
            return max(0, int(match.group(0).replace(",", "")))
        except ValueError:
            pass
    if any(marker in text for marker in ("缺货", "售罄", "无货")):
        return 0
    return -1


def _quote_key(item: dict[str, Any]) -> str:
    url = str(item.get("item_url") or "").strip()
    return url.casefold() or f"pickai-id:{item.get('id')}"


@dataclass
class PickAISnapshot:
    fetched_at: str
    categories: list[dict[str, Any]]
    product_types: list[dict[str, Any]]
    quotes: list[dict[str, Any]]
    relay_providers: dict[str, Any]
    declared_quotes: int
    duplicate_quotes: int
    request_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": PICKAI_BASE,
            "fetched_at": self.fetched_at,
            "summary": {
                "categories": len(self.categories),
                "product_types": len(self.product_types),
                "quotes": len(self.quotes),
                "declared_quotes": self.declared_quotes,
                "duplicates_merged": self.duplicate_quotes,
                "relay_items": len(self.relay_providers.get("items") or []),
                "requests": self.request_count,
            },
            "categories": self.categories,
            "product_types": self.product_types,
            "quotes": self.quotes,
            "relay_providers": self.relay_providers,
        }

    def product_records(self) -> list[ProductRecord]:
        records: list[ProductRecord] = []
        for item in self.quotes:
            url = str(item.get("item_url") or "").strip()
            key = item_url_key(url) or str(item.get("id") or "").strip()
            if not key:
                continue
            stock = _stock(item.get("stock"))
            status = ProductStatus.NORMAL if stock != 0 else ProductStatus.OUT
            raw_name = str(item.get("raw_name") or "未命名商品").strip()
            product_type_names = [
                str(value).strip()
                for value in item.get("product_type_names") or []
                if str(value).strip()
            ]
            # 扁平搜索页没有 PickAI 左侧的“标准商品”上下文。把标准类型作为
            # 前缀保留下来，确保搜索 ChatGPT Plus 时能命中该分类的全部报价，
            # 而不是只命中原始标题里碰巧写了这些词的子集。
            name = (
                f"{' / '.join(product_type_names)} · {raw_name}"
                if product_type_names
                else raw_name
            )
            records.append(
                ProductRecord(
                    external_id=f"p:{key}",
                    name=name,
                    # PickAI 当前主目录是卡网订阅；其产品族分类不是本项目的
                    # card/article/resource/equity 类型，不能错误映射成后者。
                    category=Category.CARD,
                    merchant_name=str(item.get("shop_name") or "未知商家").strip(),
                    sale_price=to_float(item.get("price"), 0.0),
                    stock=stock,
                    status=status,
                    url=url,
                    raw={**item, "_source": "pickai"},
                )
            )
        return records


class PickAICatalog:
    """读取 PickAI 公开 API，支持稳健的全量分页快照。"""

    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        *,
        base_url: str = PICKAI_BASE,
        retries: int = 4,
        retry_delay_s: float = 1.5,
        timeout_s: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or Fetcher(
            credential_policy="public",
            base_url=self.base_url,
            merchant_token="",
            merchant_referer=f"{self.base_url}/",
            timeout_s=timeout_s,
        )
        self._owns_fetcher = fetcher is None
        self.retries = max(1, retries)
        self.retry_delay_s = max(0.0, retry_delay_s)
        self.request_count = 0

    def close(self) -> None:
        if self._owns_fetcher:
            self.fetcher.close()

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self.request_count += 1
                return self.fetcher.get_json(path, params=params)
            except Exception as exc:  # noqa: BLE001 - 公开目录允许退避重试
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(self.retry_delay_s * (2 ** (attempt - 1)))
        raise RuntimeError(f"PickAI 接口请求失败：{path}：{last_error}") from last_error

    @staticmethod
    def _expect_list(payload: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise RuntimeError(f"PickAI {label}返回格式异常")
        return payload

    def categories(self) -> list[dict[str, Any]]:
        return self._expect_list(self._get("/api/backend/categories"), "分类接口")

    def product_types(self) -> list[dict[str, Any]]:
        return self._expect_list(self._get("/api/backend/product-types"), "标准商品接口")

    def relay_providers(self) -> dict[str, Any]:
        payload = self._get("/api/backend/relay-providers")
        if not isinstance(payload, dict):
            raise RuntimeError("PickAI 中转 API 接口返回格式异常")
        return payload

    def search(
        self,
        keywords: str,
        *,
        max_pages: int | None = None,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """读取 PickAI 全站关键词结果；``max_pages=None`` 表示完整分页。"""
        query = " ".join((keywords or "").split())
        if not query:
            return [], 0
        page_size = min(PICKAI_PAGE_SIZE, max(1, page_size))
        page = 1
        items: dict[str, dict[str, Any]] = {}
        total = 0
        while max_pages is None or page <= max_pages:
            payload = self._get(
                "/api/backend/search/products",
                params={"q": query, "page": page, "page_size": page_size},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise RuntimeError("PickAI 商品搜索接口返回格式异常")
            total = max(total, int(payload.get("total") or 0))
            batch = [item for item in payload["items"] if isinstance(item, dict)]
            for item in batch:
                items[_quote_key(item)] = item
            if not payload.get("has_more") or not batch:
                break
            page += 1
        return list(items.values()), total

    def _quotes_for_type(
        self,
        product_type: dict[str, Any],
        progress: ProgressCallback | None = None,
        *,
        max_pages: int | None = None,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int, int]:
        type_id = int(product_type["id"])
        page_size = min(PICKAI_PAGE_SIZE, max(1, page_size))
        page = 1
        total = 0
        result: list[dict[str, Any]] = []
        while True:
            payload = self._get(
                "/api/backend/quotes",
                params={
                    "product_type_id": type_id,
                    "page": page,
                    "page_size": page_size,
                },
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise RuntimeError(f"PickAI 报价接口返回格式异常：product_type_id={type_id}")
            total = max(total, int(payload.get("total") or 0))
            batch = [item for item in payload["items"] if isinstance(item, dict)]
            for raw in batch:
                item = dict(raw)
                item["product_type_ids"] = [type_id]
                item["product_type_names"] = [str(product_type.get("name") or "")]
                item["catalog_categories"] = [str(product_type.get("category") or "")]
                result.append(item)
            if progress is not None:
                progress(
                    {
                        "event": "page",
                        "product_type_id": type_id,
                        "product_type_name": str(product_type.get("name") or ""),
                        "page": page,
                        "received": len(result),
                        "total": total,
                    }
                )
            if (
                not payload.get("has_more")
                or not batch
                or (max_pages is not None and page >= max(1, max_pages))
            ):
                break
            page += 1
            if page > 10_000:
                raise RuntimeError(f"PickAI 分页异常：product_type_id={type_id}")
        return result, total, page

    def search_chatgpt(
        self,
        keywords: str,
        *,
        max_pages: int = 1,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """按 PickAI 标准商品抓 ChatGPT 报价，不做宽泛字符串误召回。"""
        selected_ids = chatgpt_type_ids_for_query(keywords)
        if not selected_ids:
            return [], 0

        return self._search_standard_types(
            selected_ids,
            allowed_categories={"chatgpt"},
            max_pages=max_pages,
            page_size=page_size,
        )

    def search_openai_sms(
        self,
        *,
        max_pages: int = 1,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """只读取 OpenAI/ChatGPT 接码，不混入通用、Google、PayPal 或 KYC。"""
        return self._search_standard_types(
            (OPENAI_SMS_PRODUCT_TYPE_ID,),
            allowed_categories={"接码"},
            expected_names={
                OPENAI_SMS_PRODUCT_TYPE_ID: OPENAI_SMS_PRODUCT_TYPE_NAME,
            },
            max_pages=max_pages,
            page_size=page_size,
        )

    def search_email(
        self,
        *,
        max_pages: int = 1,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """读取全部邮箱标准商品，不混入接码或 AI 账号分类。"""
        return self._search_standard_types(
            EMAIL_PRODUCT_TYPE_IDS,
            allowed_categories={"邮箱"},
            expected_names=EMAIL_PRODUCT_TYPES,
            max_pages=max_pages,
            page_size=page_size,
        )

    def search_current_strict(
        self,
        keywords: str,
        *,
        max_pages: int = 1,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """快速读取严格分类的当前低价页。

        四个快捷分类使用的标准商品 ID 是项目明确维护的固定映射。搜索热路径
        不应先请求一次 ``product-types`` 再请求报价，否则 PickAI 目录本身就会
        多拖约 1.5 秒。接口异常时上层仍会退回本地候选，但绝不会把目录库存
        当作最终库存。
        """
        selected_ids = strict_type_ids_for_query(keywords)
        if not selected_ids:
            return [], 0

        quotes_by_key: dict[str, dict[str, Any]] = {}
        declared_total = 0
        for type_id in selected_ids:
            if type_id == OPENAI_SMS_PRODUCT_TYPE_ID:
                product_type = {
                    "id": type_id,
                    "name": OPENAI_SMS_PRODUCT_TYPE_NAME,
                    "category": "接码",
                }
            elif type_id in EMAIL_PRODUCT_TYPES:
                product_type = {
                    "id": type_id,
                    "name": EMAIL_PRODUCT_TYPES[type_id],
                    "category": "邮箱",
                }
            else:
                type_name = CHATGPT_PRODUCT_TYPES.get(type_id)
                if not type_name:
                    continue
                product_type = {
                    "id": type_id,
                    "name": type_name,
                    "category": "ChatGPT",
                }
            items, total, _pages = self._quotes_for_type(
                product_type,
                max_pages=max_pages,
                page_size=page_size,
            )
            declared_total += total
            for item in items:
                key = _quote_key(item)
                existing = quotes_by_key.get(key)
                if existing is None:
                    quotes_by_key[key] = item
                else:
                    self._merge_quote(existing, item)

        order = {type_id: index for index, type_id in enumerate(selected_ids)}
        quotes = sorted(
            quotes_by_key.values(),
            key=lambda item: (
                min(
                    (
                        order.get(int(type_id), 10**9)
                        for type_id in item.get("product_type_ids") or []
                    ),
                    default=10**9,
                ),
                to_float(item.get("price"), 10**12),
                str(item.get("item_url") or ""),
            ),
        )
        return quotes, declared_total

    def _search_standard_types(
        self,
        selected_ids: tuple[int, ...],
        *,
        allowed_categories: set[str],
        expected_names: dict[int, str] | None = None,
        max_pages: int = 1,
        page_size: int = PICKAI_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        normalized_categories = {
            category.strip().casefold() for category in allowed_categories
        }

        live_types = {
            int(item["id"]): item
            for item in self.product_types()
            if str(item.get("category") or "").strip().casefold()
            in normalized_categories
            and str(item.get("id") or "").isdigit()
        }
        if expected_names:
            live_types = {
                type_id: item
                for type_id, item in live_types.items()
                if expected_names.get(type_id) == str(item.get("name") or "").strip()
            }
        quotes_by_key: dict[str, dict[str, Any]] = {}
        declared_total = 0
        for type_id in selected_ids:
            product_type = live_types.get(type_id)
            if product_type is None:
                continue
            items, total, _pages = self._quotes_for_type(
                product_type,
                max_pages=max_pages,
                page_size=page_size,
            )
            declared_total += total
            for item in items:
                key = _quote_key(item)
                existing = quotes_by_key.get(key)
                if existing is None:
                    quotes_by_key[key] = item
                else:
                    self._merge_quote(existing, item)

        order = {type_id: index for index, type_id in enumerate(selected_ids)}
        quotes = sorted(
            quotes_by_key.values(),
            key=lambda item: (
                min(
                    (order.get(int(type_id), 10**9) for type_id in item.get("product_type_ids") or []),
                    default=10**9,
                ),
                to_float(item.get("price"), 10**12),
                str(item.get("item_url") or ""),
            ),
        )
        return quotes, declared_total

    @staticmethod
    def _merge_quote(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
        for field in ("product_type_ids", "product_type_names", "catalog_categories"):
            values = list(existing.get(field) or [])
            for value in incoming.get(field) or []:
                if value not in values:
                    values.append(value)
            existing[field] = values
        # 同一商品若在抓取过程中更新，保留更新时间较新的那份报价字段。
        if str(incoming.get("updated_at") or "") > str(existing.get("updated_at") or ""):
            metadata = {
                field: existing[field]
                for field in ("product_type_ids", "product_type_names", "catalog_categories")
            }
            existing.update(incoming)
            existing.update(metadata)

    def full_snapshot(
        self,
        *,
        workers: int = 3,
        progress: ProgressCallback | None = None,
    ) -> PickAISnapshot:
        """完整读取全部标准商品分页，失败时不返回残缺快照。"""
        categories = self.categories()
        product_types = self.product_types()
        relay = self.relay_providers()
        type_results: dict[int, tuple[list[dict[str, Any]], int, int, int]] = {}

        # 注入 fake fetcher 的单元测试必须复用该 fetcher；生产环境才并发创建
        # 每线程独立会话。全局 HostThrottle 仍会限制同主机请求节奏。
        worker_count = 1 if not self._owns_fetcher else min(max(1, workers), 6)
        if worker_count == 1:
            for product_type in product_types:
                before = self.request_count
                items, total, pages = self._quotes_for_type(product_type, progress)
                type_results[int(product_type["id"])] = (
                    items,
                    total,
                    pages,
                    self.request_count - before,
                )
                if progress is not None:
                    progress({"event": "type", "product_type": product_type, "total": total})
        else:
            local = threading.local()
            clients: list[PickAICatalog] = []
            clients_lock = threading.Lock()

            def client() -> PickAICatalog:
                catalog = getattr(local, "catalog", None)
                if catalog is None:
                    catalog = PickAICatalog(
                        base_url=self.base_url,
                        retries=self.retries,
                        retry_delay_s=self.retry_delay_s,
                    )
                    local.catalog = catalog
                    with clients_lock:
                        clients.append(catalog)
                return catalog

            def load_type(product_type: dict[str, Any]):
                catalog = client()
                before = catalog.request_count
                items, total, pages = catalog._quotes_for_type(product_type, progress)
                return (
                    int(product_type["id"]),
                    items,
                    total,
                    pages,
                    catalog.request_count - before,
                    product_type,
                )

            try:
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    futures = [pool.submit(load_type, product_type) for product_type in product_types]
                    for future in as_completed(futures):
                        type_id, items, total, pages, requests, product_type = future.result()
                        type_results[type_id] = (items, total, pages, requests)
                        if progress is not None:
                            progress({"event": "type", "product_type": product_type, "total": total})
            finally:
                for catalog in clients:
                    self.request_count += catalog.request_count
                    catalog.close()

        quotes_by_key: dict[str, dict[str, Any]] = {}
        enriched_types: list[dict[str, Any]] = []
        declared_quotes = 0
        received_quotes = 0
        for product_type in product_types:
            type_id = int(product_type["id"])
            if type_id not in type_results:
                raise RuntimeError(f"PickAI 标准商品未完成抓取：{type_id}")
            items, total, pages, _requests = type_results[type_id]
            declared_quotes += total
            received_quotes += len(items)
            enriched_types.append({**product_type, "quote_count": total, "pages": pages})
            for item in items:
                key = _quote_key(item)
                existing = quotes_by_key.get(key)
                if existing is None:
                    quotes_by_key[key] = item
                else:
                    self._merge_quote(existing, item)

        quotes = sorted(
            quotes_by_key.values(),
            key=lambda item: (
                min(item.get("product_type_ids") or [10**9]),
                to_float(item.get("price"), 10**12),
                str(item.get("item_url") or ""),
            ),
        )
        return PickAISnapshot(
            fetched_at=_utc_iso(),
            categories=categories,
            product_types=enriched_types,
            quotes=quotes,
            relay_providers=relay,
            declared_quotes=declared_quotes,
            duplicate_quotes=max(0, received_quotes - len(quotes)),
            request_count=self.request_count,
        )


def export_snapshot(
    snapshot: PickAISnapshot,
    json_path: Path,
    csv_path: Path,
) -> None:
    """原子写入 JSON 全量快照和便于表格处理的 UTF-8 CSV。"""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_tmp = json_path.with_suffix(json_path.suffix + ".tmp")
    json_tmp.write_text(
        json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    json_tmp.replace(json_path)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    fields = [
        "id",
        "shop_name",
        "raw_name",
        "price",
        "stock",
        "item_url",
        "updated_at",
        "product_type_ids",
        "product_type_names",
        "catalog_categories",
        "shop_avatar_url",
    ]
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for quote in snapshot.quotes:
            row = dict(quote)
            for field in ("product_type_ids", "product_type_names", "catalog_categories"):
                row[field] = " | ".join(str(value) for value in quote.get(field) or [])
            writer.writerow(row)
    csv_tmp.replace(csv_path)


def load_snapshot(json_path: Path) -> PickAISnapshot:
    """读取 ``export_snapshot`` 生成的完整 JSON，供离线重新入库。"""
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("PickAI 快照根节点格式异常")
    summary = payload.get("summary") or {}
    categories = payload.get("categories")
    product_types = payload.get("product_types")
    quotes = payload.get("quotes")
    relay_providers = payload.get("relay_providers")
    if not all(isinstance(value, list) for value in (categories, product_types, quotes)):
        raise RuntimeError("PickAI 快照缺少分类、标准商品或报价数组")
    if not isinstance(relay_providers, dict):
        raise RuntimeError("PickAI 快照缺少中转 API 数据")
    return PickAISnapshot(
        fetched_at=str(payload.get("fetched_at") or ""),
        categories=categories,
        product_types=product_types,
        quotes=quotes,
        relay_providers=relay_providers,
        declared_quotes=int(summary.get("declared_quotes") or len(quotes)),
        duplicate_quotes=int(summary.get("duplicates_merged") or 0),
        request_count=int(summary.get("requests") or 0),
    )
