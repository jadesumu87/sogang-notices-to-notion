import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bbs_parser
import crawler
import sync
import utils
from models import FailureCategory, SiteFetchResult


def address_info(address: str = "93.184.216.34") -> tuple:
    return (
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        ),
    )


class FakeRoute:
    def __init__(self) -> None:
        self.action = ""
        self.status = 0
        self.body = b""

    def abort(self) -> None:
        self.action = "abort"

    def continue_(self) -> None:
        self.action = "continue"

    def fulfill(self, *, status: int, headers: dict, body: bytes) -> None:
        self.action = "fulfill"
        self.status = status
        self.body = body


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url
        self.resource_type = "document"


class FakeBodyLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def evaluate(self, _expression: str, limit: int) -> str:
        return self.text[:limit]

    def inner_text(self) -> str:
        return self.text


class EmptyLabelLocator:
    def count(self) -> int:
        return 0


class FakeDatePage:
    def __init__(self, body_text: str) -> None:
        self.body = FakeBodyLocator(body_text)

    def locator(self, selector: str):
        if selector == "body":
            return self.body
        return EmptyLabelLocator()


class FakeDomPage:
    def __init__(self, character_count: int, content: str) -> None:
        self.character_count = character_count
        self.content_value = content
        self.content_called = False

    def evaluate(self, _expression: str) -> int:
        return self.character_count

    def content(self) -> str:
        self.content_called = True
        return self.content_value


