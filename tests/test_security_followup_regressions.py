import json
import socket
import struct
import sys
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bbs_parser
import crawler
import notion_client
from models import FailureCategory, SiteFetchResult


def public_address_info(count: int = 1) -> tuple:
    return tuple(
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (f"93.184.216.{34 + index}", 443),
        )
        for index in range(count)
    )


def make_json_fetch_result(body: bytes) -> SiteFetchResult:
    return SiteFetchResult(
        ok=True,
        status_code=200,
        body=body,
        content_type="application/json",
        final_url="https://www.sogang.ac.kr/api/test",
    )


def make_valid_compound_payload() -> bytes:
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 3)
    header[28:30] = b"\xfe\xff"
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 1)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4, 0xFFFFFFFF)
    struct.pack_into("<I", header, 76, 0)

    fat = bytearray(b"\xff" * 512)
    struct.pack_into("<I", fat, 0, 0xFFFFFFFD)
    struct.pack_into("<I", fat, 4, 0xFFFFFFFE)

    directory = bytearray(512)
    for index, (name, entry_type) in enumerate(
        (("Root Entry", 5), ("WordDocument", 2))
    ):
        encoded_name = name.encode("utf-16le") + b"\x00\x00"
        offset = index * 128
        directory[offset : offset + len(encoded_name)] = encoded_name
        struct.pack_into("<H", directory, offset + 64, len(encoded_name))
        directory[offset + 66] = entry_type
        directory[offset + 67] = 1
        struct.pack_into(
            "<III",
            directory,
            offset + 68,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
        )
    return bytes(header + fat + directory)


