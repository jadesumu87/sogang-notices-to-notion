import os
import socket
import sys
import unittest
import urllib.request
import urllib.response
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notion_client
import sync_engine
import utils
from log import LOGGER, redact_sensitive_urls
from models import SyncCounters


def make_png_payload() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), (32, 64, 96)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeHttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.requested_urls = []

    def https_open(self, req):
        self.requested_urls.append(req.full_url)
        status, raw_headers, payload = self.routes[req.full_url]
        headers = Message()
        for name, value in raw_headers.items():
            headers[name] = value
        response = urllib.response.addinfourl(
            BytesIO(payload),
            headers,
            req.full_url,
            status,
        )
        response.msg = "test"
        return response


class TrackingResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.offset = 0
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("unbounded read")
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.open_calls = []

    def open(self, request, timeout=None):
        self.open_calls.append((request.full_url, timeout))
        return self.response


class QueueOpener:
    def __init__(self, results):
        self.results = list(results)
        self.open_calls = []

    def open(self, request, timeout=None):
        self.open_calls.append((request.full_url, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def public_dns(host, port, type=0):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("163.239.1.17", port),
        )
    ]


class ExternalDownloadSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"ATTACHMENT_ALLOWED_DOMAINS": "sogang.ac.kr"},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    @staticmethod
    def http_error(url, status_code, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError(
            url,
            status_code,
            "test",
            headers,
            BytesIO(b""),
        )

    def test_network_address_policy_blocks_non_public_ranges(self):
        blocked = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "ff02::1",
        )
        for address in blocked:
            with self.subTest(address=address):
                self.assertFalse(utils.is_public_network_address(address))
        self.assertTrue(utils.is_public_network_address("163.239.1.17"))

    def test_external_download_target_requires_standard_https_port(self):
        with patch.object(
            utils,
            "resolve_public_network_address_info",
            return_value=public_dns("www.sogang.ac.kr", 443),
        ) as resolver:
            self.assertFalse(
                utils.is_safe_external_download_target(
                    "https://www.sogang.ac.kr:8443/file-fe-prd/board/a.pdf"
                )
            )
            self.assertTrue(
                utils.is_safe_external_download_target(
                    "https://www.sogang.ac.kr/file-fe-prd/board/a.pdf"
                )
            )
        resolver.assert_called_once_with("www.sogang.ac.kr", 443)

    def test_initial_allowed_host_resolving_to_private_address_is_blocked(self):
        transport = FakeHttpsHandler({})
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.object(
                utils.socket,
                "getaddrinfo",
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", 443),
                    )
                ],
            ),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(
                "https://www.sogang.ac.kr/file-fe-prd/board/private.png"
            )

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [])

    def test_mixed_public_and_private_dns_answers_are_blocked(self):
        mixed_answers = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("163.239.1.17", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]
        with patch.object(
            utils.socket,
            "getaddrinfo",
            return_value=mixed_answers,
        ):
            self.assertFalse(
                utils.is_safe_external_download_target(
                    "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
                )
            )

    def test_redirect_to_arbitrary_host_is_blocked_before_following(self):
        source_url = "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        target_url = "https://attacker.example/metadata"
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": target_url, "Content-Type": "text/html"},
                    b"",
                ),
                target_url: (200, {"Content-Type": "text/plain"}, b"secret"),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(source_url)

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [source_url])

    def test_redirect_allowed_host_resolving_private_is_blocked(self):
        source_url = "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        target_url = "https://cdn.sogang.ac.kr/private/image.png"
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": target_url, "Content-Type": "text/html"},
                    b"",
                ),
                target_url: (200, {"Content-Type": "image/png"}, b"secret"),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )

        def resolve(host, port, type=0):
            address = "127.0.0.1" if host == "cdn.sogang.ac.kr" else "163.239.1.17"
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port),
                )
            ]

        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=resolve),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(source_url)

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [source_url])

    def test_redirect_to_http_is_blocked_before_following(self):
        source_url = "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        target_url = "http://www.sogang.ac.kr/file-fe-prd/board/image.png"
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": target_url, "Content-Type": "text/html"},
                    b"",
                ),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(source_url)

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [source_url])

    def test_allowed_sogang_https_redirect_is_preserved(self):
        source_url = "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        target_url = "https://cdn.sogang.ac.kr/file-fe-prd/board/image.png"
        payload = b"allowed-redirect"
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": target_url, "Content-Type": "text/html"},
                    b"",
                ),
                target_url: (
                    200,
                    {
                        "Content-Type": "image/png",
                        "Content-Length": str(len(payload)),
                    },
                    payload,
                ),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(source_url)

        self.assertEqual(result, (payload, "image/png"))
        self.assertEqual(transport.requested_urls, [source_url, target_url])

    def test_redirect_hop_consumes_the_same_run_request_budget(self):
        source_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        )
        target_url = (
            "https://cdn.sogang.ac.kr/file-fe-prd/board/image.png"
        )
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": target_url, "Content-Type": "text/html"},
                    b"",
                ),
                target_url: (
                    200,
                    {"Content-Type": "image/png"},
                    b"unexpected",
                ),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "EXTERNAL_DOWNLOAD_MAX_REQUESTS": "1",
                    "EXTERNAL_DOWNLOAD_MAX_SECONDS": "30",
                },
            ),
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            with notion_client.external_download_run_scope() as policy:
                result = notion_client.download_file_bytes(source_url)
                snapshot = policy.snapshot()

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [source_url])
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["stopped_reason"], "request_cap")

    def test_every_redirect_hop_is_revalidated(self):
        source_url = "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
        second_url = "https://cdn.sogang.ac.kr/file-fe-prd/board/image.png"
        blocked_url = "https://attacker.example/metadata"
        transport = FakeHttpsHandler(
            {
                source_url: (
                    302,
                    {"Location": second_url, "Content-Type": "text/html"},
                    b"",
                ),
                second_url: (
                    302,
                    {"Location": blocked_url, "Content-Type": "text/html"},
                    b"",
                ),
                blocked_url: (200, {"Content-Type": "text/plain"}, b"secret"),
            }
        )
        opener = urllib.request.build_opener(
            transport,
            notion_client.ValidatedExternalRedirectHandler(),
        )
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(source_url)

        self.assertEqual(result, (None, None))
        self.assertEqual(transport.requested_urls, [source_url, second_url])

    def test_declared_oversize_is_rejected_without_reading(self):
        response = TrackingResponse(
            b"x",
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": "9",
            },
        )
        opener = FakeOpener(response)
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(
                "https://www.sogang.ac.kr/file-fe-prd/board/file.bin",
                max_bytes=8,
            )

        self.assertEqual(result, (None, None))
        self.assertEqual(response.read_sizes, [])

    def test_streaming_oversize_is_rejected_without_unbounded_read(self):
        response = TrackingResponse(
            b"123456789",
            {"Content-Type": "application/octet-stream"},
        )
        opener = FakeOpener(response)
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(
                "https://www.sogang.ac.kr/file-fe-prd/board/file.bin",
                max_bytes=8,
            )

        self.assertEqual(result, (None, None))
        self.assertTrue(response.read_sizes)
        self.assertNotIn(-1, response.read_sizes)

    def test_streaming_exact_limit_is_preserved(self):
        payload = b"12345678"
        response = TrackingResponse(
            payload,
            {"Content-Type": "application/octet-stream"},
        )
        opener = FakeOpener(response)
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(
                "https://www.sogang.ac.kr/file-fe-prd/board/file.bin",
                max_bytes=8,
            )

        self.assertEqual(result, (payload, "application/octet-stream"))
        self.assertNotIn(-1, response.read_sizes)

    def test_allowed_sogang_https_download_is_preserved(self):
        payload = b"allowed-file"
        response = TrackingResponse(
            payload,
            {
                "Content-Type": "application/pdf",
                "Content-Length": str(len(payload)),
            },
        )
        opener = FakeOpener(response)
        with (
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
        ):
            result = notion_client.download_file_bytes(
                "https://www.sogang.ac.kr/file-fe-prd/board/notice.pdf",
                require_file_hint=True,
            )

        self.assertEqual(result, (payload, "application/pdf"))
        self.assertTrue(response.read_sizes)
        self.assertNotIn(-1, response.read_sizes)

    def test_same_host_downloads_are_spaced_without_delaying_other_hosts(self):
        clock = FakeClock()
        payloads = [b"first", b"second", b"third"]
        opener = QueueOpener(
            [
                TrackingResponse(
                    payload,
                    {
                        "Content-Type": "image/png",
                        "Content-Length": str(len(payload)),
                    },
                )
                for payload in payloads
            ]
        )
        urls = [
            "https://www.sogang.ac.kr/file-fe-prd/board/first.png",
            "https://cdn.sogang.ac.kr/file-fe-prd/board/second.png",
            "https://www.sogang.ac.kr/file-fe-prd/board/third.png",
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS": "2",
                    "EXTERNAL_DOWNLOAD_MAX_REQUESTS": "10",
                    "EXTERNAL_DOWNLOAD_MAX_SECONDS": "30",
                },
            ),
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
            patch.object(
                notion_client.time,
                "monotonic",
                side_effect=clock.monotonic,
            ),
            patch.object(
                notion_client,
                "sleep_with_run_control",
                side_effect=clock.sleep,
            ),
        ):
            with notion_client.external_download_run_scope() as policy:
                results = [
                    notion_client.download_file_bytes(url)
                    for url in urls
                ]
                snapshot = policy.snapshot()

        self.assertEqual(
            results,
            [(payload, "image/png") for payload in payloads],
        )
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(len(opener.open_calls), 3)
        self.assertEqual(snapshot["requests"], 3)

    def test_first_403_or_429_opens_circuit_without_another_request(self):
        first_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/blocked.png"
        )
        second_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/skipped.png"
        )
        for status_code in (403, 429):
            with self.subTest(status_code=status_code):
                opener = QueueOpener(
                    [
                        self.http_error(
                            first_url,
                            status_code,
                            retry_after="30",
                        )
                    ]
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS": "0.1",
                            "EXTERNAL_DOWNLOAD_MAX_REQUESTS": "10",
                            "EXTERNAL_DOWNLOAD_MAX_SECONDS": "30",
                        },
                    ),
                    patch.object(
                        utils.socket,
                        "getaddrinfo",
                        side_effect=public_dns,
                    ),
                    patch.object(
                        notion_client,
                        "build_external_download_opener",
                        return_value=opener,
                    ),
                ):
                    with notion_client.external_download_run_scope() as policy:
                        first = notion_client.download_file_bytes(first_url)
                        second = notion_client.download_file_bytes(second_url)
                        snapshot = policy.snapshot()

                self.assertEqual(first, (None, None))
                self.assertEqual(second, (None, None))
                self.assertEqual(len(opener.open_calls), 1)
                self.assertEqual(snapshot["status_code"], status_code)
                self.assertEqual(snapshot["retry_after"], "30")
                self.assertEqual(snapshot["retry_after_seconds"], 30.0)
                self.assertEqual(
                    snapshot["stopped_reason"],
                    f"http_{status_code}",
                )

    def test_request_and_time_caps_stop_remaining_downloads(self):
        url = "https://www.sogang.ac.kr/file-fe-prd/board/file.png"
        for cap_kind in ("requests", "time"):
            with self.subTest(cap_kind=cap_kind):
                clock = FakeClock()
                opener = QueueOpener(
                    [
                        TrackingResponse(
                            b"ok",
                            {
                                "Content-Type": "image/png",
                                "Content-Length": "2",
                            },
                        )
                    ]
                )
                env = {
                    "EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS": "0.1",
                    "EXTERNAL_DOWNLOAD_MAX_REQUESTS": (
                        "1" if cap_kind == "requests" else "10"
                    ),
                    "EXTERNAL_DOWNLOAD_MAX_SECONDS": "1",
                }
                with (
                    patch.dict(os.environ, env),
                    patch.object(
                        utils.socket,
                        "getaddrinfo",
                        side_effect=public_dns,
                    ),
                    patch.object(
                        notion_client,
                        "build_external_download_opener",
                        return_value=opener,
                    ),
                    patch.object(
                        notion_client.time,
                        "monotonic",
                        side_effect=clock.monotonic,
                    ),
                ):
                    with notion_client.external_download_run_scope() as policy:
                        first = notion_client.download_file_bytes(url)
                        if cap_kind == "time":
                            clock.now = 2.0
                        second = notion_client.download_file_bytes(
                            url.replace("file.png", "next.png")
                        )
                        snapshot = policy.snapshot()

                self.assertEqual(first, (b"ok", "image/png"))
                self.assertEqual(second, (None, None))
                self.assertEqual(len(opener.open_calls), 1)
                self.assertEqual(
                    snapshot["stopped_reason"],
                    "request_cap" if cap_kind == "requests" else "time_cap",
                )

    def test_retry_after_longer_than_download_budget_stops_without_sleep(self):
        url = "https://www.sogang.ac.kr/file-fe-prd/board/file.png"
        opener = QueueOpener(
            [self.http_error(url, 503, retry_after="30")]
        )
        clock = FakeClock()
        with (
            patch.dict(
                os.environ,
                {
                    "EXTERNAL_DOWNLOAD_MAX_REQUESTS": "10",
                    "EXTERNAL_DOWNLOAD_MAX_SECONDS": "1",
                    "EXTERNAL_DOWNLOAD_MIN_REQUEST_INTERVAL_SECONDS": "0.1",
                },
            ),
            patch.object(utils.socket, "getaddrinfo", side_effect=public_dns),
            patch.object(
                notion_client,
                "build_external_download_opener",
                return_value=opener,
            ),
            patch.object(
                notion_client.time,
                "monotonic",
                side_effect=clock.monotonic,
            ),
            patch.object(
                notion_client,
                "sleep_with_run_control",
                side_effect=clock.sleep,
            ),
        ):
            with notion_client.external_download_run_scope() as policy:
                result = notion_client.download_file_bytes(url)
                snapshot = policy.snapshot()

        self.assertEqual(result, (None, None))
        self.assertEqual(len(opener.open_calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["stopped_reason"], "time_cap")

    def test_apply_report_transfers_circuit_and_resets_next_run(self):
        policies = []

        def fake_apply_report(*args, **kwargs):
            policy = notion_client.current_external_download_run_policy()
            self.assertIsNotNone(policy)
            policies.append(policy)
            if len(policies) == 1:
                policy.open_circuit(429, "45")
            return SyncCounters()

        with patch.object(
            sync_engine,
            "_apply_report",
            side_effect=fake_apply_report,
        ):
            with notion_client.external_download_run_scope() as outer:
                first = sync_engine.apply_report(
                    "token",
                    "database",
                    object(),
                    False,
                    run_id="first",
                )
                second = sync_engine.apply_report(
                    "token",
                    "database",
                    object(),
                    False,
                    run_id="second",
                )
                self.assertIs(
                    notion_client.current_external_download_run_policy(),
                    outer,
                )

        self.assertIsNot(policies[0], policies[1])
        self.assertIsNot(policies[0], outer)
        self.assertIsNot(policies[1], outer)
        self.assertEqual(first.external_download_status_code, 429)
        self.assertEqual(first.external_download_retry_after, "45")
        self.assertEqual(
            first.external_download_retry_after_seconds,
            45.0,
        )
        self.assertEqual(
            first.external_download_stopped_reason,
            "http_429",
        )
        self.assertIsNone(second.external_download_status_code)
        self.assertIsNone(second.external_download_retry_after)
        self.assertEqual(second.external_download_stopped_reason, "")
        self.assertIsNone(
            notion_client.current_external_download_run_policy()
        )

    def test_https_connection_uses_the_validated_numeric_address(self):
        address_info = (
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("163.239.1.17", 443),
            ),
        )

        class FakeSocket:
            def __init__(self):
                self.connected_address = None

            def settimeout(self, timeout):
                self.timeout = timeout

            def bind(self, address):
                self.bound_address = address

            def connect(self, address):
                self.connected_address = address

            def setsockopt(self, level, option, value):
                self.socket_option = (level, option, value)

            def close(self):
                self.closed = True

        class FakeContext:
            def wrap_socket(self, connection, server_hostname=None):
                self.server_hostname = server_hostname
                return connection

        fake_socket = FakeSocket()
        context = FakeContext()
        connection = notion_client.ValidatedExternalHTTPSConnection(
            "www.sogang.ac.kr",
            443,
            timeout=30,
            context=context,
        )
        with (
            patch.object(
                notion_client,
                "resolve_public_network_address_info",
                return_value=address_info,
            ),
            patch.object(notion_client.socket, "socket", return_value=fake_socket),
        ):
            connection.connect()

        self.assertEqual(fake_socket.connected_address, ("163.239.1.17", 443))
        self.assertEqual(context.server_hostname, "www.sogang.ac.kr")

    def test_sensitive_url_query_and_fragment_are_removed_from_logs(self):
        url = (
            "https://user:password@www.sogang.ac.kr/"
            "file-fe-prd/board/image.png?token=secret&signature=private#fragment"
        )
        redacted = redact_sensitive_urls(f"download failed: {url}")

        self.assertIn(
            "www.sogang.ac.kr/file-fe-prd/board/image.png",
            redacted,
        )
        for secret in ("user", "password", "token", "secret", "signature", "fragment"):
            self.assertNotIn(secret, redacted)

        with self.assertLogs(LOGGER, level="INFO") as captured:
            LOGGER.info("외부 URL 실패: %s", url)
        joined = "\n".join(captured.output)
        self.assertNotIn("secret", joined)
        self.assertNotIn("signature", joined)

        notion_id = "12345678-1234-1234-1234-123456789abc"
        compact_id = "abcdefabcdefabcdefabcdefabcdefab"
        identifier_log = redact_sensitive_urls(
            "GET /v1/pages/"
            f"{notion_id} request_id={compact_id}"
        )
        self.assertNotIn(notion_id, identifier_log)
        self.assertNotIn(compact_id, identifier_log)
        self.assertIn("[ID]", identifier_log)

        with self.assertLogs(LOGGER, level="ERROR") as captured:
            try:
                raise RuntimeError(url)
            except RuntimeError:
                LOGGER.exception("외부 URL 예외")
        exception_log = "\n".join(captured.output)
        self.assertNotIn("secret", exception_log)
        self.assertNotIn("signature", exception_log)
        self.assertNotIn("private", exception_log)

        with self.assertLogs(LOGGER, level="ERROR") as captured:
            LOGGER.error(
                "외부 URL 예외 객체: %s",
                RuntimeError(
                    "https://www.sogang.ac.kr/file.png"
                    "?token=object-secret#object-fragment"
                ),
            )
            LOGGER.error(
                "인증 오류: %s token=%s",
                "Bearer example-auth-value",
                "example-query-value",
            )
        object_log = "\n".join(captured.output)
        for secret in (
            "object-secret",
            "object-fragment",
            "example-auth-value",
            "example-query-value",
        ):
            self.assertNotIn(secret, object_log)

    def test_notion_tokens_are_removed_from_plain_and_structured_logs(self):
        legacy_token = "secret_" + ("a" * 16)
        current_token = "ntn_" + ("b" * 16)
        opaque_token = "opaque-bearer-value-123"
        basic_token = "basic-credential-value-456"
        token_scheme_value = "token-scheme-value-789"
        samples = (
            legacy_token,
            current_token,
            f"NOTION_TOKEN={legacy_token}",
            f'{{"token":"{current_token}"}}',
            f"Authorization: Bearer {current_token}",
            f"Authorization: Bearer {opaque_token}",
            f"Authorization: Basic {basic_token}",
            f"authorization: Token {token_scheme_value}",
        )

        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_sensitive_urls(sample)
                self.assertNotIn(legacy_token, redacted)
                self.assertNotIn(current_token, redacted)
                self.assertNotIn(opaque_token, redacted)
                self.assertNotIn(basic_token, redacted)
                self.assertNotIn(token_scheme_value, redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_external_url_log_arguments_are_sanitized_before_logging(self):
        source_url = (
            "https://www.sogang.ac.kr/file-fe-prd/board/image.png"
            "?token=source-secret#source-fragment"
        )
        upload_url = (
            "https://uploads.example/image"
            "?signature=upload-secret#upload-fragment"
        )

        with patch.object(notion_client.LOGGER, "warning") as warning:
            notion_client.download_file_bytes(
                "https://attacker.example/image.png"
                "?token=blocked-secret#blocked-fragment"
            )

        warning_text = repr(warning.call_args_list)
        self.assertNotIn("blocked-secret", warning_text)
        self.assertNotIn("blocked-fragment", warning_text)

        with (
            patch.object(
                notion_client,
                "download_file_bytes",
                return_value=(make_png_payload(), "image/png"),
            ),
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                return_value={"upload_url": upload_url},
            ),
            patch.object(notion_client.LOGGER, "info") as info,
        ):
            result = notion_client.upload_external_file_to_notion(
                "token",
                source_url,
            )

        self.assertIsNone(result)
        info_text = repr(info.call_args_list)
        for secret in (
            "source-secret",
            "source-fragment",
            "upload-secret",
            "upload-fragment",
        ):
            self.assertNotIn(secret, info_text)

        blocks = [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": (
                            "https://attacker.example/image.png"
                            "?token=body-secret#body-fragment"
                        )
                    },
                },
            },
            {
                "type": "embed",
                "embed": {
                    "url": (
                        "https://attacker.example/file.pdf"
                        "?signature=embed-secret#embed-fragment"
                    )
                },
            },
        ]
        with (
            patch.object(
                notion_client,
                "should_upload_files_to_notion",
                return_value=True,
            ),
            patch.object(notion_client.LOGGER, "info") as info,
        ):
            notion_client.prepare_body_blocks_for_sync(
                "token",
                blocks,
            )

        info_text = repr(info.call_args_list)
        for secret in (
            "body-secret",
            "body-fragment",
            "embed-secret",
            "embed-fragment",
        ):
            self.assertNotIn(secret, info_text)

    def test_image_pixel_and_dimension_limits_block_before_decode(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (100, 100), (255, 255, 255)).save(
            buffer,
            format="PNG",
        )
        payload = buffer.getvalue()

        with patch.dict(
            os.environ,
            {
                "IMAGE_MAX_PIXELS": "9999",
                "IMAGE_MAX_DIMENSION": "100",
            },
        ):
            self.assertIsNone(
                notion_client.compress_image_to_limit(
                    payload,
                    "image/png",
                    1024,
                )
            )

        with patch.dict(
            os.environ,
            {
                "IMAGE_MAX_PIXELS": "10000",
                "IMAGE_MAX_DIMENSION": "99",
            },
        ):
            self.assertIsNone(
                notion_client.compress_image_to_limit(
                    payload,
                    "image/png",
                    1024,
                )
            )


if __name__ == "__main__":
    unittest.main()
