import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{SCRIPTS}{os.pathsep}{existing}" if existing else str(SCRIPTS)
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"하위 프로세스가 준비 전에 종료되었습니다: "
                f"code={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.02)
    process.kill()
    process.communicate(timeout=5)
    raise AssertionError(f"하위 프로세스 준비 시간이 초과되었습니다: {path}")


def run_lock_attempt(lock_path: Path) -> subprocess.CompletedProcess[str]:
    code = """
import sys
from pathlib import Path
from run_lock import exclusive_run_lock

with exclusive_run_lock(Path(sys.argv[1])):
    print("acquired", flush=True)
"""
    return subprocess.run(
        [sys.executable, "-c", code, str(lock_path)],
        env=child_environment(),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL이 필요합니다")
class ProcessRecoveryTests(unittest.TestCase):
    def test_sigkill_during_atomic_write_preserves_previous_state(self):
        import run_state

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            state_path = directory / "run-state.json"
            original = run_state.default_run_state()
            original["consecutive_failures"] = 2
            run_state.write_run_state_atomic(state_path, original)
            original_bytes = state_path.read_bytes()
            code = """
import os
import sys
import time
from pathlib import Path
import run_state

state_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
phase = sys.argv[3]
state = run_state.load_run_state(state_path)
state["consecutive_failures"] = 99

def block_dump(_payload, handle, **_kwargs):
    handle.write('{"schema_version":')
    handle.flush()
    os.fsync(handle.fileno())
    ready_path.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)

def block_replace(_source, _target):
    ready_path.write_text("ready", encoding="utf-8")
    while True:
        time.sleep(1)

if phase == "partial_dump":
    run_state.json.dump = block_dump
else:
    run_state.os.replace = block_replace
run_state.write_run_state_atomic(state_path, state)
"""
            for phase in ("partial_dump", "before_replace"):
                ready_path = directory / f"{phase}-ready"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(state_path),
                        str(ready_path),
                        phase,
                    ],
                    env=child_environment(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    wait_for_path(ready_path, process)
                    process.kill()
                    process.communicate(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

                with self.subTest(phase=phase):
                    self.assertEqual(process.returncode, -signal.SIGKILL)
                    self.assertEqual(state_path.read_bytes(), original_bytes)
                    loaded = run_state.load_run_state(state_path)
                    self.assertEqual(loaded["consecutive_failures"], 2)

            loaded = run_state.load_run_state(state_path)
            loaded["consecutive_failures"] = 3
            run_state.write_run_state_atomic(state_path, loaded)
            recovered = run_state.load_run_state(state_path)
            self.assertEqual(recovered["consecutive_failures"], 3)

    def test_sigkill_releases_lock_and_stale_pid_does_not_block_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            lock_path = directory / "run.lock"
            ready_path = directory / "lock-ready"
            code = """
import signal
import sys
from pathlib import Path
from run_lock import exclusive_run_lock

lock_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
with exclusive_run_lock(lock_path):
    ready_path.write_text("ready", encoding="utf-8")
    signal.pause()
"""
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(lock_path),
                    str(ready_path),
                ],
                env=child_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                wait_for_path(ready_path, process)
                stale_pid = lock_path.read_text(encoding="ascii")
                blocked = run_lock_attempt(lock_path)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("이미 진행 중", blocked.stderr)

                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

            self.assertEqual(process.returncode, -signal.SIGKILL)
            recovered = run_lock_attempt(lock_path)
            self.assertEqual(
                recovered.returncode,
                0,
                msg=recovered.stderr,
            )
            self.assertIn("acquired", recovered.stdout)
            self.assertNotEqual(
                lock_path.read_text(encoding="ascii"),
                stale_pid,
            )

    def test_sigterm_exits_cooperatively_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            lock_path = directory / "run.lock"
            ready_path = directory / "term-ready"
            code = """
import sys
from pathlib import Path
from run_control import install_run_control, sleep_with_run_control
from run_lock import exclusive_run_lock

install_run_control()
with exclusive_run_lock(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text("ready", encoding="utf-8")
    sleep_with_run_control(60)
"""
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(lock_path),
                    str(ready_path),
                ],
                env=child_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                wait_for_path(ready_path, process)
                process.terminate()
                _, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    _, stderr = process.communicate(timeout=5)

            self.assertNotEqual(process.returncode, 0)
            self.assertIn("안전 종료 신호가 요청되었습니다", stderr)
            recovered = run_lock_attempt(lock_path)
            self.assertEqual(
                recovered.returncode,
                0,
                msg=recovered.stderr,
            )

    def test_pending_sigterm_blocks_destination_entry(self):
        code = """
import os
import signal
from run_control import install_run_control, require_destination_state_reserve

install_run_control()
os.kill(os.getpid(), signal.SIGTERM)
require_destination_state_reserve()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=child_environment(),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("안전 종료 신호가 요청되었습니다", result.stderr)


if __name__ == "__main__":
    unittest.main()
