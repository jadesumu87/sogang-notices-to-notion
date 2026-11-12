import io
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from collections.abc import Sequence
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_lock_vulnerabilities
import cache_state
import install_playwright_browser
import verify_actions
import verify_lock


def browser_tests_required() -> bool:
    return os.environ.get("REQUIRE_BROWSER_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class RecordingRunner:
    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.failures = dict(failures or {})
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command[1:]))
        action = command[3]
        remaining = self.failures.get(action, 0)
        if remaining:
            self.failures[action] = remaining - 1
            return 1
        return 0


def workflow_text(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def setup_python_blocks(workflow: str) -> list[str]:
    blocks: list[str] = []
    for block in workflow.split("\n\n"):
        marker = block.find("uses: actions/setup-python@")
        if marker >= 0:
            blocks.append(block[marker:])
    return blocks


class CiScriptTests(unittest.TestCase):
    def test_action_references_require_commit_hashes(self) -> None:
        valid = (
            "steps:\n"
            "  - uses: actions/checkout@"
            + "a" * 40
            + "\n"
        )
        self.assertEqual(verify_actions.verify_text(valid, "workflow.yml"), [])
        self.assertTrue(
            verify_actions.verify_text(
                "steps:\n  - uses: actions/checkout@v4\n",
                "workflow.yml",
            )
        )

    def test_lock_matches_direct_requirements(self) -> None:
        source = "example-package==1.2.3\n"
        lock = (
            "example-package==1.2.3 "
            "--hash=sha256:"
            + "a" * 64
            + "\n"
        )
        verify_lock.verify([source], lock)
        with self.assertRaises(ValueError):
            verify_lock.verify(
                [source],
                "example-package==1.2.3\n",
            )
        with self.assertRaises(ValueError):
            verify_lock.parse_requirements(
                "--index-url https://example.invalid/simple\n",
                require_hashes=False,
            )
        with self.assertRaises(ValueError):
            verify_lock.parse_requirements(
                "example-package==1.2.3 --hash=sha256:short\n",
                require_hashes=True,
            )

    def test_vulnerability_audit_validates_release_versions(self) -> None:
        def clean_release(name: str, version: str) -> dict[str, object]:
            return {
                "info": {"version": version},
                "vulnerabilities": [],
            }

        self.assertEqual(
            audit_lock_vulnerabilities.audit_releases(
                [("example-package", "1.2.3")],
                clean_release,
            ),
            [],
        )
        self.assertEqual(
            audit_lock_vulnerabilities.vulnerability_ids(
                {
                    "vulnerabilities": [
                        {"id": "PYSEC-2"},
                        {
                            "id": "PYSEC-WITHDRAWN",
                            "withdrawn": "2026-07-01T00:00:00Z",
                        },
                        {"id": "PYSEC-1"},
                        {"id": "PYSEC-1"},
                    ]
                }
            ),
            ["PYSEC-1", "PYSEC-2"],
        )
        with self.assertRaises(RuntimeError):
            audit_lock_vulnerabilities.vulnerability_ids(
                {
                    "vulnerabilities": [
                        {"id": "PYSEC-INVALID", "withdrawn": ""},
                    ]
                }
            )

    def test_vulnerability_audit_retries_only_transient_fetch_errors(
        self,
    ) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = (
            b'{"info":{"version":"1.2.3"},"vulnerabilities":[]}'
        )
        with (
            patch(
                "audit_lock_vulnerabilities.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary"), response],
            ) as urlopen_mock,
            patch("audit_lock_vulnerabilities.time.sleep") as sleep_mock,
        ):
            payload = audit_lock_vulnerabilities.fetch_release(
                "example-package",
                "1.2.3",
            )
        self.assertEqual(payload["info"]["version"], "1.2.3")
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.0)

        permanent_error = urllib.error.HTTPError(
            "https://pypi.org/pypi/example-package/1.2.3/json",
            404,
            "Not Found",
            Message(),
            None,
        )
        with (
            patch(
                "audit_lock_vulnerabilities.urllib.request.urlopen",
                side_effect=permanent_error,
            ) as urlopen_mock,
            patch("audit_lock_vulnerabilities.time.sleep") as sleep_mock,
            self.assertRaises(urllib.error.HTTPError),
        ):
            audit_lock_vulnerabilities.fetch_release(
                "example-package",
                "1.2.3",
            )
        urlopen_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_crawler_workflow_uses_public_state_cache(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "crawler.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("path: .runtime/run-state.json", workflow)
        self.assertIn("path: .runtime/public-run-state.json", workflow)
        self.assertIn(
            "NOTION_DATA_SOURCE_ID: ${{ secrets.NOTION_DATA_SOURCE_ID }}",
            workflow,
        )
        self.assertGreaterEqual(
            workflow.count("NOTION_DB_ID: ${{ secrets.NOTION_DB_ID }}"),
            2,
        )
        self.assertIn("existing_pages_migration:", workflow)
        self.assertIn("existing_pages_confirmation:", workflow)
        self.assertIn("recovery_notice_ids:", workflow)
        self.assertIn(
            "MANUAL_NOTICE_RECOVERY_IDS: ${{ inputs.recovery_notice_ids }}",
            workflow,
        )
        self.assertIn('FULL_RECONCILE_LOCAL_HOUR: "7"', workflow)
        self.assertIn("--all-pages", workflow)
        self.assertIn(
            'if [[ -n "$MIGRATION_CONFIRMATION" ]]',
            workflow,
        )
        self.assertIn(
            "github.event_name != 'workflow_dispatch' || "
            "(!inputs.schema_migration && "
            "!inputs.existing_pages_migration)",
            workflow,
        )
        self.assertIn(
            "hashFiles('.runtime/run-state.json') != '' && "
            "hashFiles('.runtime/public-run-state.json') != ''",
            workflow,
        )
        timeout = re.search(r"timeout-minutes:\s*(\d+)", workflow)
        deadline = re.search(
            r'INTERNAL_DEADLINE_SECONDS:\s*"(\d+)"',
            workflow,
        )
        manual_deadline = re.search(
            r"INTERNAL_DEADLINE_SECONDS:\s*\$\{\{"
            r".*?'(\d+)'\s*\|\|\s*env\.INTERNAL_DEADLINE_SECONDS"
            r"\s*\}\}",
            workflow,
        )
        self.assertIsNotNone(timeout)
        self.assertIsNotNone(deadline)
        self.assertIsNotNone(manual_deadline)
        assert timeout is not None
        assert deadline is not None
        assert manual_deadline is not None
        self.assertLess(
            int(deadline.group(1)),
            int(timeout.group(1)) * 60,
        )
        self.assertGreaterEqual(
            int(timeout.group(1)) * 60 - int(deadline.group(1)),
            600,
        )
        self.assertGreater(
            int(manual_deadline.group(1)),
            int(deadline.group(1)),
        )
        self.assertGreaterEqual(
            int(timeout.group(1)) * 60
            - int(manual_deadline.group(1)),
            600,
        )
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && "
            "!inputs.dry_run && !inputs.schema_migration",
            workflow,
        )

    def test_workflows_prevent_spurious_warning_output(self) -> None:
        for workflow_name in ("ci.yml", "crawler.yml"):
            workflow = (
                ROOT / ".github" / "workflows" / workflow_name
            ).read_text(encoding="utf-8")
            self.assertEqual(
                workflow.count(
                    "git config --global init.defaultBranch main"
                ),
                workflow.count("uses: actions/checkout@"),
            )
        ci_workflow = (
            ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'CRAWLER_ACTIONS_ANNOTATIONS: "0"',
            ci_workflow,
        )

    def test_browser_install_skips_system_packages_when_browser_runs(
        self,
    ) -> None:
        runner = RecordingRunner()
        sleep = MagicMock()
        output = io.StringIO()
        with redirect_stdout(output):
            result = install_playwright_browser.install_browser(
                optional=False,
                runner=runner,
                sleep=sleep,
                launch_check=lambda: True,
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            runner.commands,
            [["-m", "playwright", "install", "chromium"]],
        )
        sleep.assert_not_called()
        self.assertNotIn("::", output.getvalue())

    def test_browser_install_adds_system_packages_only_after_launch_fails(
        self,
    ) -> None:
        runner = RecordingRunner()
        launch_results = iter([False, True])
        output = io.StringIO()
        with redirect_stdout(output):
            result = install_playwright_browser.install_browser(
                optional=False,
                runner=runner,
                sleep=MagicMock(),
                launch_check=lambda: next(launch_results),
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            runner.commands,
            [
                ["-m", "playwright", "install", "chromium"],
                ["-m", "playwright", "install-deps", "chromium"],
            ],
        )
        self.assertNotIn("::", output.getvalue())

    def test_browser_install_retries_transient_failures(self) -> None:
        runner = RecordingRunner({"install": 2, "install-deps": 1})
        launch_results = iter([False, True])
        sleep = MagicMock()
        output = io.StringIO()
        with redirect_stdout(output):
            result = install_playwright_browser.install_browser(
                optional=False,
                runner=runner,
                sleep=sleep,
                launch_check=lambda: next(launch_results),
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            [command[2] for command in runner.commands],
            ["install", "install", "install", "install-deps", "install-deps"],
        )
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [
                *install_playwright_browser.DOWNLOAD_RETRY_DELAYS_SECONDS,
                *install_playwright_browser.SYSTEM_DEPENDENCY_RETRY_DELAYS_SECONDS,
            ],
        )

    def test_browser_install_failure_is_fatal_only_when_required(
        self,
    ) -> None:
        scenarios = (
            ({"install": 99}, [True], ["install"] * 3),
            ({"install-deps": 99}, [False], ["install", "install-deps", "install-deps"]),
            ({}, [False, False], ["install", "install-deps"]),
        )
        for failures, launches, expected_actions in scenarios:
            for optional, expected_result, annotation in (
                (False, 1, "::error::"),
                (True, 0, "::warning::"),
            ):
                with self.subTest(failures=failures, optional=optional):
                    runner = RecordingRunner(failures)
                    launch_results = iter(launches)
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = install_playwright_browser.install_browser(
                            optional=optional,
                            runner=runner,
                            sleep=MagicMock(),
                            launch_check=lambda results=launch_results: next(
                                results
                            ),
                        )
                    self.assertEqual(result, expected_result)
                    self.assertEqual(
                        [command[2] for command in runner.commands],
                        expected_actions,
                    )
                    self.assertIn(annotation, output.getvalue())
                    if optional:
                        self.assertIn("API와 HTTP 수집", output.getvalue())

    @unittest.skipUnless(
        browser_tests_required(),
        "Playwright 브라우저 검증이 선택되지 않았습니다",
    )
    def test_browser_launch_check_renders_probe(self) -> None:
        self.assertTrue(install_playwright_browser.browser_launches())

    def test_workflows_survive_transient_setup_failures(self) -> None:
        crawler = workflow_text("crawler.yml")
        ci = workflow_text("ci.yml")
        for workflow in (crawler, ci):
            self.assertNotIn("--with-deps", workflow)
            blocks = setup_python_blocks(workflow)
            self.assertEqual(len(blocks), 3)
            self.assertEqual(len(set(blocks)), 1)
            self.assertIn(
                "if: ${{ steps.setup-python.outcome == 'failure' }}",
                workflow,
            )
            self.assertIn(
                "if: ${{ steps.setup-python-retry.outcome == 'failure' }}",
                workflow,
            )
            self.assertEqual(workflow.count("continue-on-error: true"), 2)
        self.assertIn(
            "run: python .github/scripts/install_playwright_browser.py --optional\n",
            crawler,
        )
        self.assertIn(
            "run: python .github/scripts/install_playwright_browser.py\n",
            ci,
        )
        self.assertIn("--retry-all-errors", ci)

    def test_cache_projection_removes_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "run-state.json"
            destination = Path(temp_dir) / "public-run-state.json"
            scripts_root = ROOT / "scripts"
            sys.path.insert(0, str(scripts_root))
            try:
                import run_state
            finally:
                sys.path.remove(str(scripts_root))
            state = run_state.default_run_state()
            state["operations"] = {
                "operation": {
                    "page_id": "private-page-id",
                    "generation_id": "private-generation-id",
                }
            }
            state["sources"]["2"] = {
                "observed_ids": ["public-notice-id"],
                "error": "private-error",
                "source_circuit_reason": "private-reason",
            }
            state["runs"] = [
                {
                    "run_id": "1",
                    "run_attempt": "1",
                    "execution_id": "1:1",
                    "source_results": [{"error": "private-result"}],
                }
            ]
            incident = run_state.build_incident(
                state,
                run_state.FailureCategory.SOURCE_UPSTREAM,
                "출처 실패",
                "private-incident-summary",
            )
            run_state.mark_failure_signaled(state, incident)
            state["active_incidents"][incident["fingerprint"]][
                "private_summary"
            ] = "private-active-incident"
            state["state_checksum"] = run_state.state_checksum(state)
            run_state.write_run_state_atomic(source, state)

            cache_state.project_state(source, destination)

            text = destination.read_text(encoding="utf-8")
            self.assertNotIn("private-", text)
            self.assertIn("public-notice-id", text)
            self.assertIn(incident["fingerprint"], text)
            self.assertIn("failure_signaled_at", text)


if __name__ == "__main__":
    unittest.main()
