import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bbs_parser
import sync
import utils


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
    def test_notion_elided_zero_width_space_is_removed_from_body_text(
        self,
    ) -> None:
        html = """
        <div class="tiptap">
          <p>발급을 재개할 예정&#8203;이오니 참고하시기 바랍니다.</p>
        </div>
        """

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        self.assertEqual(
            rich_text_content(blocks[0]),
            "발급을 재개할 예정이오니 참고하시기 바랍니다.",
        )

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


def text_item(
    content: str,
    *,
    bold: bool = False,
    color: str = "default",
    link: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content}
    if link:
        payload["link"] = {"url": link}
    return {
        "type": "text",
        "text": payload,
        "annotations": {
            **utils.DEFAULT_ANNOTATIONS,
            "bold": bold,
            "color": color,
        },
    }


def visible_formats(rich_text: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            character,
            tuple(sorted(item["annotations"].items())),
            (item["text"].get("link") or {}).get("url"),
        )
        for item in rich_text
        for character in item["text"]["content"]
        if not character.isspace()
    ]


class NotionLimitContractTests(unittest.TestCase):
    def test_rich_text_within_limit_is_unchanged(self) -> None:
        rich_text = [
            text_item(f"{index}", bold=index % 2 == 0)
            for index in range(utils.MAX_RICH_TEXT_ITEMS)
        ]

        self.assertIs(utils.fit_rich_text_items(rich_text), rich_text)

    def test_whitespace_runs_merge_without_changing_visible_format(
        self,
    ) -> None:
        rich_text = []
        for index in range(60):
            rich_text.append(
                text_item(f"항목{index}", bold=index % 2 == 0)
            )
            rich_text.append(text_item("\n", color="blue"))

        fitted = utils.fit_rich_text_items(rich_text)

        self.assertLessEqual(len(fitted), utils.MAX_RICH_TEXT_ITEMS)
        self.assertEqual(
            "".join(item["text"]["content"] for item in fitted),
            "".join(item["text"]["content"] for item in rich_text),
        )
        self.assertEqual(visible_formats(fitted), visible_formats(rich_text))

    def test_dense_formatting_keeps_text_and_links(self) -> None:
        rich_text = [
            text_item(
                f"조각{index}",
                bold=index % 2 == 0,
                link=(
                    f"https://www.sogang.ac.kr/link/{index}"
                    if index % 10 == 0
                    else None
                ),
            )
            for index in range(150)
        ]

        fitted = utils.fit_rich_text_items(rich_text)

        self.assertLessEqual(len(fitted), utils.MAX_RICH_TEXT_ITEMS)
        self.assertEqual(
            "".join(item["text"]["content"] for item in fitted),
            "".join(item["text"]["content"] for item in rich_text),
        )
        self.assertEqual(
            [
                item["text"]["link"]["url"]
                for item in fitted
                if item["text"].get("link")
            ],
            [
                f"https://www.sogang.ac.kr/link/{index}"
                for index in range(0, 150, 10)
            ],
        )
        self.assertEqual(utils.fit_rich_text_items(rich_text), fitted)

    def test_content_length_limit_is_never_exceeded(self) -> None:
        rich_text = [
            text_item("가" * 1500, bold=index % 2 == 0)
            for index in range(150)
        ]

        fitted = utils.fit_rich_text_items(rich_text)

        self.assertEqual(len(fitted), 150)
        self.assertTrue(
            all(
                len(item["text"]["content"])
                <= utils.MAX_RICH_TEXT_CONTENT_LENGTH
                for item in fitted
            )
        )

    def test_dense_table_cell_fits_notion_payload_limits(self) -> None:
        spans = "".join(
            f'<span style="color: {"red" if index % 2 else "blue"}">'
            f"{index}</span><br>"
            for index in range(120)
        )
        html = (
            '<div class="tiptap"><table><tr><td>구분</td>'
            f"<td>{spans}</td></tr></table></div>"
        )

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        cell = blocks[0]["table"]["children"][0]["table_row"]["cells"][1]
        self.assertLessEqual(len(cell), utils.MAX_RICH_TEXT_ITEMS)
        self.assertEqual(
            "".join(item["text"]["content"] for item in cell).split(),
            [str(index) for index in range(120)],
        )
        sync.validate_body_write_payloads(blocks)

    def test_long_table_splits_into_blocks_with_repeated_header(
        self,
    ) -> None:
        header = [[text_item("구분")], [text_item("값")]]
        rows = [header] + [
            [[text_item(f"행{index}")], [text_item(str(index))]]
            for index in range(149)
        ]

        tables = utils.build_table_blocks(rows, True, False)

        self.assertEqual(
            [len(table["table"]["children"]) for table in tables],
            [100, 51],
        )
        self.assertTrue(
            all(
                table["table"]["children"][0]["table_row"]["cells"]
                == header
                for table in tables
            )
        )
        self.assertEqual(
            [
                row["table_row"]["cells"][0][0]["text"]["content"]
                for table in tables
                for row in table["table"]["children"][1:]
            ],
            [f"행{index}" for index in range(149)],
        )
        self.assertEqual(
            [
                len(table["table"]["children"])
                for table in utils.build_table_blocks(rows[1:], False, False)
            ],
            [100, 49],
        )
        self.assertEqual(
            len(utils.build_table_blocks(rows[:100], True, False)),
            1,
        )

    def test_long_html_table_fits_notion_payload_limits(self) -> None:
        body_rows = "".join(
            f"<tr><td>행{index}</td><td>{index}</td></tr>"
            for index in range(150)
        )
        html = (
            '<div class="tiptap"><table>'
            "<tr><th>구분</th><th>값</th></tr>"
            f"{body_rows}</table></div>"
        )

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        self.assertEqual(
            [block["type"] for block in blocks],
            ["table", "table"],
        )
        sync.validate_body_write_payloads(blocks)


