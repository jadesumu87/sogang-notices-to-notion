import hashlib
import struct
import sys
import unittest
import zipfile
import zlib
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import notion_client


def make_image_payload(image_format: str, color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (3, 2), color).save(buffer, format=image_format)
    return buffer.getvalue()


def make_zip_payload(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def make_ooxml_payload(kind: str) -> bytes:
    target_name, root_name = {
        "docx": ("word/document.xml", "document"),
        "xlsx": ("xl/workbook.xml", "workbook"),
        "pptx": ("ppt/presentation.xml", "presentation"),
    }[kind]
    return make_zip_payload(
        {
            "[Content_Types].xml": (
                b'<Types xmlns="http://schemas.openxmlformats.org/'
                b'package/2006/content-types"/>'
            ),
            "_rels/.rels": (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/'
                b'package/2006/relationships"/>'
            ),
            target_name: f"<{root_name}/>".encode(),
        }
    )


def make_hwpx_payload() -> bytes:
    return make_zip_payload(
        {
            "mimetype": b"application/hwp+zip",
            "version.xml": b"<HCFVersion/>",
            "Contents/content.hpf": b"<package/>",
            "Contents/section0.xml": b"<section/>",
            "META-INF/manifest.xml": b"<manifest/>",
        }
    )


def make_compound_payload(names: list[str]) -> bytes:
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)
    struct.pack_into("<H", header, 26, 3)
    header[28:30] = b"\xfe\xff"
    struct.pack_into("<H", header, 30, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<I", header, 40, 0)
    struct.pack_into("<I", header, 44, 1)
    struct.pack_into("<I", header, 48, 1)
    struct.pack_into("<I", header, 56, 4096)
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)
    struct.pack_into("<I", header, 64, 0)
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)
    struct.pack_into("<I", header, 72, 0)
    for index in range(109):
        struct.pack_into("<I", header, 76 + index * 4, 0xFFFFFFFF)
    struct.pack_into("<I", header, 76, 0)

    fat = bytearray(b"\xff" * 512)
    struct.pack_into("<I", fat, 0, 0xFFFFFFFD)
    struct.pack_into("<I", fat, 4, 0xFFFFFFFE)

    directory = bytearray(512)
    all_names = [("Root Entry", 5), *((name, 2) for name in names)]
    for index, (name, entry_type) in enumerate(all_names):
        encoded_name = name.encode("utf-16le") + b"\x00\x00"
        offset = index * 128
        directory[offset : offset + len(encoded_name)] = encoded_name
        struct.pack_into("<H", directory, offset + 64, len(encoded_name))
        directory[offset + 66] = entry_type
        directory[offset + 67] = 1
        struct.pack_into("<III", directory, offset + 68, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    return bytes(header + fat + directory)


def make_rar_payload() -> bytes:
    def build_header(header_type: int, body: bytes = b"") -> bytes:
        header_without_crc = struct.pack(
            "<BHH",
            header_type,
            0,
            7 + len(body),
        ) + body
        return (
            struct.pack(
                "<H",
                zlib.crc32(header_without_crc) & 0xFFFF,
            )
            + header_without_crc
        )

    return (
        b"Rar!\x1a\x07\x00"
        + build_header(0x73, b"\x00" * 6)
        + build_header(0x7B)
    )


def make_7z_payload() -> bytes:
    next_header = b"\x01\x00"
    start_header = struct.pack(
        "<QQI",
        0,
        len(next_header),
        zlib.crc32(next_header) & 0xFFFFFFFF,
    )
    return (
        b"7z\xbc\xaf\x27\x1c"
        + b"\x00\x04"
        + struct.pack("<I", zlib.crc32(start_header) & 0xFFFFFFFF)
        + start_header
        + next_header
    )


class AttachmentFormatSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()
        self.png = make_image_payload("PNG", (10, 20, 30))
        self.pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    def tearDown(self) -> None:
        notion_client.FILE_UPLOAD_CACHE.clear()

    def test_supported_payloads_match_extension_and_content_type(self) -> None:
        cases = [
            (make_image_payload("JPEG", (1, 2, 3)), "a.jpg", "image/jpeg", True),
            (self.png, "a.png", "image/png", True),
            (make_image_payload("GIF", (4, 5, 6)), "a.gif", "image/gif", True),
            (make_image_payload("BMP", (7, 8, 9)), "a.bmp", "image/bmp", True),
            (make_image_payload("WEBP", (10, 11, 12)), "a.webp", "image/webp", True),
            (self.pdf, "a.pdf", "application/pdf", False),
            (make_compound_payload(["WordDocument"]), "a.doc", "application/msword", False),
            (
                make_compound_payload(["Workbook"]),
                "a.xls",
                "application/vnd.ms-excel",
                False,
            ),
            (
                make_compound_payload(["PowerPoint Document"]),
                "a.ppt",
                "application/vnd.ms-powerpoint",
                False,
            ),
            (
                make_compound_payload(["FileHeader", "BodyText"]),
                "a.hwp",
                "application/vnd.hancom.hwp",
                False,
            ),
            (
                make_ooxml_payload("docx"),
                "a.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                False,
            ),
            (
                make_ooxml_payload("xlsx"),
                "a.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                False,
            ),
            (
                make_ooxml_payload("pptx"),
                "a.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                False,
            ),
            (
                make_hwpx_payload(),
                "a.hwpx",
                "application/vnd.hancom.hwpx",
                False,
            ),
            (make_zip_payload({"a.txt": b"hello"}), "a.zip", "application/zip", False),
            (make_rar_payload(), "a.rar", "application/vnd.rar", False),
            (make_7z_payload(), "a.7z", "application/x-7z-compressed", False),
            ("서강대학교".encode("cp949"), "a.txt", "text/plain", False),
            ("제목,내용\n공지,본문".encode("cp949"), "a.csv", "text/csv", False),
        ]

        for payload, filename, content_type, expect_image in cases:
            with self.subTest(filename=filename):
                result = notion_client.validate_external_upload_payload(
                    payload,
                    filename,
                    content_type,
                    expect_image,
                )
                self.assertIsNotNone(result)

    def test_valid_image_and_pdf_reach_upload_transport(self) -> None:
        with (
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                side_effect=[
                    {"id": "image-upload"},
                    {"id": "pdf-upload"},
                ],
            ) as create,
            patch.object(
                notion_client,
                "send_file_upload",
                return_value={"status": "uploaded"},
            ) as send,
        ):
            image_result = notion_client.upload_external_file_to_notion(
                "token",
                "https://www.sogang.ac.kr/file-fe-prd/board/image.png",
                downloaded_file=(self.png, "image/png"),
            )
            pdf_result = notion_client.upload_external_file_to_notion(
                "token",
                "https://www.sogang.ac.kr/file-fe-prd/board/notice.pdf",
                expect_image=False,
                downloaded_file=(self.pdf, "application/pdf"),
            )

        self.assertEqual(image_result, "image-upload")
        self.assertEqual(pdf_result, "pdf-upload")
        self.assertEqual(create.call_count, 2)
        self.assertEqual(send.call_count, 2)

    def test_missing_extension_is_added_after_format_validation(self) -> None:
        with (
            patch.object(
                notion_client,
                "get_workspace_upload_limit",
                return_value=None,
            ),
            patch.object(
                notion_client,
                "create_file_upload",
                return_value={"id": "image-upload"},
            ) as create,
            patch.object(
                notion_client,
                "send_file_upload",
                return_value={"status": "uploaded"},
            ),
        ):
            result = notion_client.upload_external_file_to_notion(
                "token",
                "https://www.sogang.ac.kr/file-fe-prd/board/download",
                filename_hint="download",
                downloaded_file=(self.png, "image/png"),
            )

        self.assertEqual(result, "image-upload")
        self.assertEqual(create.call_args.args[1], "download.png")

    def test_mismatches_polyglots_and_unsafe_formats_never_create_upload(
        self,
    ) -> None:
        ooxml = make_ooxml_payload("docx")
        ambiguous_ole = make_compound_payload(["WordDocument", "Workbook"])
        malformed_ooxml = make_zip_payload({"word/document.xml": b"<document/>"})
        archive_tail = make_zip_payload({"payload.txt": b"embedded"})
        invalid_cases = [
            (self.png, "a.jpg", "image/png", True),
            (self.png, "a.png", "image/jpeg", True),
            (self.pdf, "a.png", "image/png", True),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.svg", "image/svg+xml", True),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.png", "image/png", True),
            (self.png + archive_tail, "a.png", "image/png", True),
            (
                b"%PDF-1.4\n" + archive_tail + b"\n%%EOF\n",
                "a.pdf",
                "application/pdf",
                False,
            ),
            (ooxml, "a.zip", "application/zip", False),
            (ambiguous_ole, "a.doc", "application/msword", False),
            (
                make_compound_payload(["WordDocument"]) + archive_tail,
                "a.doc",
                "application/msword",
                False,
            ),
            (
                malformed_ooxml,
                "a.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                False,
            ),
            (b"plain\x00binary", "a.txt", "text/plain", False),
            (b"Rar!\x1a\x07\x00", "a.rar", "application/vnd.rar", False),
            (
                make_rar_payload() + self.pdf,
                "a.rar",
                "application/vnd.rar",
                False,
            ),
            (self.pdf, "a.exe", "application/pdf", False),
        ]

        with (
            patch.object(notion_client, "create_file_upload") as create,
            patch.object(notion_client, "send_file_upload") as send,
        ):
            for index, (
                payload,
                filename,
                content_type,
                expect_image,
            ) in enumerate(invalid_cases):
                with self.subTest(filename=filename, index=index):
                    result = notion_client.upload_external_file_to_notion(
                        "token",
                        (
                            "https://www.sogang.ac.kr/file-fe-prd/board/"
                            f"{index}-{filename}"
                        ),
                        filename_hint=filename,
                        expect_image=expect_image,
                        downloaded_file=(payload, content_type),
                    )
                    self.assertIsNone(result)

        create.assert_not_called()
        send.assert_not_called()

    def test_invalid_payload_cannot_bypass_validation_through_cache(self) -> None:
        payload = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        url = "https://www.sogang.ac.kr/file-fe-prd/board/image.svg"
        cache_key = (
            notion_client.normalize_attachment_identity_url(url) or url,
            hashlib.sha256(payload).hexdigest(),
        )
        notion_client.FILE_UPLOAD_CACHE[cache_key] = "cached-upload"

        with patch.object(notion_client, "create_file_upload") as create:
            result = notion_client.upload_external_file_to_notion(
                "token",
                url,
                downloaded_file=(payload, "image/svg+xml"),
            )

        self.assertIsNone(result)
        create.assert_not_called()

    def test_corrupted_png_is_rejected_even_when_small(self) -> None:
        corrupted = bytearray(self.png)
        corrupted[-8] ^= 0x01

        self.assertIsNone(
            notion_client.validate_external_upload_payload(
                bytes(corrupted),
                "a.png",
                "image/png",
                True,
            )
        )


if __name__ == "__main__":
    unittest.main()
