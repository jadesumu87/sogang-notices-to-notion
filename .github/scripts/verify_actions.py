from __future__ import annotations

import re
import sys
from pathlib import Path

USES_RE = re.compile(
    r"^\s*(?:-\s+)?uses\s*:\s*(?P<target>\"[^\"]+\"|'[^']+'|[^\s#]+)"
    r"\s*(?:#.*)?$"
)
USES_KEY_RE = re.compile(r"(?:^|[\s{,])(?:-\s*)?uses\s*:")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^docker://.+@sha256:[0-9a-f]{64}$")


def unquote(value: str) -> str:
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1]
    return value


def verify_text(text: str, source: str) -> list[str]:
    errors: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = USES_RE.match(line)
        if match is None:
            if USES_KEY_RE.search(line):
                errors.append(
                    f"{source}:{line_number}: uses 구문을 확인할 수 없습니다."
                )
            continue
        target = unquote(match.group("target"))
        if target.startswith("./"):
            continue
        if target.startswith("docker://"):
            if not IMAGE_DIGEST_RE.fullmatch(target):
                errors.append(
                    f"{source}:{line_number}: 컨테이너 이미지가 SHA-256 해시로 고정되지 않았습니다."
                )
            continue
        action, separator, reference = target.rpartition("@")
        if (
            not separator
            or "/" not in action
            or not COMMIT_SHA_RE.fullmatch(reference)
        ):
            errors.append(
                f"{source}:{line_number}: 액션이 40자리 커밋 SHA로 고정되지 않았습니다."
            )
    return errors


def workflow_files(root: Path) -> list[Path]:
    return sorted([*root.rglob("*.yml"), *root.rglob("*.yaml")])


def verify_directory(root: Path) -> list[str]:
    files = workflow_files(root)
    if not files:
        return [f"{root}: 워크플로 파일이 없습니다."]
    errors: list[str] = []
    for path in files:
        errors.extend(verify_text(path.read_text(encoding="utf-8"), str(path)))
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("사용법: verify_actions.py WORKFLOW_DIRECTORY", file=sys.stderr)
        return 2
    errors = verify_directory(Path(argv[0]))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("워크플로 액션 버전 고정을 확인했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
