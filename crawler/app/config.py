"""运行配置。所有可调项集中在这里，桌面端可通过环境变量覆盖。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _data_dir() -> Path:
    """开发时沿用项目数据；打包后写入用户目录，避免安装目录无写权限。"""
    override = os.getenv("BIPAI_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if getattr(sys, "frozen", False):
        if os.name == "nt":
            root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return root / "Bipai"
        return Path.home() / ".local" / "share" / "bipai"

    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

_ROOT = Path(__file__).resolve().parent.parent

# 凭据落地文件（data/ 已被 .gitignore 忽略，不会进版本库）
COOKIE_FILE = DATA_DIR / "cookie.txt"
TOKEN_FILE = DATA_DIR / "merchant_token.txt"
CATFK_TOKEN_FILE = DATA_DIR / "catfk_merchant_token.txt"
PUBLIC_CLEARANCE_FILE = DATA_DIR / "public_clearance_cookie.txt"
PUBLIC_CLEARANCE_UA_FILE = DATA_DIR / "public_clearance_user_agent.txt"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRICEAI_",
        env_file=str(_ROOT / ".env"),
        extra="ignore",
    )

    # 服务
    host: str = "127.0.0.1"
    port: int = 8756

    # 数据库
    db_path: Path = DATA_DIR / "priceai.db"

    # 抓取目标
    base_url: str = "https://www.ldxp.cn"
    catfk_base_url: str = "https://catfk.com"
    pickai_base_url: str = "https://pickai.cc"
    # curl_cffi 伪装的浏览器指纹，用于绕过 ESA 的 TLS 指纹识别
    impersonate: str = "chrome"

    # 商家鉴权接口全进程串行，并在相邻请求间保留较长随机间隔。
    # 该间隔优先保护账号，不再追求短时间内遍历完整 GoodsPool。
    min_delay_ms: int = 3000
    max_delay_ms: int = 6000
    # 公开零售接口是匿名访客流量，无需账号级保护间隔。同主机的整体节奏由
    # 全局 HostThrottle 统一把关，这里不再叠加每会话间隔，避免重复等待。
    public_min_delay_ms: int = 0
    public_max_delay_ms: int = 0
    max_concurrency: int = 3
    retail_index_concurrency: int = 2
    # PickAI 公开目录全量快照约百余个分页。少量并发只重叠响应等待，
    # 同主机请求起始节奏仍受 HostThrottle 全局限速。
    pickai_workers: int = 3
    pickai_refresh_minutes: int = 30
    # 匿名零售接口同样可能被平台按出口 IP 关联；全局请求不再高频突发。
    host_min_delay_ms: int = 900
    host_max_delay_ms: int = 1600
    # 匿名公开店铺遇到 HTML 滑块时，只做短暂、固定的本地保护。旧版沿用
    # 商家接口的 180～1800 秒指数退避，一次滑块就把所有公开店铺误锁三分钟。
    public_waf_cooldown_s: int = 15
    public_waf_max_cooldown_s: int = 15
    waf_cooldown_s: int = 180
    waf_max_cooldown_s: int = 1800
    request_timeout_s: int = 25
    # 账号接口不应在 401/403 后自动重试；瞬时网络错误也只发起一次请求。
    max_retries: int = 1

    # 登录态：从浏览器导出的 Cookie 串，形如 "k1=v1; k2=v2"（可选）
    cookie: str = ""
    # 真正的鉴权令牌：浏览器 localStorage 里 auth-token 的 value，请求头 Merchant-Token
    merchant_token: str = ""
    catfk_merchant_token: str = ""
    # pay.ldxp.cn 的匿名 ESA 真人验证通行 Cookie。它来自应用打开的隔离
    # 浏览器配置目录，不含商家登录态，并且只会发给 pay.ldxp.cn。
    public_clearance_cookie: str = ""
    public_clearance_user_agent: str = ""
    # 仅当前桌面进程有效：真人验证后保留的隔离浏览器 CDP 端口。公开原店
    # 请求优先在这个真实浏览器上下文执行，避免 Cookie/指纹绑定导致二次滑块。
    public_browser_debug_port: int = 0

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    def set_cookie(self, cookie: str) -> None:
        """更新并持久化 Cookie，重启后仍在。"""
        self.cookie = cookie.strip()
        if self.cookie:
            COOKIE_FILE.write_text(self.cookie, encoding="utf-8")
        elif COOKIE_FILE.exists():
            COOKIE_FILE.unlink()

    def set_token(self, token: str) -> None:
        """更新并持久化 Merchant-Token。"""
        self.merchant_token = token.strip()
        if self.merchant_token:
            TOKEN_FILE.write_text(self.merchant_token, encoding="utf-8")
        elif TOKEN_FILE.exists():
            TOKEN_FILE.unlink()

    def set_catfk_token(self, token: str) -> None:
        """更新并持久化云猫寄售 Merchant-Token。"""
        self.catfk_merchant_token = token.strip()
        if self.catfk_merchant_token:
            CATFK_TOKEN_FILE.write_text(self.catfk_merchant_token, encoding="utf-8")
        elif CATFK_TOKEN_FILE.exists():
            CATFK_TOKEN_FILE.unlink()

    def set_public_clearance(self, cookie: str, user_agent: str = "") -> None:
        """保存匿名源站真人验证状态；绝不与商家 Cookie 混用。"""
        self.public_clearance_cookie = cookie.strip()
        self.public_clearance_user_agent = user_agent.strip()
        if self.public_clearance_cookie:
            PUBLIC_CLEARANCE_FILE.write_text(
                self.public_clearance_cookie,
                encoding="utf-8",
            )
        elif PUBLIC_CLEARANCE_FILE.exists():
            PUBLIC_CLEARANCE_FILE.unlink()
        if self.public_clearance_user_agent:
            PUBLIC_CLEARANCE_UA_FILE.write_text(
                self.public_clearance_user_agent,
                encoding="utf-8",
            )
        elif PUBLIC_CLEARANCE_UA_FILE.exists():
            PUBLIC_CLEARANCE_UA_FILE.unlink()


settings = Settings()

# 启动时若环境变量没给凭据，则尝试从落地文件读取
if not settings.cookie and COOKIE_FILE.exists():
    settings.cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
if not settings.merchant_token and TOKEN_FILE.exists():
    settings.merchant_token = TOKEN_FILE.read_text(encoding="utf-8").strip()
if not settings.catfk_merchant_token and CATFK_TOKEN_FILE.exists():
    settings.catfk_merchant_token = CATFK_TOKEN_FILE.read_text(encoding="utf-8").strip()
if not settings.public_clearance_cookie and PUBLIC_CLEARANCE_FILE.exists():
    settings.public_clearance_cookie = PUBLIC_CLEARANCE_FILE.read_text(encoding="utf-8").strip()
if not settings.public_clearance_user_agent and PUBLIC_CLEARANCE_UA_FILE.exists():
    settings.public_clearance_user_agent = PUBLIC_CLEARANCE_UA_FILE.read_text(
        encoding="utf-8"
    ).strip()
