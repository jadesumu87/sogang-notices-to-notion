from pathlib import Path
import runpy
import sys


def main() -> None:
    # 실제 실행 코드는 scripts/ 아래로 옮기고, 기존 루트 진입점은 계속 유지한다.
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    runpy.run_path(str(scripts_dir / "main.py"), run_name="__main__")


if __name__ == "__main__":
    # 기존 `python main.py` 실행 습관이 깨지지 않도록 얇은 래퍼로만 남긴다.
    main()
