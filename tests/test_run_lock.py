import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_lock import exclusive_run_lock


class RunLockTests(unittest.TestCase):
    def test_second_holder_is_rejected_and_release_allows_reentry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.lock"
            with exclusive_run_lock(path):
                with self.assertRaisesRegex(RuntimeError, "이미 진행 중"):
                    with exclusive_run_lock(path):
                        pass
            with exclusive_run_lock(path):
                self.assertEqual(path.read_text(encoding="ascii"), str(os.getpid()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.lock"
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                with exclusive_run_lock(path):
                    raise RuntimeError("sentinel")
            with exclusive_run_lock(path):
                pass

    def test_symlink_lock_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.write_text("preserve", encoding="utf-8")
            path = root / "run.lock"
            path.symlink_to(target)
            with self.assertRaises(OSError):
                with exclusive_run_lock(path):
                    pass
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
