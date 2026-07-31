import os
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
from models import FailureCategory, SourceCrawlResult, SourceSpec, SourceStatus


SOURCE = SourceSpec(
    config_fk="141",
    classification="장학공지",
    list_url="https://www.sogang.ac.kr/ko/scholarship-notice",
)


def address_info(address: str) -> tuple:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    socket_address = (
        (address, 443, 0, 0)
        if family == socket.AF_INET6
        else (address, 443)
    )
    return (
        (
            family,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            socket_address,
        ),
    )


class FakeRequest:
    def __init__(self, url: str, resource_type: str):
        self.url = url
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self):
        self.action = ""
        self.body = b""

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"

    def fulfill(self, *, status, headers, body):
        self.action = "fulfill"
        self.body = body


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


class RedirectPage:
    def __init__(self, redirect_url: str):
        self.redirect_url = redirect_url
        self.url = "about:blank"
        self.route_handler = None

    def goto(self, url, **kwargs):
        route = FakeRoute()
        self.route_handler(
            route,
            FakeRequest(url, "document"),
        )
        if route.action not in {"continue", "fulfill"}:
            raise RuntimeError("navigation blocked")
        self.url = self.redirect_url
        return types.SimpleNamespace(status=200, headers={})

    def wait_for_load_state(self, *args, **kwargs):
        return None


class RedirectContext:
    def __init__(self, redirect_url: str):
        self.page = RedirectPage(redirect_url)

    def route(self, _pattern, handler):
        self.page.route_handler = handler

    def new_page(self):
        return self.page


class RedirectBrowser:
    def __init__(self, redirect_url: str):
        self.context = RedirectContext(redirect_url)

    def new_context(self, **kwargs):
        return self.context

    def close(self):
        return None


class RedirectLauncher:
    def __init__(self, redirect_url: str):
        self.browser = RedirectBrowser(redirect_url)
        self.launch_kwargs = {}

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return False


class PlaywrightNetworkSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "SOURCE_MAX_REQUESTS": "20",
                "SOURCE_MAX_SECONDS": "60",
                "SITE_MIN_REQUEST_INTERVAL_SECONDS": "0",
                "FALLBACK_MIN_INTERVAL_SECONDS": "0",
                "FALLBACK_JITTER_SECONDS": "0",
            },
        )
        self.env.start()
        crawler.NEXT_PLAYWRIGHT_REQUEST_AT_BY_HOST.clear()

    def tearDown(self):
        crawler.NEXT_PLAYWRIGHT_REQUEST_AT_BY_HOST.clear()
        self.env.stop()

    def make_guard(
        self,
        resolver,
        budget=None,
    ):
        return crawler.PlaywrightNetworkGuard(
            "www.sogang.ac.kr",
            address_info("93.184.216.34"),
            budget or crawler.SourceRequestBudget(),
            resolver=resolver,
        )

    def test_document_is_bounded_and_script_xhr_fetch_are_dns_validated(self):
        resolver = Mock(return_value=address_info("93.184.216.34"))
        guard = self.make_guard(resolver)

        def fetch_result(url, _label):
            return crawler.SiteFetchResult(
                ok=True,
                status_code=200,
                body=b"<html></html>",
                content_type="text/html",
                final_url=url,
            )

        with patch.object(
            crawler,
            "fetch_site_result",
            side_effect=fetch_result,
        ):
            for resource_type in ("document", "script", "xhr", "fetch"):
                with self.subTest(resource_type=resource_type):
                    route = FakeRoute()
                    guard.handle_route(
                        route,
                        FakeRequest(
                            "https://www.sogang.ac.kr/assets/app.js",
                            resource_type,
                        ),
                    )
                    self.assertEqual(
                        route.action,
                        (
                            "fulfill"
                            if resource_type == "document"
                            else "continue"
                        ),
                    )
        self.assertEqual(resolver.call_count, 5)

    def test_document_redirect_is_revalidated_before_fulfill(self):
        resolver = Mock(return_value=address_info("93.184.216.34"))
        guard = self.make_guard(resolver)
        route = FakeRoute()
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=crawler.SiteFetchResult(
                ok=True,
                status_code=200,
                body=b"<html></html>",
                content_type="text/html",
                final_url="https://evil.example/redirected",
            ),
        ):
            guard.handle_route(
                route,
                FakeRequest(
                    "https://www.sogang.ac.kr/ko/notices",
                    "document",
                ),
            )

        self.assertEqual(route.action, "abort")
        self.assertEqual(
            guard.security_error,
            "fallback_browser_unsafe_redirect",
        )

    def test_insecure_private_reserved_and_rebound_targets_are_blocked(self):
        cases = (
            (
                "http://www.sogang.ac.kr/ko/notices",
                Mock(return_value=address_info("93.184.216.34")),
            ),
            (
                "https://evil.example/script.js",
                Mock(return_value=address_info("93.184.216.34")),
            ),
            (
                "https://www.sogang.ac.kr/private.js",
                Mock(return_value=address_info("10.0.0.1")),
            ),
            (
                "https://www.sogang.ac.kr/reserved.js",
                Mock(return_value=address_info("192.0.2.1")),
            ),
            (
                "https://www.sogang.ac.kr/rebound.js",
                Mock(return_value=address_info("93.184.216.35")),
            ),
        )

        for url, resolver in cases:
            with self.subTest(url=url):
                route = FakeRoute()
                guard = self.make_guard(resolver)
                guard.handle_route(route, FakeRequest(url, "script"))
                self.assertEqual(route.action, "abort")

    def test_third_party_and_unnecessary_resources_are_blocked_before_budget(self):
        resolver = Mock(return_value=address_info("93.184.216.34"))
        budget = crawler.SourceRequestBudget()
        guard = self.make_guard(resolver, budget)

        third_party = FakeRoute()
        image = FakeRoute()
        guard.handle_route(
            third_party,
            FakeRequest("https://analytics.example/script.js", "script"),
        )
        guard.handle_route(
            image,
            FakeRequest(
                "https://www.sogang.ac.kr/assets/image.png",
                "image",
            ),
        )

        self.assertEqual(third_party.action, "abort")
        self.assertEqual(image.action, "abort")
        self.assertEqual(budget.actual_requests, 0)
        resolver.assert_not_called()

    def test_allowed_requests_are_spaced_per_host_and_share_actual_budget(self):
        clock = FakeClock()
        with (
            patch.dict(os.environ, {"SOURCE_MAX_REQUESTS": "2"}),
            patch.object(crawler.time, "monotonic", clock.monotonic),
            patch.object(
                crawler,
                "sleep_with_run_control",
                clock.sleep,
            ),
            patch.object(
                crawler,
                "get_site_min_request_interval_seconds",
                return_value=1.0,
            ),
        ):
            budget = crawler.SourceRequestBudget()
            guard = self.make_guard(
                Mock(return_value=address_info("93.184.216.34")),
                budget,
            )
            routes = [FakeRoute(), FakeRoute(), FakeRoute()]
            for route in routes:
                guard.handle_route(
                    route,
                    FakeRequest(
                        "https://www.sogang.ac.kr/api/data",
                        "xhr",
                    ),
                )

        self.assertEqual(
            [route.action for route in routes],
            ["continue", "continue", "abort"],
        )
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(budget.actual_requests, 2)
        self.assertEqual(
            budget.exhausted_reason,
            "source_request_budget_exceeded:2",
        )

    def test_redirected_page_url_is_revalidated_before_content_use(self):
        original = SourceCrawlResult(
            source=SOURCE,
            status=SourceStatus.FAILED,
            method="api",
            category=FailureCategory.SOURCE_UPSTREAM,
            error="api_failed",
        )
        launcher = RedirectLauncher(
            "https://evil.example/redirected"
        )
        http_fallback = Mock(
            side_effect=AssertionError("HTTP fallback must not run")
        )
        playwright_package = types.ModuleType("playwright")
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.TimeoutError = TimeoutError
        playwright_api.sync_playwright = lambda: FakePlaywright()
        playwright_package.sync_api = playwright_api

        with (
            patch.dict(
                sys.modules,
                {
                    "playwright": playwright_package,
                    "playwright.sync_api": playwright_api,
                },
            ),
            patch.object(
                crawler,
                "get_browser_launcher",
                return_value=launcher,
            ),
            patch.object(
                crawler,
                "resolve_public_network_address_info",
                return_value=address_info("93.184.216.34"),
            ),
            patch.object(
                crawler,
                "crawl_top_items_http_result",
                http_fallback,
            ),
            patch.object(
                crawler,
                "fetch_site_result",
                side_effect=lambda url, _label: crawler.SiteFetchResult(
                    ok=True,
                    status_code=200,
                    body=b"<html></html>",
                    content_type="text/html",
                    final_url=url,
                ),
            ),
        ):
            result = crawler.crawl_top_items_playwright_result(
                SOURCE,
                True,
                0,
                set(),
                False,
                original,
            )

        self.assertFalse(result.write_safe)
        self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(
            result.error,
            "fallback_browser_unsafe_redirect",
        )
        self.assertEqual(http_fallback.call_count, 0)
        self.assertIn("--disable-quic", launcher.launch_kwargs["args"])
        resolver_rules = next(
            value
            for value in launcher.launch_kwargs["args"]
            if value.startswith("--host-resolver-rules=MAP ")
        )
        self.assertIn("MAP www.sogang.ac.kr 93.184.216.34", resolver_rules)
        self.assertIn("MAP * ~NOTFOUND", resolver_rules)

    def test_legacy_playwright_entry_delegates_to_guarded_result(self):
        expected_items = [{"notice_id": "1001"}]
        guarded = Mock(
            return_value=SourceCrawlResult(
                source=SOURCE,
                status=SourceStatus.SUCCESS,
                items=expected_items,
                method="fallback_playwright",
            )
        )

        with (
            patch.object(
                crawler,
                "build_source_spec",
                return_value=SOURCE,
            ),
            patch.object(
                crawler,
                "crawl_top_items_playwright_result",
                guarded,
            ),
        ):
            items = crawler.crawl_top_items_playwright(
                "141",
                True,
                0,
            )

        self.assertEqual(items, expected_items)
        guarded.assert_called_once()


if __name__ == "__main__":
    unittest.main()