class OversizedContentContractTests(unittest.TestCase):
    def test_table_over_row_limit_is_kept_as_paragraphs(self) -> None:
        body_rows = "".join(
            f"<tr><td>행{index}</td><td>{index}</td></tr>"
            for index in range(utils.MAX_TABLE_ROWS + 1)
        )
        html = (
            '<div class="tiptap"><table>'
            "<tr><th>구분</th><th>값</th></tr>"
            f"{body_rows}</table></div>"
        )

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        self.assertTrue(all(block["type"] == "paragraph" for block in blocks))
        self.assertEqual(rich_text_content(blocks[0]), "구분 | 값")
        self.assertEqual(
            [rich_text_content(block) for block in blocks[1:]],
            [
                f"행{index} | {index}"
                for index in range(utils.MAX_TABLE_ROWS + 1)
            ],
        )
        sync.validate_body_write_payloads(blocks)

    def test_sparse_table_fallback_uses_only_source_cells(self) -> None:
        rows = [[[text_item(f"{index}")]] for index in range(100)]
        rows.append([[text_item(f"칸{index}")] for index in range(100)])
        rows.append([[], []])

        self.assertEqual(utils.build_table_blocks(rows, False, False), [])
        blocks = utils.build_table_text_blocks(rows)

        self.assertEqual(len(blocks), 101)
        self.assertEqual(
            rich_text_content(blocks[-1]),
            " | ".join(f"칸{index}" for index in range(100)),
        )
        self.assertTrue(
            all(
                len(block["paragraph"]["rich_text"])
                <= utils.MAX_RICH_TEXT_ITEMS
                for block in blocks
            )
        )

    def test_link_longer_than_notion_limit_keeps_text_only(self) -> None:
        long_link = "https://www.sogang.ac.kr/apply?" + "q=1&" * 600
        short_link = "https://www.sogang.ac.kr/apply?q=1"
        html = (
            '<div class="tiptap"><p>'
            f'<a href="{long_link}">긴 신청 링크</a> '
            f'<a href="{short_link}">짧은 신청 링크</a>'
            f'</p><iframe src="https://www.youtube.com/embed/abc?'
            f'{"t=1&" * 600}"></iframe></div>'
        )

        blocks = bbs_parser.extract_body_blocks_from_html(html)

        self.assertEqual([block["type"] for block in blocks], ["paragraph"])
        rich_text = blocks[0]["paragraph"]["rich_text"]
        self.assertEqual(
            rich_text_content(blocks[0]),
            "긴 신청 링크 짧은 신청 링크",
        )
        self.assertNotIn("link", rich_text[0]["text"])
        self.assertEqual(rich_text[-1]["text"]["link"]["url"], short_link)
        sync.validate_body_write_payloads(blocks)


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
