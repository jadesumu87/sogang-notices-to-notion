import argparse
import os
from pathlib import Path
import runpy
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="서강대학교 공지를 수집해 Notion과 동기화합니다.",
    )
    parser.add_argument(
        "html_path",
        nargs="?",
        metavar="HTML_PATH",
        help="네트워크 대신 진단할 로컬 HTML 파일",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.html_path:
        os.environ["HTML_PATH"] = args.html_path
    sys.argv = [str(Path(__file__).resolve())]
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    runpy.run_path(str(scripts_dir / "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
