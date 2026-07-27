import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils


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


if __name__ == "__main__":
    unittest.main()
