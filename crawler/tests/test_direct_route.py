from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from app.direct_route import (
    PhysicalRoute,
    _discover_windows_route,
    _read_dns_name,
    direct_target_for_url,
    is_direct_host,
)


class DirectRouteTests(unittest.TestCase):
    def test_only_known_source_domains_may_use_local_proxy(self) -> None:
        for host in (
            "pay.ldxp.cn",
            "www.ldxp.cn",
            "pickai.cc",
            "catfk.com",
            "o.alicdn.com",
        ):
            self.assertTrue(is_direct_host(host), host)
        for host in ("evil-ldxp.cn", "example.com", "ldxp.cn.example.com", ""):
            self.assertFalse(is_direct_host(host), host)

    def test_route_discovery_rejects_fake_ip_tun_adapter(self) -> None:
        rows = [
            {
                "alias": "fcclient",
                "description": "fcclient Tunnel",
                "index": 41,
                "addresses": ["198.18.0.1"],
                "gateways": ["198.18.0.2"],
                "dns": ["198.18.0.2"],
            },
            {
                "alias": "Ethernet",
                "description": "Realtek PCIe GbE Family Controller",
                "index": 2,
                "addresses": ["192.168.0.10"],
                "gateways": ["192.168.0.1"],
                "dns": ["192.168.0.1"],
            },
        ]
        with patch("app.direct_route._powershell_json", return_value=rows):
            route = _discover_windows_route()
        self.assertEqual(route, PhysicalRoute("Ethernet", 2, "192.168.0.10", "192.168.0.1", ("192.168.0.1",)))

    def test_dns_parser_handles_compressed_answer_name(self) -> None:
        payload = b"\0" * 12 + b"\x03www\x07example\x03com\0"
        answer_offset = len(payload)
        payload += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + b"\x01\x02\x03\x04"
        name, next_offset = _read_dns_name(payload, answer_offset)
        self.assertEqual(name, "www.example.com")
        self.assertEqual(next_offset, answer_offset + 2)

    def test_unrelated_url_never_discovers_or_forces_route(self) -> None:
        with patch("app.direct_route.get_physical_route") as discover:
            result = direct_target_for_url("https://example.com/path")
        self.assertIsNone(result)
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
