import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bbs_parser


def rich_text_content(block: dict[str, Any]) -> str:
    block_type = str(block["type"])
    rich_text = block[block_type]["rich_text"]
    return "".join(part["text"]["content"] for part in rich_text)


class ListRowContractTests(unittest.TestCase):
    def test_rows_preserve_identity_url_date_views_and_top_status(self) -> None:
        html = """
        <table><tbody>
          <tr data-id="12345">
            <td>TOP</td>
            <td><a href="/ko/detail/12345?bbsConfigFk=141">장학 공지</a></td>
            <td>학생지원팀</td>
            <td>2026.07.27 09:30</td>
            <td>1,234</td>
          </tr>
          <tr onclick="view('23456')">
            <td>17</td>
            <td>일반 공지</td>
            <td>교무팀</td>
            <td>2026-07-26</td>
            <td>9</td>
          </tr>
        </tbody></table>
        """

        rows = bbs_parser.parse_rows(html, config_fk="141")

        self.assertEqual(
            rows,
            [
                {
                    "title": "장학 공지",
                    "author": "학생지원팀",
                    "date": "2026-07-27T09:30:00+09:00",
                    "views": 1234,
                    "top": True,
                    "url": (
                        "https://www.sogang.ac.kr/ko/detail/"
                        "12345?bbsConfigFk=141"
                    ),
                },
                {
                    "title": "일반 공지",
                    "author": "교무팀",
                    "date": "2026-07-26T00:00:00+09:00",
                    "views": 9,
                    "top": False,
                    "url": (
                        "https://www.sogang.ac.kr/ko/detail/"
                        "23456?bbsConfigFk=141"
                    ),
                },
            ],
        )

    def test_invalid_rows_are_skipped_and_unsafe_metadata_is_not_a_url(self) -> None:
        html = """
        <table>
          <tr><td>1</td><td>셀 부족</td><td>작성자</td></tr>
          <tr><td>2</td><td>날짜 오류</td><td>작성자</td>
              <td>오늘</td><td>1</td></tr>
          <tr><td>3</td><td>조회수 오류</td><td>작성자</td>
              <td>2026-07-27</td><td>많음</td></tr>
          <tr><td>3</td><td>숨은 날짜</td><td>작성자</td>
              <td><script>2026-07-27</script></td><td>1</td></tr>
          <tr onclick="javascript:alert('99999')">
            <td>4</td><td><script>오염</script>안전한 행</td><td>작성자</td>
            <td>2026-07-27</td><td>10</td>
          </tr>
        </table>
        """

        rows = bbs_parser.parse_rows(html, config_fk="141")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "안전한 행")
        self.assertIsNone(rows[0]["url"])


