"""基于 curl_cffi 的会话：伪装浏览器指纹 + 限速 + 重试。

链动小铺挂在阿里云 ESA 后面，用 TLS/JA3、HTTP2 指纹识别爬虫，普通
requests/httpx 会直接吃 403。curl_cffi 用 impersonate 复刻真实浏览器
的握手指纹，这是能过掉那道 403 的关键。
"""
from __future__ import annotations

import http.client
import json
import random
import re
import threading
import time
from typing import Any, Literal, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from curl_cffi import requests as cffi
from curl_cffi.const import CurlOpt
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings


class BlockedError(RuntimeError):
    """被反爬拦截（403 / 验证页）时抛出。"""


class JsonChallengeError(BlockedError):
    """接口临时返回 HTML 验证页，可安全退避重试。"""


_ACW_HOSTS = {"pay.ldxp.cn", "www.ldxp.cn"}
_PUBLIC_CLEARANCE_HOST = "pay.ldxp.cn"
_ACW_ARG_RE = re.compile(r"\bvar\s+arg1\s*=\s*['\"]([0-9a-fA-F]{40})['\"]")
_ACW_PERMUTATION = (
    15, 35, 29, 24, 33, 16, 1, 38, 10, 9,
    19, 31, 40, 27, 22, 23, 25, 13, 6, 11,
    39, 18, 20, 8, 14, 21, 32, 26, 2, 30,
    7, 4, 17, 5, 3, 28, 34, 37, 12, 36,
)
_ACW_XOR_KEY = "3000176000856006061501533003690027800375"
CredentialPolicy = Literal["merchant", "public"]


class BrowserBridgeResponse:
    """curl_cffi Response 的最小兼容层，数据实际来自已验证浏览器。"""

    def __init__(self, status: int, text: str, headers: dict[str, str]) -> None:
        self.status_code = status
        self.text = text
        self.headers = headers

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"浏览器源站请求失败({self.status_code})")


def _public_browser_page(port: int) -> str:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1.2)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    finally:
        connection.close()
    for target in payload if isinstance(payload, list) else []:
        url = str(target.get("url") or "")
        host = (urlsplit(url).hostname or "").lower()
        websocket = str(target.get("webSocketDebuggerUrl") or "")
        if target.get("type") == "page" and host == _PUBLIC_CLEARANCE_HOST and websocket:
            return websocket
    raise RuntimeError("真人验证浏览器页面已经关闭。")