def make_difat_expansion_probe(difat_sector_count: int = 32) -> bytes:
    sector_size = 512
    payload = bytearray((difat_sector_count + 1) * sector_size)
    payload[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", payload, 24, 0x003E)
    struct.pack_into("<H", payload, 26, 3)
    payload[28:30] = b"\xfe\xff"
    struct.pack_into("<H", payload, 30, 9)
    struct.pack_into("<H", payload, 32, 6)
    struct.pack_into(
        "<I",
        payload,
        44,
        (sector_size // 4 - 1) * difat_sector_count,
    )
    struct.pack_into("<I", payload, 48, 0xFFFFFFFE)
    struct.pack_into("<I", payload, 68, 0)
    struct.pack_into("<I", payload, 72, difat_sector_count)
    for index in range(109):
        struct.pack_into("<I", payload, 76 + index * 4, 0xFFFFFFFF)
    for index in range(difat_sector_count):
        offset = (index + 1) * sector_size
        next_sector = (
            index + 1
            if index + 1 < difat_sector_count
            else 0xFFFFFFFE
        )
        struct.pack_into("<I", payload, offset + sector_size - 4, next_sector)
    return bytes(payload)


class FakeRoute:
    def __init__(self) -> None:
        self.action = ""
        self.body = b""

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"

    def fulfill(self, *, status: int, headers: dict, body: bytes) -> None:
        self.action = "fulfill"
        self.body = body


class FakeRequest:
    def __init__(
        self,
        url: str,
        resource_type: str,
        method: str = "GET",
    ) -> None:
        self.url = url
        self.resource_type = resource_type
        self.method = method


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class PlannedSocket:
    clock = FakeClock()
    plan: list[tuple[float, bool]] = []
    instances: list["PlannedSocket"] = []

    def __init__(self, family: int, socktype: int, proto: int) -> None:
        self.family = family
        self.socktype = socktype
        self.proto = proto
        self.timeout = None
        self.closed = False
        self.index = len(self.instances)
        self.instances.append(self)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def bind(self, address: tuple) -> None:
        self.bound_address = address

    def connect(self, address: tuple) -> None:
        self.connected_address = address
        advance, succeeds = self.plan[self.index]
        self.clock.now += advance
        if not succeeds:
            raise socket.timeout("planned timeout")

    def close(self) -> None:
        self.closed = True


class SecurityFollowupRegressionTests(unittest.TestCase):
    def test_csf_dec07cf94fb4581f65fa390a_json_structure_is_bounded(
        self,
    ) -> None:
        url = "https://www.sogang.ac.kr/api/test"
        deeply_nested = (
            b'{"data":'
            + b"[" * 10_000
            + b"0"
            + b"]" * 10_000
            + b"}"
        )
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=make_json_fetch_result(deeply_nested),
        ):
            result, payload = crawler.fetch_site_json_result(url)
        self.assertIsNone(payload)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "json_structure_too_deep")

        at_depth_limit = (
            b'{"data":'
            + b"[" * (crawler.MAX_JSON_NESTING_DEPTH - 1)
            + b"0"
            + b"]" * (crawler.MAX_JSON_NESTING_DEPTH - 1)
            + b"}"
        )
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=make_json_fetch_result(at_depth_limit),
        ):
            result, payload = crawler.fetch_site_json_result(url)
        self.assertTrue(result.ok)
        self.assertIsInstance(payload, dict)

        flat = json.dumps({"data": [1, 2, 3, 4]}).encode()
        with (
            patch.object(crawler, "MAX_JSON_STRUCTURE_NODES", 4),
            patch.object(
                crawler,
                "fetch_site_result",
                return_value=make_json_fetch_result(flat),
            ),
        ):
            result, payload = crawler.fetch_site_json_result(url)
        self.assertIsNone(payload)
        self.assertEqual(result.error, "json_structure_too_large")

        valid = {"data": '[{},\\",],', "items": [1, 2]}
        with (
            patch.object(crawler, "MAX_JSON_STRUCTURE_NODES", 4),
            patch.object(
                crawler,
                "fetch_site_result",
                return_value=make_json_fetch_result(
                    json.dumps(valid).encode()
                ),
            ),
        ):
            result, payload = crawler.fetch_site_json_result(url)
        self.assertTrue(result.ok)
        self.assertEqual(payload, valid)

        with (
            patch.object(
                crawler,
                "fetch_site_result",
                return_value=make_json_fetch_result(b"{}"),
            ),
            patch.object(
                crawler.json,
                "loads",
                side_effect=RecursionError,
            ),
        ):
            result, payload = crawler.fetch_site_json_result(url)
        self.assertIsNone(payload)
        self.assertEqual(result.error, "json_structure_too_deep")

        with patch.object(
            crawler,
            "fetch_site_result",
            side_effect=lambda *_args: make_json_fetch_result(
                deeply_nested
            ),
        ):
            list_result = crawler.fetch_bbs_list_result(
                1,
                20,
                config_fk="141",
            )
            detail_result = crawler.fetch_bbs_detail(
                "1001",
                config_fk="141",
            )
        self.assertFalse(list_result.ok)
        self.assertEqual(
            list_result.category,
            FailureCategory.SOURCE_CONTRACT,
        )
        self.assertEqual(
            list_result.error,
            "json_structure_too_deep",
        )
        self.assertIsNone(detail_result)

    def test_csf_fb84d60f3b3e3278a0bcf9f0_attachment_name_is_bounded(
        self,
    ) -> None:
        long_name = "&lt;" * 200_000
        html = (
            "<span>첨부파일</span>"
            '<a href="/file-fe-prd/board/large.pdf">'
            f"{long_name}</a>"
            '<a href="/file-fe-prd/board/normal.pdf">'
            "안내 &lt;tag&gt; 자료</a>"
            '<a href="/file-fe-prd/board/second.pdf">'
            f"{'B' * 800}</a>"
        )

        attachments = bbs_parser.extract_attachments_from_detail(html)

        self.assertEqual(len(attachments), 3)
        self.assertEqual(
            attachments[0]["name"],
            "<" * bbs_parser.MAX_ATTACHMENT_NAME_CHARS,
        )
        self.assertEqual(attachments[1]["name"], "안내 <tag> 자료")
        self.assertEqual(
            attachments[2]["name"],
            "B" * bbs_parser.MAX_ATTACHMENT_NAME_CHARS,
        )

        parser = bbs_parser.VisibleAnchorParser()
        parser.feed(
            '<span>첨부파일</span>'
            '<a href="/file-fe-prd/board/split.pdf">'
        )
        parser.feed("&lt;")
        parser.feed("&gt;")
        parser.feed("</a>")
        parser.close()
        self.assertEqual(
            parser.anchors,
            [
                (
                    "/file-fe-prd/board/split.pdf",
                    "< >",
                )
            ],
        )

    def test_csf_643991b626360b178b217063_connect_deadline_is_global(
        self,
    ) -> None:
        def reset_sockets(
            clock: FakeClock,
            plan: list[tuple[float, bool]],
        ) -> None:
            PlannedSocket.clock = clock
            PlannedSocket.plan = plan
            PlannedSocket.instances = []

        clock = FakeClock()
        reset_sockets(clock, [(0.0, False)] * 12)
        with (
            patch.object(notion_client.time, "monotonic", clock.monotonic),
            patch.object(
                notion_client,
                "resolve_public_network_address_info",
                return_value=public_address_info(12),
            ),
            patch.object(notion_client.socket, "socket", PlannedSocket),
            patch.object(notion_client, "check_run_control"),
        ):
            with self.assertRaises(OSError):
                notion_client.create_public_network_socket(
                    "www.sogang.ac.kr",
                    443,
                    10.0,
                )
        self.assertEqual(
            len(PlannedSocket.instances),
            notion_client.EXTERNAL_DOWNLOAD_MAX_CONNECT_ADDRESSES,
        )

        clock = FakeClock()
        reset_sockets(clock, [(0.25, False)] * 8)
        with (
            patch.object(notion_client.time, "monotonic", clock.monotonic),
            patch.object(
                notion_client,
                "resolve_public_network_address_info",
                return_value=public_address_info(8),
            ),
            patch.object(notion_client.socket, "socket", PlannedSocket),
            patch.object(notion_client, "check_run_control"),
        ):
            with self.assertRaises(socket.timeout):
                notion_client.create_public_network_socket(
                    "www.sogang.ac.kr",
                    443,
                    1.0,
                )
        self.assertEqual(
            [item.timeout for item in PlannedSocket.instances],
            [1.0, 0.75, 0.5, 0.25],
        )

        mixed_addresses = (
            public_address_info(1)[0],
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0),
            ),
        )
        clock = FakeClock()
        reset_sockets(clock, [(0.1, False), (0.0, True)])
        with (
            patch.object(notion_client.time, "monotonic", clock.monotonic),
            patch.object(
                notion_client,
                "resolve_public_network_address_info",
                return_value=mixed_addresses,
            ),
            patch.object(notion_client.socket, "socket", PlannedSocket),
            patch.object(notion_client, "check_run_control"),
        ):
            connected = notion_client.create_public_network_socket(
                "www.sogang.ac.kr",
                443,
                1.0,
            )
        self.assertIs(connected, PlannedSocket.instances[1])
        self.assertAlmostEqual(PlannedSocket.instances[1].timeout, 0.9)

        clock = FakeClock()
        reset_sockets(clock, [(0.0, True)])
        with (
            patch.object(notion_client.time, "monotonic", clock.monotonic),
            patch.object(
                notion_client,
                "resolve_public_network_address_info",
                return_value=public_address_info(),
            ),
            patch.object(notion_client.socket, "socket", PlannedSocket),
            patch.object(notion_client, "check_run_control"),
            notion_client.external_download_run_scope(
                force_new=True
            ) as policy,
        ):
            policy.max_seconds = 0.4
            with policy.activity():
                notion_client.create_public_network_socket(
                    "www.sogang.ac.kr",
                    443,
                    5.0,
                )
        self.assertAlmostEqual(PlannedSocket.instances[0].timeout, 0.4)

    def test_csf_2d3a31ef45f9b76873543558_subresources_are_bounded(
        self,
    ) -> None:
        resolver = Mock(return_value=public_address_info())
        guard = crawler.PlaywrightNetworkGuard(
            "www.sogang.ac.kr",
            public_address_info(),
            None,
            resolver=resolver,
        )

        def fetch_result(url: str, _label: str) -> SiteFetchResult:
            return SiteFetchResult(
                ok=True,
                status_code=200,
                body=b"1234",
                content_type="application/octet-stream",
                final_url=url,
            )

        routes = [FakeRoute(), FakeRoute(), FakeRoute()]
        with (
            patch.object(
                crawler,
                "PLAYWRIGHT_NAVIGATION_MAX_RESPONSE_BYTES",
                10,
            ),
            patch.object(
                crawler,
                "fetch_site_result",
                side_effect=fetch_result,
            ) as fetch,
        ):
            guard.begin_navigation()
            for route, resource_type in zip(
                routes,
                ("document", "script", "xhr"),
                strict=True,
            ):
                guard.handle_route(
                    route,
                    FakeRequest(
                        "https://www.sogang.ac.kr/assets/resource",
                        resource_type,
                    ),
                )
        self.assertEqual(
            [route.action for route in routes],
            ["fulfill", "fulfill", "abort"],
        )
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            guard.security_error,
            "fallback_browser_navigation_response_too_large:12>10",
        )
        self.assertNotIn("continue", [route.action for route in routes])

        guard.begin_navigation()
        for resource_type in ("document", "script", "xhr", "fetch"):
            with self.subTest(
                resource_type=resource_type,
                condition="oversized",
            ):
                guard.begin_navigation()
                oversized = FakeRoute()
                with patch.object(
                    crawler,
                    "fetch_site_result",
                    return_value=SiteFetchResult(
                        ok=False,
                        category=FailureCategory.SECURITY_POLICY,
                        error="response_too_large:1025>1024",
                    ),
                ):
                    guard.handle_route(
                        oversized,
                        FakeRequest(
                            "https://www.sogang.ac.kr/assets/resource",
                            resource_type,
                        ),
                    )
                self.assertEqual(oversized.action, "abort")
                self.assertEqual(
                    guard.security_error,
                    "response_too_large:1025>1024",
                )

        post = FakeRoute()
        with patch.object(
            crawler,
            "fetch_site_result",
        ) as fetch:
            guard.handle_route(
                post,
                FakeRequest(
                    "https://www.sogang.ac.kr/api/data",
                    "xhr",
                    "POST",
                ),
            )
        self.assertEqual(post.action, "abort")
        self.assertEqual(
            guard.security_error,
            "fallback_browser_unsupported_method",
        )
        fetch.assert_not_called()

    def test_csf_676e19d89ec17caab5c9535f_ole_metadata_is_bounded(
        self,
    ) -> None:
        probe = make_difat_expansion_probe(1_600)
        tracemalloc.start()
        try:
            self.assertIsNone(
                notion_client.inspect_upload_payload(probe)
            )
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak_bytes, len(probe) * 4)

        with patch.object(
            notion_client,
            "read_compound_sector",
            wraps=notion_client.read_compound_sector,
        ) as read_sector:
            self.assertIsNone(
                notion_client.collect_compound_directory_names(probe)
            )
        read_sector.assert_not_called()

        duplicate = bytearray(make_valid_compound_payload())
        struct.pack_into("<I", duplicate, 44, 2)
        struct.pack_into("<I", duplicate, 80, 0)
        with patch.object(
            notion_client,
            "read_compound_sector",
            wraps=notion_client.read_compound_sector,
        ) as read_sector:
            self.assertIsNone(
                notion_client.collect_compound_directory_names(
                    bytes(duplicate)
                )
            )
        read_sector.assert_not_called()
        self.assertEqual(
            notion_client.inspect_upload_payload(
                make_valid_compound_payload()
            ),
            "doc",
        )


if __name__ == "__main__":
    unittest.main()