class BodyBlockContractTests(unittest.TestCase):
    def test_heading_levels_paragraph_list_and_inline_styles_are_preserved(
        self,
    ) -> None:
        html = """
        <div class="tiptap">
          <h1>제목 1</h1><h2>제목 2</h2><h3>제목 3</h3>
          <h4>제목 4</h4><h5>제목 5</h5><h6>제목 6</h6>
          <p>본문 <strong>굵게</strong> <em>기울임</em>
             <u>밑줄</u> <s>취소</s> <code>코드</code>
             <span style="color:#ff0000">빨강</span>
             <a href="https://www.sogang.ac.kr/ko/page">링크</a></p>
          <ul><li>첫째</li><li>둘째</li></ul>
        </div>
        """

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        self.assertEqual(
            [block["type"] for block in blocks[:6]],
            [
                "heading_1",
                "heading_2",
                "heading_3",
                "heading_3",
                "heading_3",
                "heading_3",
            ],
        )
        self.assertEqual(
            [rich_text_content(block) for block in blocks[:6]],
            ["제목 1", "제목 2", "제목 3", "제목 4", "제목 5", "제목 6"],
        )
        paragraph = blocks[6]["paragraph"]["rich_text"]
        annotations = {
            part["text"]["content"].strip(): part["annotations"]
            for part in paragraph
            if part["text"]["content"].strip()
        }
        self.assertTrue(annotations["굵게"]["bold"])
        self.assertTrue(annotations["기울임"]["italic"])
        self.assertTrue(annotations["밑줄"]["underline"])
        self.assertTrue(annotations["취소"]["strikethrough"])
        self.assertTrue(annotations["코드"]["code"])
        self.assertEqual(annotations["빨강"]["color"], "red")
        link = next(
            part for part in paragraph if part["text"]["content"].strip() == "링크"
        )
        self.assertEqual(
            link["text"]["link"]["url"],
            "https://www.sogang.ac.kr/ko/page",
        )
        self.assertEqual(
            [block["type"] for block in blocks[7:]],
            ["bulleted_list_item", "bulleted_list_item"],
        )
        self.assertEqual(
            [rich_text_content(block) for block in blocks[7:]],
            ["첫째", "둘째"],
        )

    def test_table_image_embed_and_fragment_contracts(self) -> None:
        html = """
        <div class="tiptap">
          <table>
            <tr><th>구분</th><th>값</th></tr>
            <tr><th>A</th><td>1</td></tr>
          </table>
          <img src="/file-fe-prd/board/chart.png">
          <iframe src="https://www.youtube.com/embed/abc"></iframe>
        </div>
        """

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        table = blocks[0]["table"]
        self.assertEqual(table["table_width"], 2)
        self.assertTrue(table["has_column_header"])
        self.assertTrue(table["has_row_header"])
        self.assertEqual(len(table["children"]), 2)
        self.assertEqual(
            table["children"][1]["table_row"]["cells"][1][0]["text"]["content"],
            "1",
        )
        self.assertEqual(
            blocks[1]["image"]["external"]["url"],
            "https://www.sogang.ac.kr/file-fe-prd/board/chart.png",
        )
        self.assertEqual(
            blocks[2]["embed"]["url"],
            "https://www.youtube.com/embed/abc",
        )
        fragment = bbs_parser.extract_body_blocks_from_html("<p>조각 본문</p>")
        self.assertEqual(rich_text_content(fragment[0]), "조각 본문")

    def test_empty_body_and_unsafe_media_are_rejected(self) -> None:
        empty = '<div class="tiptap"><p></p></div>'
        unsafe = """
        <div class="tiptap">
          <img src="data:image/png;base64,AAAA">
          <img src="javascript:alert(1)">
          <iframe src="javascript:alert(2)"></iframe>
          <p><a href="javascript:alert(3)">표시 텍스트</a></p>
        </div>
        """

        self.assertEqual(bbs_parser.extract_body_blocks_from_html(empty), [])
        self.assertEqual(bbs_parser.inspect_body_content(empty), (True, False))
        blocks = bbs_parser.extract_body_blocks_from_html(unsafe)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(rich_text_content(blocks[0]), "표시 텍스트")
        self.assertNotIn("link", blocks[0]["paragraph"]["rich_text"][0]["text"])

    def test_hidden_executable_content_is_neither_parsed_nor_counted(self) -> None:
        hidden_only = """
        <div class="tiptap">
          <script>secretScript()</script>
          <style>.secret { color: red; }</style>
          <template><p>secret template</p><img src="/secret.png"></template>
        </div>
        """
        with_visible = hidden_only.replace(
            "</div>",
            "<p>공개 본문</p></div>",
        )

        self.assertEqual(
            bbs_parser.extract_body_blocks_from_html(hidden_only),
            [],
        )
        self.assertEqual(
            bbs_parser.inspect_body_content(hidden_only),
            (True, False),
        )
        blocks = bbs_parser.extract_body_blocks_from_html(with_visible)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(rich_text_content(blocks[0]), "공개 본문")
        self.assertEqual(
            bbs_parser.inspect_body_content(with_visible),
            (True, True),
        )

    def test_unclosed_full_document_fails_closed(self) -> None:
        html = '<html><body><div class="tiptap"><p>완료되지 않은 본문'

        self.assertEqual(bbs_parser.extract_body_blocks_from_html(html), [])
        self.assertEqual(bbs_parser.inspect_body_content(html), (True, False))


class DetailSignalContractTests(unittest.TestCase):
    def test_loading_shell_is_distinct_from_error_and_hidden_shells(self) -> None:
        loading = '<main><div class="notice-loading skeleton"></div></main>'
        error = '<main><div class="notice-error">불러오기 실패</div></main>'
        hidden = (
            '<main><div class="loading" aria-hidden="true">'
            "로딩 중</div></main>"
        )

        self.assertTrue(bbs_parser.detect_loading_shell(loading))
        self.assertFalse(bbs_parser.detect_loading_shell(error))
        self.assertFalse(bbs_parser.detect_loading_shell(hidden))

    def test_attachment_container_and_allowed_links_are_narrowly_detected(
        self,
    ) -> None:
        html = """
        <section class="attachment-list">
          <span>첨부파일</span>
          <a href="/file-fe-prd/board/guide.pdf">안내서</a>
          <a href="https://evil.example/file.pdf">외부 파일</a>
          <a href="https://www.sogang.ac.kr/ko/home">학교 홈</a>
          <a href="/file-fe-prd/board/guide.pdf">중복</a>
        </section>
        """

        self.assertTrue(bbs_parser.detect_attachment_container(html))
        attachments = bbs_parser.extract_attachments_from_detail(html)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["name"], "안내서")
        self.assertEqual(
            attachments[0]["external"]["url"],
            "https://www.sogang.ac.kr/file-fe-prd/board/guide.pdf",
        )
        self.assertFalse(
            bbs_parser.detect_attachment_container(
                "<script>첨부파일</script><div>첨부파일</div>"
            )
        )
        self.assertEqual(
            bbs_parser.extract_attachments_from_detail(
                "<script><a href='/file-fe-prd/board/hidden.pdf'>"
                "숨은 첨부</a></script>"
            ),
            [],
        )

    def test_written_at_prefers_timestamp_and_supports_registration_date(
        self,
    ) -> None:
        timestamp = """
        <dl><dt>등록일</dt><dd>2026-07-26</dd>
            <dt>작성일</dt><dd>2026.07.27 14:05:09</dd></dl>
        """

        self.assertEqual(
            bbs_parser.extract_written_at_from_detail(timestamp),
            "2026-07-27T14:05:09+09:00",
        )
        self.assertEqual(
            bbs_parser.extract_written_at_from_detail(
                "<span>등록일</span><time>2026-07-26</time>"
            ),
            "2026-07-26T00:00:00+09:00",
        )
        self.assertIsNone(
            bbs_parser.extract_written_at_from_detail(
                "<script>작성일 2026-07-27</script>"
            )
        )


if __name__ == "__main__":
    unittest.main()
