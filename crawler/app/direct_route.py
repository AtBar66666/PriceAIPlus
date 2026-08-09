"""Windows 物理网卡直连通道。

系统代理可以用 ``CURLOPT_PROXY=\"\"`` 关闭，但 TUN/全局 VPN 仍会接管默认
路由和 Fake-IP DNS。链动、PickAI、云猫都能从国内网络直接访问；这里为这些
域名解析真实地址，再把出站 socket 绑定到有真实网关的物理网卡。这样不需要
关闭或重载用户的代理客户端，也不会影响其他应用继续走代理。
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import select
import socket
import socketserver
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


DIRECT_HOST_SUFFIXES = (
    "ldxp.cn",
    "pickai.cc",
    "catfk.com",
    "alicdn.com",
    "aliyun.com",
    "aliyuncs.com",
    "wdnmd.wang",
)
_VIRTUAL_MARKERS = (
    "tun",
    "tap",
    "vpn",
    "wintun",
    "wireguard",
    "clash",
    "fcclient",
    "sing-box",
    "v2ray",
    "xray",
    "zerotier",
    "tailscale",
)
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class PhysicalRoute:
    interface_alias: str
    interface_index: int
    interface_ip: str
    gateway: str
    dns_servers: tuple[str, ...]


@dataclass(frozen=True)
class DirectTarget:
    route: PhysicalRoute
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def curl_resolve_entry(self) -> str:
        return f"{self.host}:{self.port}:{','.join(self.addresses)}"


def is_direct_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in DIRECT_HOST_SUFFIXES)


def _usable_ipv4(value: str, *, allow_private: bool = True) -> bool:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    if address.version != 4 or address.is_loopback or address.is_link_local or address.is_unspecified:
        return False
    if address in _FAKE_IP_NETWORK:
        return False
    return allow_private or not address.is_private


def _powershell_json(script: str, timeout_s: float = 5.0):
    if os.name != "nt":
        return None
    prefix = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        "$OutputEncoding=[Console]::OutputEncoding;"
        "$ErrorActionPreference='Stop';"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            prefix + script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return json.loads(completed.stdout.lstrip("\ufeff").strip())
    except json.JSONDecodeError:
        return None


def _discover_windows_route() -> PhysicalRoute | None:
    payload = _powershell_json(
        "$rows=@(Get-NetIPConfiguration | "
        "Where-Object {$_.NetAdapter.Status -eq 'Up' -and $_.IPv4Address -and $_.IPv4DefaultGateway} | "
        "ForEach-Object { [pscustomobject]@{"
        "alias=[string]$_.InterfaceAlias;"
        "description=[string]$_.NetAdapter.InterfaceDescription;"
        "index=[int]$_.InterfaceIndex;"
        "addresses=@($_.IPv4Address | ForEach-Object {[string]$_.IPAddress});"
        "gateways=@($_.IPv4DefaultGateway | ForEach-Object {[string]$_.NextHop});"
        "dns=@($_.DNSServer.ServerAddresses | Where-Object {$_ -match '^\\d+\\.'} | ForEach-Object {[string]$_})"
        "} }); $rows | ConvertTo-Json -Compress -Depth 4",
    )
    if not payload:
        return None
    rows = payload if isinstance(payload, list) else [payload]
    candidates: list[tuple[int, PhysicalRoute]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        alias = str(row.get("alias") or "")
        description = str(row.get("description") or "")
        label = f"{alias} {description}".lower()
        if any(marker in label for marker in _VIRTUAL_MARKERS):
            continue
        addresses = row.get("addresses") or []
        gateways = row.get("gateways") or []
        dns_servers = row.get("dns") or []
        if isinstance(addresses, str):
            addresses = [addresses]
        if isinstance(gateways, str):
            gateways = [gateways]
        if isinstance(dns_servers, str):
            dns_servers = [dns_servers]
        interface_ip = next((str(item) for item in addresses if _usable_ipv4(str(item))), "")
        gateway = next((str(item) for item in gateways if _usable_ipv4(str(item))), "")
        if not interface_ip or not gateway:
            continue
        clean_dns = tuple(
            dict.fromkeys(str(item) for item in dns_servers if _usable_ipv4(str(item)))
        )
        if not clean_dns:
            clean_dns = (gateway,)
        try:
            index = int(row.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        # 有 RFC1918 地址和本地网关的普通以太网/Wi-Fi 优先。
        score = 10
        if ipaddress.ip_address(interface_ip).is_private:
            score += 5
        if any(word in label for word in ("ethernet", "wi-fi", "wireless", "以太网", "无线")):
            score += 3
        candidates.append(
            (
                score,
                PhysicalRoute(alias, index, interface_ip, gateway, clean_dns),
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


_route_lock = threading.Lock()
_route_cache: PhysicalRoute | None = None
_route_expires_at = 0.0


def get_physical_route(*, force: bool = False) -> PhysicalRoute | None:
    global _route_cache, _route_expires_at
    if os.name != "nt" or os.getenv("PRICEAI_PHYSICAL_DIRECT", "1").strip().lower() in {"0", "false", "off"}:
        return None
    now = time.monotonic()
    with _route_lock:
        if not force and _route_cache is not None and now < _route_expires_at:
            return _route_cache
        _route_cache = _discover_windows_route()
        _route_expires_at = now + (120.0 if _route_cache else 15.0)
        return _route_cache


def _encode_dns_name(host: str) -> bytes:
    labels = host.rstrip(".").split(".")
    return b"".join(bytes((len(label.encode("idna")),)) + label.encode("idna") for label in labels) + b"\0"


def _read_dns_name(payload: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    return_offset = offset
    jumped = False
    seen: set[int] = set()
    while offset < len(payload):
        if offset in seen:
            raise ValueError("DNS compression loop")
        seen.add(offset)
        length = payload[offset]
        if length == 0:
            offset += 1
            if not jumped:
                return_offset = offset
            return ".".join(labels), return_offset
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(payload):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | payload[offset + 1]
            if not jumped:
                return_offset = offset + 2
                jumped = True
            offset = pointer
            continue
        offset += 1
        end = offset + length
        if end > len(payload):
            raise ValueError("truncated DNS label")
        labels.append(payload[offset:end].decode("ascii", errors="ignore"))
        offset = end
    raise ValueError("unterminated DNS name")


def _bind_physical(sock: socket.socket, route: PhysicalRoute) -> None:
    sock.bind((route.interface_ip, 0))
    # Windows 的 IP_UNICAST_IF=31。绑定源地址通常已经足够；额外指定接口可
    # 防止存在两个相同前缀路由时又被虚拟网卡抢走。
    if os.name == "nt" and route.interface_index > 0:
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                31,
                struct.pack("I", socket.htonl(route.interface_index)),
            )
        except OSError:
            pass


def _query_dns_a(host: str, dns_server: str, route: PhysicalRoute) -> tuple[str, ...]:
    query_id = random.randint(1, 0xFFFF)
    question = _encode_dns_name(host) + struct.pack("!HH", 1, 1)
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.8)
        _bind_physical(sock, route)
        sock.sendto(packet, (dns_server, 53))
        payload, _ = sock.recvfrom(8192)
    finally:
        sock.close()
    if len(payload) < 12:
        return ()
    response_id, flags, question_count, answer_count, _, _ = struct.unpack("!HHHHHH", payload[:12])
    if response_id != query_id or flags & 0x000F:
        return ()
    offset = 12
    for _ in range(question_count):
        _, offset = _read_dns_name(payload, offset)
        offset += 4
    addresses: list[str] = []
    for _ in range(answer_count):
        _, offset = _read_dns_name(payload, offset)
        if offset + 10 > len(payload):
            break
        record_type, record_class, _, data_length = struct.unpack("!HHIH", payload[offset:offset + 10])
        offset += 10
        data = payload[offset:offset + data_length]
        offset += data_length
        if record_type == 1 and record_class == 1 and len(data) == 4:
            address = socket.inet_ntoa(data)
            if _usable_ipv4(address, allow_private=False):
                addresses.append(address)
    return tuple(dict.fromkeys(addresses))


_dns_lock = threading.Lock()
_dns_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}


def resolve_physical_ipv4(host: str, route: PhysicalRoute | None = None) -> tuple[str, ...]:
    host = host.strip().lower().rstrip(".")
    route = route or get_physical_route()
    if not route or not host:
        return ()
    key = (route.interface_ip, host)
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    addresses: tuple[str, ...] = ()
    for dns_server in (*route.dns_servers, route.gateway):
        if not _usable_ipv4(dns_server):
            continue
        try:
            addresses = _query_dns_a(host, dns_server, route)
        except OSError:
            continue
        if addresses:
            break
    if addresses:
        with _dns_lock:
            _dns_cache[key] = (now + 120.0, addresses)
    return addresses


def direct_target_for_url(url: str) -> DirectTarget | None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if not is_direct_host(host):
        return None
    route = get_physical_route()
    if route is None:
        return None
    addresses = resolve_physical_ipv4(host, route)
    if not addresses:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return DirectTarget(route, host, port, addresses)


def _connect_physical(host: str, port: int, route: PhysicalRoute, timeout_s: float = 12.0) -> socket.socket:
    last_error: OSError | None = None
    for address in resolve_physical_ipv4(host, route):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_s)
            _bind_physical(sock, route)
            sock.connect((address, port))
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError(f"无法通过物理网卡解析 {host}")


class _DirectProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], route: PhysicalRoute):
        self.route = route
        super().__init__(address, _DirectProxyHandler)


class _DirectProxyHandler(socketserver.StreamRequestHandler):
    def _reply(self, status: str) -> None:
        self.wfile.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))
        self.wfile.flush()

    def handle(self) -> None:
        self.connection.settimeout(12.0)
        request_line = self.rfile.readline(8192).decode("latin-1", errors="replace").strip()
        if not request_line:
            return
        while True:
            line = self.rfile.readline(8192)
            if line in {b"\r\n", b"\n", b""}:
                break
        parts = request_line.split()
        if len(parts) < 3 or parts[0].upper() != "CONNECT":
            self._reply("405 Method Not Allowed")
            return
        authority = parts[1]
        if authority.startswith("[") or ":" not in authority:
            self._reply("400 Bad Request")
            return
        host, raw_port = authority.rsplit(":", 1)
        host = host.lower().rstrip(".")
        try:
            port = int(raw_port)
        except ValueError:
            self._reply("400 Bad Request")
            return
        if port not in {80, 443} or not is_direct_host(host):
            self._reply("403 Forbidden")
            return
        try:
            upstream = _connect_physical(host, port, self.server.route)  # type: ignore[attr-defined]
        except OSError:
            self._reply("502 Bad Gateway")
            return
        try:
            self._reply("200 Connection Established")
            sockets = (self.connection, upstream)
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 30.0)
                if exceptional or not readable:
                    break
                for source in readable:
                    try:
                        chunk = source.recv(65536)
                    except (BlockingIOError, ConnectionResetError, OSError):
                        return
                    if not chunk:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(chunk)
        finally:
            upstream.close()


_proxy_lock = threading.Lock()
_proxy_server: _DirectProxyServer | None = None
_proxy_thread: threading.Thread | None = None


def ensure_direct_proxy() -> tuple[int, PhysicalRoute] | None:
    global _proxy_server, _proxy_thread
    with _proxy_lock:
        if _proxy_server is not None and _proxy_thread is not None and _proxy_thread.is_alive():
            return int(_proxy_server.server_address[1]), _proxy_server.route
        route = get_physical_route()
        if route is None:
            return None
        try:
            server = _DirectProxyServer(("127.0.0.1", 0), route)
        except OSError:
            return None
        thread = threading.Thread(
            target=server.serve_forever,
            name="bipai-physical-direct-proxy",
            daemon=True,
        )
        thread.start()
        _proxy_server = server
        _proxy_thread = thread
        return int(server.server_address[1]), route