class SecurityReportRegressionTests(unittest.TestCase):
    def test_csf_f02078151ed4c8d6add39259_remote_marker_is_body_data(self) -> None:
        rich_text = [
            {
                "type": "text",
                "text": {
                    "content": (
                        f"{sync.SYNC_CONTAINER_MARKER}\n"
                        "원격 공지 첫 문단"
                    )
                },
            }
        ]
        block = {
            "id": "remote-marker",
            "type": "quote",
            "quote": {"rich_text": rich_text},
        }
        with patch.object(sync, "list_block_children", return_value=[]):
            prefix_length = sync.sync_container_prefix_length(
                "token",
                block,
                rich_text,
                [],
            )

        self.assertEqual(prefix_length, 0)
        self.assertEqual(
            sync.sync_container_body_rich_text(block),
            rich_text,
        )
        with (
            patch.object(
                sync,
                "load_body_generation_manifest",
                return_value=None,
            ),
            patch.object(
                sync,
                "list_block_children",
                return_value=[block],
            ),
        ):
            self.assertEqual(
                sync.list_sync_container_blocks("token", "page"),
                [],
            )

    def test_csf_24cda728e05348095fae3dd2_css_rgb_is_bounded(self) -> None:
        self.assertIsNone(
            bbs_parser.parse_css_color("rgb(1e309, 0, 0)")
        )
        self.assertIsNone(
            bbs_parser.parse_css_color(
                f"rgb({'9' * 300}, 0, 0)"
            )
        )
        self.assertEqual(
            bbs_parser.parse_css_color("rgb(12, 34, 56)"),
            (12, 34, 56),
        )

    def test_csf_a839d00b87f1ab81bfb2b31e_content_url_is_total(self) -> None:
        self.assertIsNone(
            utils.normalize_content_url("https://[invalid/path")
        )
        self.assertIsNone(
            utils.normalize_content_url(
                "https://www.sogang.ac.kr/" + "a" * 9000
            )
        )
        self.assertEqual(
            utils.normalize_content_url("/ko/page"),
            "https://www.sogang.ac.kr/ko/page",
        )

    def test_csf_1b1cbb065c09b239cb3fbc80_file_url_is_total(self) -> None:
        self.assertIsNone(
            utils.normalize_file_url("https://[invalid/file.pdf")
        )
        self.assertEqual(
            utils.normalize_file_url("/file-fe-prd/board/guide.pdf"),
            "https://www.sogang.ac.kr/file-fe-prd/board/guide.pdf",
        )

    def test_csf_1e36e0b849a4a392415ac0b7_detail_url_is_total(self) -> None:
        self.assertIsNone(
            utils.normalize_detail_url(
                "https://www.sogang.ac.kr:999999/ko/detail/1"
            )
        )
        self.assertEqual(
            utils.normalize_detail_url(
                "https://www.sogang.ac.kr/ko/detail/1"
                "?bbsConfigFk=141&page=9"
            ),
            "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=141",
        )

    def test_csf_866cdf740c17ed5844f4571b_view_count_is_bounded(self) -> None:
        self.assertIsNone(utils.parse_int("9" * 10000))
        self.assertIsNone(utils.parse_int("9" * 19))
        self.assertEqual(utils.parse_int("조회 12,345회"), 12345)

    def test_csf_9c56bc453eec83e90adf6783_json_integer_is_bounded(self) -> None:
        fetch_result = SiteFetchResult(
            ok=True,
            status_code=200,
            body=(
                b'{"data":{"viewCount":'
                + b"9" * 10000
                + b"}}"
            ),
            content_type="application/json",
            final_url="https://www.sogang.ac.kr/api/test",
        )
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=fetch_result,
        ):
            result, payload = crawler.fetch_site_json_result(
                "https://www.sogang.ac.kr/api/test"
            )

        self.assertIsNone(payload)
        self.assertFalse(result.ok)
        self.assertEqual(result.category, FailureCategory.SOURCE_CONTRACT)
        self.assertEqual(result.error, "json_integer_out_of_range")

    def test_csf_e3756e1dd038e92f638044d5_attachment_index_is_bounded(self) -> None:
        keys = crawler.api_attachment_field_keys(
            {
                "fileValue2": "b",
                "fileValue1": "a",
                f"fileValue{'9' * 10000}": "x",
                "fileValue101": "x",
            }
        )

        self.assertEqual(keys, ["fileValue1", "fileValue2"])

    def test_csf_703ec68687777cfd930a5c92_tag_stripping_is_linear(self) -> None:
        self.assertEqual(
            crawler.strip_html_for_attachment_text("<" * 200000),
            "",
        )
        self.assertEqual(
            crawler.strip_html_for_attachment_text(
                "<p>공지 <strong>본문</strong></p>"
            ),
            "공지  본문",
        )

    def test_csf_100b9e250295d921a03b4d0f_html_date_search_is_bounded(self) -> None:
        self.assertIsNone(
            bbs_parser.extract_written_at_from_detail(
                "<p>" + "작성일 " * 10000 + "</p>"
            )
        )
        self.assertEqual(
            bbs_parser.extract_written_at_from_detail(
                "<span>등록일</span><time>2026-07-31 10:20:30</time>"
            ),
            "2026-07-31T10:20:30+09:00",
        )

    def test_csf_84de92a10097a33ffc979e9a_page_date_search_is_bounded(self) -> None:
        self.assertIsNone(
            bbs_parser.extract_written_at_from_page(
                FakeDatePage("작성일 " * 100000)
            )
        )
        self.assertEqual(
            bbs_parser.extract_written_at_from_page(
                FakeDatePage("등록일 2026-07-31")
            ),
            "2026-07-31T00:00:00+09:00",
        )

    def test_csf_dfeb0e45cb03b6c0a9b0373a_title_parser_is_bounded(self) -> None:
        self.assertEqual(
            crawler.extract_detail_title_from_html(
                "<h1>" + "<" * 600000
            ),
            "",
        )
        self.assertEqual(
            crawler.extract_detail_title_from_html(
                '<meta content="정상 공지" property="og:title">'
                "<h1>다른 제목</h1>"
            ),
            "정상 공지",
        )

    def test_csf_4d6f4a6f3da21981b6f2aefb_sparse_table_is_rejected(self) -> None:
        sparse_rows = [[[{}]] for _ in range(100)]
        sparse_rows.append([[{}] for _ in range(100)])

        self.assertIsNone(
            utils.build_table_block(sparse_rows, False, False)
        )
        self.assertIsNotNone(
            utils.build_table_block(
                [
                    [[{}], [{}]],
                    [[{}], [{}]],
                ],
                True,
                False,
            )
        )

    def test_csf_c28d4465b08ec91e6e7dc1d7_list_bytes_are_bounded(self) -> None:
        guard = crawler.PlaywrightNetworkGuard(
            "www.sogang.ac.kr",
            address_info(),
            None,
            resolver=lambda _host, _port: address_info(),
        )
        route = FakeRoute()
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=SiteFetchResult(
                ok=False,
                category=FailureCategory.SECURITY_POLICY,
                error="response_too_large:10485761>10485760",
            ),
        ):
            guard.handle_route(
                route,
                FakeRequest(
                    "https://www.sogang.ac.kr/ko/academic-support/notices"
                ),
            )

        self.assertEqual(route.action, "abort")
        self.assertEqual(
            guard.document_error,
            "response_too_large:10485761>10485760",
        )
        page = FakeDomPage(
            crawler.get_site_response_max_bytes() + 1,
            "",
        )
        with self.assertRaisesRegex(ValueError, "page_dom_too_large"):
            crawler.read_bounded_playwright_page_content(page)
        self.assertFalse(page.content_called)

    def test_csf_f5f09656dab8d21f3437e728_detail_bytes_are_bounded(self) -> None:
        detail_url = (
            "https://www.sogang.ac.kr/ko/detail/1?bbsConfigFk=141"
        )
        guard = crawler.PlaywrightNetworkGuard(
            "www.sogang.ac.kr",
            address_info(),
            None,
            resolver=lambda _host, _port: address_info(),
        )
        route = FakeRoute()
        with patch.object(
            crawler,
            "fetch_site_result",
            return_value=SiteFetchResult(
                ok=False,
                category=FailureCategory.SECURITY_POLICY,
                error="response_too_large:10485761>10485760",
            ),
        ):
            guard.handle_route(route, FakeRequest(detail_url))

        self.assertEqual(route.action, "abort")
        self.assertEqual(guard.document_category, FailureCategory.SECURITY_POLICY)
        max_bytes = crawler.get_site_response_max_bytes()
        page = FakeDomPage(max_bytes, "가" * (max_bytes // 2))
        with self.assertRaisesRegex(ValueError, "page_dom_too_large"):
            crawler.read_bounded_playwright_page_content(page)
        self.assertTrue(page.content_called)


if __name__ == "__main__":
    unittest.main()