def _public_browser_request(
    port: int,
    method: str,
    url: str,
    headers: dict[str, str],
    kwargs: dict[str, Any],
    timeout_s: float,
) -> BrowserBridgeResponse:
    """在用户刚完成真人验证的同一浏览器上下文执行公开 API。"""
    from websockets.sync.client import connect

    params = kwargs.get("params")
    if params:
        parsed = urlsplit(url)
        extra = urlencode(params, doseq=True)
        query = f"{parsed.query}&{extra}" if parsed.query else extra
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    request_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() in {"accept", "content-type", "x-requested-with"}
    }
    body: str | None = None
    if "json" in kwargs:
        body = json.dumps(kwargs["json"], ensure_ascii=False, separators=(",", ":"))
        request_headers.setdefault("Content-Type", "application/json")
    elif kwargs.get("data") is not None:
        raw = kwargs["data"]
        body = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    specification = {
        "url": url,
        "method": method.upper(),
        "headers": request_headers,
        "body": body,
    }
    expression = f"""
    (async () => {{
      const spec = {json.dumps(specification, ensure_ascii=False)};
      const options = {{
        method: spec.method,
        headers: spec.headers,
        credentials: 'include',
        cache: 'no-store',
        redirect: 'follow'
      }};
      if (spec.body !== null) options.body = spec.body;
      const response = await fetch(spec.url, options);
      const responseHeaders = {{}};
      response.headers.forEach((value, key) => {{ responseHeaders[key] = value; }});
      return {{
        status: response.status,
        text: await response.text(),
        headers: responseHeaders
      }};
    }})()
    """
    websocket_url = _public_browser_page(port)
    deadline = time.monotonic() + max(1.0, timeout_s)
    with connect(
        websocket_url,
        origin=None,
        open_timeout=min(2.0, timeout_s),
        close_timeout=0.2,
        max_size=8 * 1024 * 1024,
    ) as socket:
        socket.send(
            json.dumps(
                {
                    "id": 8401,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("真人验证浏览器请求超时。")
            message = json.loads(socket.recv(timeout=remaining))
            if message.get("id") != 8401:
                continue
            if message.get("error") or message.get("result", {}).get("exceptionDetails"):
                raise RuntimeError("真人验证浏览器执行公开请求失败。")
            value = (
                message.get("result", {})
                .get("result", {})
                .get("value", {})
            )
            if not isinstance(value, dict):
                raise RuntimeError("真人验证浏览器返回了无效结果。")
            return BrowserBridgeResponse(
                int(value.get("status") or 0),
                str(value.get("text") or ""),
                {
                    str(key).lower(): str(header_value)
                    for key, header_value in (value.get("headers") or {}).items()
                },
            )


def solve_acw_challenge(html: str) -> Optional[str]:
    """计算阿里云 ACW 挑战页要求的 acw_sc__v2 Cookie。"""
    match = _ACW_ARG_RE.search(html or "")
    if match is None or "acw_sc__v2" not in html:
        return None

    arg = match.group(1)
    reordered = "".join(arg[index - 1] for index in _ACW_PERMUTATION)
    return "".join(
        f"{int(reordered[index:index + 2], 16) ^ int(_ACW_XOR_KEY[index:index + 2], 16):02x}"
        for index in range(0, 40, 2)
    )


class Throttle:
    """进程内限速器：请求之间加随机停顿，避免节奏太整齐被风控盯上。"""

    def __init__(self, min_ms: int, max_ms: int) -> None:
        self._min = min_ms / 1000
        self._max = max_ms / 1000
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = random.uniform(self._min, self._max)
            elapsed = time.monotonic() - self._last
            if elapsed < gap:
                time.sleep(gap - elapsed)
            self._last = time.monotonic()


class HostThrottle:
    """跨会话按主机平滑请求，防止多个索引 worker 同时形成突发流量。"""

    def __init__(self, min_ms: int, max_ms: int) -> None:
        self._min = min_ms / 1000
        self._max = max_ms / 1000
        self._lock = threading.Lock()
        self._last_by_host: dict[str, float] = {}
        self._blocked_until_by_host: dict[str, float] = {}
        self._penalty_count_by_host: dict[str, int] = {}

    def wait(
        self,
        url: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("实时核验已被新的搜索替换。")
            with self._lock:
                now = time.monotonic()
                if deadline_monotonic is not None and now >= deadline_monotonic:
                    raise TimeoutError("实时核验已达到墙钟预算。")
                cooldown = self._blocked_until_by_host.get(host, 0.0) - now
                gap = random.uniform(self._min, self._max)
                spacing = gap - (now - self._last_by_host.get(host, 0.0))
                delay = max(cooldown, spacing, 0.0)
                if delay <= 0:
                    self._last_by_host[host] = now
                    return
            if deadline_monotonic is not None:
                delay = min(delay, max(0.0, deadline_monotonic - time.monotonic()))
            # 不能一次 sleep 完全部节流时间：用户切换分类时，旧请求必须能在
            # 100ms 内停止排队，不能继续占住下一次实时搜索的请求额度。
            delay = min(delay, 0.1)
            if delay <= 0:
                raise TimeoutError("实时核验已达到墙钟预算。")
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise TimeoutError("实时核验已被新的搜索替换。")
            else:
                time.sleep(delay)

    def penalize(
        self,
        url: str,
        seconds: float,
        max_seconds: float,
    ) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return
        with self._lock:
            now = time.monotonic()
            if self._blocked_until_by_host.get(host, 0.0) > now:
                return
            penalty_count = self._penalty_count_by_host.get(host, 0) + 1
            self._penalty_count_by_host[host] = penalty_count
            cooldown = min(
                max(0.0, max_seconds),
                max(0.0, seconds) * (2 ** min(penalty_count - 1, 10)),
            )
            self._blocked_until_by_host[host] = now + cooldown

    def success(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return
        with self._lock:
            if self._blocked_until_by_host.get(host, 0.0) <= time.monotonic():
                self._blocked_until_by_host.pop(host, None)
                self._penalty_count_by_host.pop(host, None)

    def cooldown_remaining(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return 0.0
        with self._lock:
            return max(
                0.0,
                self._blocked_until_by_host.get(host, 0.0) - time.monotonic(),
            )

    def clear(self, url: str) -> None:
        """真人验证完成后立即解除该主机的本地短保护。"""
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return
        with self._lock:
            self._blocked_until_by_host.pop(host, None)
            self._penalty_count_by_host.pop(host, None)


_HOST_THROTTLE = HostThrottle(
    settings.host_min_delay_ms,
    settings.host_max_delay_ms,
)
_MERCHANT_THROTTLE = HostThrottle(
    settings.min_delay_ms,
    settings.max_delay_ms,
)


def host_cooldown_remaining(url: str) -> float:
    return _HOST_THROTTLE.cooldown_remaining(url)


def clear_host_cooldown(url: str) -> None:
    _HOST_THROTTLE.clear(url)


class Fetcher:
    def __init__(
        self,
        cookie: Optional[str] = None,
        *,
        credential_policy: CredentialPolicy = "merchant",
        base_url: Optional[str] = None,
        merchant_token: Optional[str] = None,
        merchant_referer: Optional[str] = None,
        timeout_s: float | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if credential_policy not in {"merchant", "public"}:
            raise ValueError(f"未知凭据策略：{credential_policy}")
        self.credential_policy = credential_policy
        uses_default_site = base_url is None
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.cookie = (
            cookie
            if cookie is not None
            else settings.cookie if uses_default_site else ""
        )
        self.merchant_token = (
            merchant_token
            if merchant_token is not None
            else settings.merchant_token if uses_default_site else ""
        )
        self.merchant_referer = merchant_referer or f"{self.base_url}/package/merchant/"
        self.timeout_s = (
            max(1.0, float(timeout_s))
            if timeout_s is not None
            else float(settings.request_timeout_s)
        )
        self.deadline_monotonic = deadline_monotonic
        self.cancel_event = cancel_event
        # 商家鉴权请求由全进程共享的 _MERCHANT_THROTTLE 串行化，避免
        # 搜索、连接验证和索引使用各自 Fetcher 时叠加成突发流量。
        if credential_policy == "public":
            self.throttle = Throttle(
                settings.public_min_delay_ms,
                settings.public_max_delay_ms,
            )
        else:
            self.throttle = Throttle(0, 0)
        self.host_throttle = _HOST_THROTTLE
        self.merchant_host_throttle = _MERCHANT_THROTTLE
        # 官方货源、公开店铺和参考目录都可在国内直连。显式清空
        # CURLOPT_PROXY，避免用户级 HTTP(S)_PROXY 指向已关闭的本地代理时，
        # libcurl 仍把全部搜索请求发往该端口。curl_cffi 0.15 中仅设置
        # trust_env=False 不足以阻止 libcurl 读取这些环境变量。
        self.session = cffi.Session(
            impersonate=settings.impersonate,
            curl_options={CurlOpt.PROXY: ""},
        )

    def _may_send_merchant_credentials(self, url: str) -> bool:
        if getattr(self, "credential_policy", "merchant") != "merchant":
            return False
        try:
            target_host = (urlsplit(url).hostname or "").lower()
            base_url = getattr(self, "base_url", settings.base_url)
            merchant_host = (urlsplit(base_url).hostname or "").lower()
        except ValueError:
            return False
        return bool(target_host and target_host == merchant_host)

    def _waf_cooldown_limits(self) -> tuple[float, float]:
        """按请求性质选择保护时间，公开滑块不能锁死全部搜索三分钟。"""
        if getattr(self, "credential_policy", "merchant") == "public":
            return (
                float(settings.public_waf_cooldown_s),
                float(settings.public_waf_max_cooldown_s),
            )
        return (
            float(settings.waf_cooldown_s),
            float(settings.waf_max_cooldown_s),
        )

    def _headers(self, url: str) -> dict[str, str]:
        base_url = getattr(self, "base_url", settings.base_url)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": base_url,
            "Referer": getattr(
                self,
                "merchant_referer",
                f"{base_url}/package/merchant/",
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
        # 真实鉴权：请求头 Merchant-Token（来自浏览器 localStorage 的 auth-token）
        merchant_token = getattr(self, "merchant_token", settings.merchant_token)
        if self._may_send_merchant_credentials(url) and merchant_token:
            headers["Merchant-Token"] = merchant_token
        if self._may_send_merchant_credentials(url) and self.cookie:
            headers["Cookie"] = self.cookie
        # 匿名真人验证 Cookie 只允许发往公开零售域名。不能把它与商家
        # Cookie/Token 混用，也不能泄露给 PickAI、云猫或其他候选站点。
        target_host = (urlsplit(url).hostname or "").lower()
        if (
            getattr(self, "credential_policy", "merchant") == "public"
            and target_host == _PUBLIC_CLEARANCE_HOST
            and settings.public_clearance_cookie
        ):
            headers["Cookie"] = settings.public_clearance_cookie
            if settings.public_clearance_user_agent:
                headers["User-Agent"] = settings.public_clearance_user_agent
        return headers

    def _wait_for_request(self, url: str) -> None:
        self._request_timeout()
        self.throttle.wait()
        deadline = getattr(self, "deadline_monotonic", None)
        cancel_event = getattr(self, "cancel_event", None)
        if self._may_send_merchant_credentials(url):
            merchant_throttle = getattr(self, "merchant_host_throttle", None)
            if merchant_throttle is not None:
                merchant_throttle.wait(
                    url,
                    deadline_monotonic=deadline,
                    cancel_event=cancel_event,
                )
        host_throttle = getattr(self, "host_throttle", None)
        if host_throttle is not None:
            cooldown = host_throttle.cooldown_remaining(url)
            if cooldown > 0:
                raise BlockedError(
                    f"站点保护冷却中，请约 {max(1, int(cooldown))} 秒后再手动重试。"
                )
            host_throttle.wait(
                url,
                deadline_monotonic=deadline,
                cancel_event=cancel_event,
            )
        self._request_timeout()

    def _request_timeout(self) -> float:
        """返回本次请求剩余超时，保证一组联网操作服从同一墙钟截止时间。"""
        cancel_event = getattr(self, "cancel_event", None)
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("实时核验已被新的搜索替换。")
        timeout_s = float(getattr(self, "timeout_s", settings.request_timeout_s))
        deadline = getattr(self, "deadline_monotonic", None)
        if deadline is None:
            return timeout_s
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("实时核验已达到墙钟预算。")
        return max(0.1, min(timeout_s, remaining))

    @retry(
        # 401/403/站点拦截绝不能自动重试，否则失效 Token 会被连续提交。
        retry=retry_if_exception_type(cffi.RequestsError),
        stop=stop_after_attempt(settings.max_retries),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs: Any):
        self._wait_for_request(url)
        host_throttle = getattr(self, "host_throttle", None)
        headers = {**self._headers(url), **kwargs.pop("headers", {})}
        if not self._may_send_merchant_credentials(url):
            expected_public_cookie = settings.public_clearance_cookie
            public_cookie = (
                expected_public_cookie
                if expected_public_cookie
                and headers.get("Cookie") == expected_public_cookie
                and getattr(self, "credential_policy", "merchant") == "public"
                and (urlsplit(url).hostname or "").lower() == _PUBLIC_CLEARANCE_HOST
                else None
            )
            headers = {
                key: value
                for key, value in headers.items()
                if key.lower() not in {"merchant-token", "cookie"}
            }
            if public_cookie:
                headers["Cookie"] = public_cookie
        request_timeout = self._request_timeout()
        resp = None
        if (
            getattr(self, "credential_policy", "merchant") == "public"
            and (urlsplit(url).hostname or "").lower() == _PUBLIC_CLEARANCE_HOST
            and settings.public_browser_debug_port
        ):
            try:
                resp = _public_browser_request(
                    settings.public_browser_debug_port,
                    method,
                    url,
                    headers,
                    kwargs,
                    request_timeout,
                )
            except Exception:  # noqa: BLE001 - 浏览器关闭时无缝退回 Cookie 会话
                settings.public_browser_debug_port = 0
        if resp is None:
            resp = self.session.request(
                method,
                url,
                headers=headers,
                timeout=request_timeout,
                **kwargs,
            )
        host = (urlsplit(url).hostname or "").lower()
        if host in _ACW_HOSTS:
            cookie = solve_acw_challenge(resp.text[:10000])
            if cookie:
                self.session.cookies.set("acw_sc__v2", cookie, domain=host, path="/")
                self._wait_for_request(url)
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self._request_timeout(),
                    **kwargs,
                )
                if solve_acw_challenge(resp.text[:10000]):
                    raise BlockedError("店铺站点的浏览器校验未通过，请稍后重试。")
        # 401：多为登录态/令牌失效；403：ESA 反爬拦截
        if resp.status_code == 403 or "http_bot_simple" in resp.text[:400]:
            if host_throttle is not None:
                cooldown_s, max_cooldown_s = self._waf_cooldown_limits()
                host_throttle.penalize(
                    url,
                    cooldown_s,
                    max_cooldown_s,
                )
            raise BlockedError(f"被反爬拦截({resp.status_code})：触发了 ESA 防护。")
        if resp.status_code == 401:
            if self._may_send_merchant_credentials(url):
                raise BlockedError("未登录(401)：Merchant-Token 缺失或已失效，请在设置里重新填。")
            raise BlockedError("公开店铺接口拒绝访问(401)，请稍后重试。")
        resp.raise_for_status()
        return resp

    def get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        base_url = getattr(self, "base_url", settings.base_url)
        url = path if path.startswith("http") else f"{base_url}{path}"
        return self._request("GET", url, params=params).json()

    @retry(
        retry=retry_if_exception_type(JsonChallengeError),
        stop=stop_after_attempt(settings.max_retries),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        reraise=True,
    )
    def post_json(
        self,
        path: str,
        body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        """POST JSON 并返回解析后的响应体（含 {code, msg, data} 信封）。"""
        base_url = getattr(self, "base_url", settings.base_url)
        url = path if path.startswith("http") else f"{base_url}{path}"
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        resp = self._request("POST", url, headers=headers, json=body or {})
        try:
            payload = resp.json()
            host_throttle = getattr(self, "host_throttle", None)
            if host_throttle is not None:
                host_throttle.success(url)
            return payload
        except ValueError as exc:
            content_type = (resp.headers.get("content-type") or "").lower()
            if "html" in content_type or (resp.text or "").lstrip().startswith("<"):
                host_throttle = getattr(self, "host_throttle", None)
                if host_throttle is not None:
                    cooldown_s, max_cooldown_s = self._waf_cooldown_limits()
                    host_throttle.penalize(
                        url,
                        cooldown_s,
                        max_cooldown_s,
                    )
                raise JsonChallengeError(
                    "接口返回了网页验证而不是 JSON，请稍后重试。"
                ) from exc
            raise RuntimeError("接口返回了无法解析的 JSON 数据。") from exc

    def get_html(self, path: str, params: Optional[dict[str, Any]] = None) -> str:
        base_url = getattr(self, "base_url", settings.base_url)
        url = path if path.startswith("http") else f"{base_url}{path}"
        return self._request("GET", url, params=params).text

    def probe(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """探测请求：返回状态码、内容类型、原始文本片段，用于确认真实结构。

        不抛拦截异常，把结果如实带回来，方便前端和开发对照。
        """
        base_url = getattr(self, "base_url", settings.base_url)
        url = path if path.startswith("http") else f"{base_url}{path}"
        self._wait_for_request(url)
        resp = self.session.request(
            "GET",
            url,
            headers=self._headers(url),
            timeout=self._request_timeout(),
            params=params,
        )
        text = resp.text or ""
        blocked = resp.status_code in (401, 403) or "http_bot_simple" in text[:400]
        return {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "blocked": blocked,
            "length": len(text),
            "text": text,
        }

    def close(self) -> None:
        self.session.close()
