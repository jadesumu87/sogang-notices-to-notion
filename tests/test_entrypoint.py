import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_help_exits_without_runtime_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), "--help"],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("HTML_PATH", result.stdout)
            self.assertFalse((Path(temp_dir) / ".runtime").exists())


if __name__ == "__main__":
    unittest.main()
