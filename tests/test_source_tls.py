import hashlib
import ssl
import sys
import tempfile
import unittest
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Literal, Optional, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crawler
import notion_client
from models import FailureCategory


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"ok",
        final_url: str = "https://www.sogang.ac.kr/ko/source",
    ) -> None:
        self.body = body
        self.final_url = final_url
        self.offset = 0
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/plain"

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> Literal[False]:
        return False

    def geturl(self) -> str:
        return self.final_url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("unbounded response read")
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def open(
        self,
        request: urllib.request.Request,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        return self.response


class SourceTLSContextTests(unittest.TestCase):
    def tearDown(self) -> None:
        crawler.build_sogang_source_ssl_context.cache_clear()

    def test_pinned_intermediate_is_loaded_without_partial_chain_trust(
        self,
    ) -> None:
        context = crawler.build_sogang_source_ssl_context()

        loaded_digests = {
            hashlib.sha256(certificate).hexdigest()
            for certificate in context.get_ca_certs(binary_form=True)
        }
        self.assertIn(
            crawler.SOGANG_TLS_INTERMEDIATE_DER_SHA256,
            loaded_digests,
        )
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        partial_chain = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
        if partial_chain:
            self.assertFalse(context.verify_flags & partial_chain)

    def test_context_selection_is_limited_to_expected_https_hosts(
        self,
    ) -> None:
        for url in (
            "https://www.sogang.ac.kr/ko/source",
            "https://sogang.ac.kr/ko/source",
            "https://WWW.SOGANG.AC.KR./ko/source",
        ):
            with self.subTest(url=url):
                self.assertIsNotNone(crawler.source_ssl_context_for_url(url))

        for url in (
            "http://www.sogang.ac.kr/ko/source",
            "https://user@www.sogang.ac.kr/ko/source",
            "https://www.sogang.ac.kr:444/ko/source",
            "https://scc.sogang.ac.kr/ko/source",
            "https://example.com/ko/source",
            "https://[invalid/source",
        ):
            with self.subTest(url=url):
                self.assertIsNone(crawler.source_ssl_context_for_url(url))

    def test_tampered_bundle_fails_closed_before_network_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid_path = Path(temp_dir) / "invalid.pem"
            invalid_path.write_text("not a certificate", encoding="ascii")
            with (
                patch.object(
                    crawler,
                    "SOGANG_TLS_INTERMEDIATE_PATH",
                    invalid_path,
                ),
                patch.object(
                    crawler,
                    "is_safe_external_download_target",
                    return_value=True,
                ),
                patch.object(
                    crawler,
                    "build_external_download_opener",
                ) as build_opener,
            ):
                crawler.build_sogang_source_ssl_context.cache_clear()
                result = crawler.fetch_site_result(
                    "https://www.sogang.ac.kr/ko/source",
                    "source",
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(result.error, "source_tls_bundle_invalid")
        self.assertEqual(result.attempts, 0)
        build_opener.assert_not_called()

    def test_bundle_digest_mismatch_fails_closed_before_network_access(
        self,
    ) -> None:
        with (
            patch.object(
                crawler,
                "SOGANG_TLS_INTERMEDIATE_DER_SHA256",
                "0" * 64,
            ),
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
            ) as build_opener,
        ):
            crawler.build_sogang_source_ssl_context.cache_clear()
            result = crawler.fetch_site_result(
                "https://www.sogang.ac.kr/ko/source",
                "source",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.category, FailureCategory.SECURITY_POLICY)
        self.assertEqual(result.error, "source_tls_bundle_invalid")
        self.assertEqual(result.attempts, 0)
        build_opener.assert_not_called()

    def test_source_fetch_passes_strict_context_to_validated_opener(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        def build_opener(
            before_redirect: Optional[Callable[[str], bool]] = None,
            ssl_context: Optional[ssl.SSLContext] = None,
        ) -> FakeOpener:
            captured["before_redirect"] = before_redirect
            captured["ssl_context"] = ssl_context
            return FakeOpener(FakeResponse())

        with (
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "reserve_site_request",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                side_effect=build_opener,
            ),
        ):
            result = crawler.fetch_site_result(
                "https://www.sogang.ac.kr/ko/source",
                "source",
            )

        self.assertTrue(result.ok)
        self.assertIsNotNone(captured["before_redirect"])
        self.assertIs(
            captured["ssl_context"],
            crawler.build_sogang_source_ssl_context(),
        )

    def test_unrelated_fetch_keeps_the_platform_default_context(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        def build_opener(
            before_redirect: Optional[Callable[[str], bool]] = None,
            ssl_context: Optional[ssl.SSLContext] = None,
        ) -> FakeOpener:
            captured["ssl_context"] = ssl_context
            return FakeOpener(
                FakeResponse(final_url="https://example.com/source")
            )

        with (
            patch.object(
                crawler,
                "is_safe_external_download_target",
                return_value=True,
            ),
            patch.object(
                crawler,
                "reserve_site_request",
                return_value=True,
            ),
            patch.object(
                crawler,
                "build_external_download_opener",
                side_effect=build_opener,
            ),
        ):
            result = crawler.fetch_site_result(
                "https://example.com/source",
                "source",
            )

        self.assertTrue(result.ok)
        self.assertIsNone(captured["ssl_context"])

    def test_validated_opener_preserves_the_explicit_context(self) -> None:
        context = ssl.create_default_context()
        opener = notion_client.build_external_download_opener(
            ssl_context=context
        )
        handler = cast(
            Any,
            next(
                value
                for value in cast(Any, opener).handlers
                if isinstance(
                    value,
                    notion_client.ValidatedExternalHTTPSHandler,
                )
            ),
        )

        self.assertIs(handler._context, context)


if __name__ == "__main__":
    unittest.main()
