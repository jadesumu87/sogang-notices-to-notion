from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from run_state import (
    build_public_cache_state,
    load_run_state,
    write_run_state_atomic,
)


def project_state(source: Path, destination: Path) -> None:
    try:
        state = build_public_cache_state(load_run_state(source))
        write_run_state_atomic(destination, state)
    except Exception:
        if source != destination:
            destination.unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "사용법: cache_state.py SOURCE DESTINATION",
            file=sys.stderr,
        )
        return 2
    project_state(Path(argv[0]), Path(argv[1]))
    print("공개 캐시용 실행 상태를 확인했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
