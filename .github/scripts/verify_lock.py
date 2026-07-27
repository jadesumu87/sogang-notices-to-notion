from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(.*)$"
)
HASH_RE = re.compile(
    r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)"
)


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def logical_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\\\n", " ").splitlines()]


def parse_requirements(text: str, require_hashes: bool) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in logical_lines(text):
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise ValueError(f"허용하지 않는 요구사항 지시문입니다: {line[:120]}")
        match = REQUIREMENT_RE.match(line)
        if not match:
            raise ValueError(f"정확한 버전 고정이 아닌 요구사항입니다: {line[:120]}")
        name = normalize_name(match.group(1))
        version = match.group(2)
        suffix = match.group(3)
        hashes = HASH_RE.findall(suffix)
        if HASH_RE.sub("", suffix).strip():
            raise ValueError(f"{name} 패키지에 허용하지 않는 옵션이 있습니다.")
        previous = parsed.get(name)
        if previous is not None and previous != version:
            raise ValueError(f"{name} 패키지 버전이 중복 충돌합니다.")
        if require_hashes and not hashes:
            raise ValueError(f"{name} 패키지에 SHA-256 해시가 없습니다.")
        parsed[name] = version
    if not parsed:
        raise ValueError("검증할 패키지가 없습니다.")
    return parsed


def merge_requirements(source_texts: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source_text in source_texts:
        for name, version in parse_requirements(
            source_text,
            require_hashes=False,
        ).items():
            previous = merged.get(name)
            if previous is not None and previous != version:
                raise ValueError(
                    f"{name} 패키지 버전이 요구사항 파일 사이에서 충돌합니다."
                )
            merged[name] = version
    return merged


def verify(source_texts: list[str], lock_text: str) -> None:
    source = merge_requirements(source_texts)
    lock = parse_requirements(lock_text, require_hashes=True)
    missing = sorted(name for name in source if name not in lock)
    unexpected = sorted(name for name in lock if name not in source)
    mismatched = sorted(
        name for name, version in source.items() if name in lock and lock[name] != version
    )
    if missing:
        raise ValueError(f"잠금 파일에 직접 의존성이 없습니다: {', '.join(missing)}")
    if mismatched:
        details = ", ".join(
            f"{name}={source[name]}(요구사항)/{lock[name]}(잠금)" for name in mismatched
        )
        raise ValueError(f"직접 의존성 버전이 잠금 파일과 다릅니다: {details}")
    if unexpected:
        raise ValueError(
            "잠금 파일에 선언되지 않은 패키지가 있습니다: "
            + ", ".join(unexpected)
        )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "사용법: verify_lock.py LOCK SOURCE [SOURCE ...]",
            file=sys.stderr,
        )
        return 2
    lock_path = Path(argv[0])
    source_paths = [Path(value) for value in argv[1:]]
    verify(
        [path.read_text(encoding="utf-8") for path in source_paths],
        lock_path.read_text(encoding="utf-8"),
    )
    print(f"의존성 잠금 파일을 확인했습니다: {lock_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
