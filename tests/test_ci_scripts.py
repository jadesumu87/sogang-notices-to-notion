import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_lock_vulnerabilities
import cache_state
import verify_actions
import verify_lock


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
