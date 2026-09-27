import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils
import notion_client


class BodyHashIdentityTests(unittest.TestCase):
    def image_blocks(self, file_id: str, signature: str) -> list[dict]:
        return [
            {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {
                        "url": (
                            "https://www.sogang.ac.kr/files/image"
                            f"?fileId={file_id}&signature={signature}"
                        )
                    },
                },
            }
        ]

    def file_blocks(self, file_id: str, signature: str) -> list[dict]:
        return [
            {
                "type": "embed",
                "embed": {
                    "url": (
                        "https://www.sogang.ac.kr/files/document.pdf"
                        f"?fileId={file_id}&signature={signature}"
                    )
                },
            }
        ]

    def normalized_hash(
        self,
        blocks: list[dict],
    ) -> tuple[list[dict], str]:
        normalized = utils.normalize_body_blocks_for_hash(
            blocks,
            upload_files=True,
        )
        return normalized, utils.compute_body_hash(
            normalized,
            image_mode="upload",
        )

    def test_rotating_signature_keeps_uploaded_image_and_file_hash_stable(self):
        for builder in (self.image_blocks, self.file_blocks):
            with self.subTest(builder=builder.__name__):
                old_blocks, old_hash = self.normalized_hash(
                    builder("77", "old")
                )
                new_blocks, new_hash = self.normalized_hash(
                    builder("77", "new")
                )

                self.assertEqual(old_hash, new_hash)
                self.assertEqual(old_blocks, new_blocks)
                marker = old_blocks[0][old_blocks[0]["type"]]
                self.assertIn("fileid=77", marker["source_url"])
                self.assertNotIn("signature", marker["source_url"])

    def test_file_identity_change_changes_uploaded_image_and_file_hash(self):
        for builder in (self.image_blocks, self.file_blocks):
            with self.subTest(builder=builder.__name__):
                _, first_hash = self.normalized_hash(
                    builder("77", "old")
                )
                _, second_hash = self.normalized_hash(
                    builder("78", "new")
                )

                self.assertNotEqual(first_hash, second_hash)

    def test_unavailable_media_hash_matches_preserved_write_payload(self):
        blocks = self.image_blocks("77", "old") + self.file_blocks("88", "old")
        with (
            patch.object(notion_client, "should_upload_files_to_notion", return_value=True),
            patch.object(notion_client, "pop_external_preflight_download", return_value=None),
            patch.object(notion_client, "download_file_bytes", return_value=(b"", "")),
            patch.object(notion_client, "upload_external_file_to_notion") as upload,
        ):
            actual, hash_blocks, state = notion_client.prepare_body_blocks_for_sync(
                "token", blocks
            )
        self.assertEqual(actual, blocks)
        self.assertEqual(state, [])
        self.assertEqual(
            utils.normalize_body_blocks_for_hash(blocks, True, media_content_state=[]),
            hash_blocks,
        )
        upload.assert_not_called()

    def test_unavailable_media_does_not_shift_later_content_hashes(self):
        blocks = (
            self.image_blocks("77", "old")
            + self.image_blocks("78", "old")
            + self.file_blocks("88", "old")
            + self.file_blocks("89", "old")
        )
        state = [
            {
                "type": "image",
                "source_url": blocks[1]["image"]["external"]["url"],
                "content_sha256": "a" * 64,
            },
            {
                "type": "pdf",
                "source_url": blocks[3]["embed"]["url"],
                "content_sha256": "b" * 64,
            },
        ]
        normalized = utils.normalize_body_blocks_for_hash(blocks, True, state)
        self.assertEqual(normalized[0], blocks[0])
        self.assertEqual(normalized[2], blocks[2])
        self.assertEqual(
            normalized[1],
            utils.build_uploaded_image_hash_block(state[0]["source_url"], None, "a" * 64),
        )
        self.assertEqual(
            normalized[3],
            utils.build_uploaded_file_hash_block(
                state[1]["source_url"], as_pdf=True, content_sha256="b" * 64
            ),
        )


if __name__ == "__main__":
    unittest.main()
