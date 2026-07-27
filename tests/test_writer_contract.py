import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import main as crawler_main
from settings import is_writer_context_confirmed, should_run_dry_run


class WriteAuthorizationTests(unittest.TestCase):
    authorized_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "example/notices",
        "GITHUB_WORKFLOW_REF": (
            "example/notices/"
            ".github/workflows/crawler.yml@refs/heads/main"
        ),
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": "a" * 40,
    }

    def test_dry_run_is_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(should_run_dry_run())

    def test_invalid_dry_run_value_fails_closed(self):
        with (
            patch.dict(
                os.environ,
                {"SYNC_DRY_RUN": "treu"},
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            should_run_dry_run()

    def test_writer_context_requires_matching_repository_and_crawler_workflow(
        self,
    ):
        with patch.dict(
            os.environ,
            self.authorized_environment,
            clear=True,
        ):
            self.assertTrue(is_writer_context_confirmed())
            with patch.dict(
                os.environ,
                {
                    "GITHUB_WORKFLOW_REF": (
                        "example/notices/"
                        ".github/workflows/untrusted.yml"
                        "@refs/heads/main"
                    ),
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                },
            ):
                self.assertFalse(is_writer_context_confirmed())
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "another/project",
                    "GITHUB_WORKFLOW_REF": (
                        "another/project/.github/workflows/crawler.yml"
                        "@refs/heads/main"
                    ),
                },
            ):
                self.assertTrue(is_writer_context_confirmed())
            for name, invalid in (
                ("GITHUB_REPOSITORY", ""),
                (
                    "GITHUB_WORKFLOW_REF",
                    "attacker/fork/.github/workflows/crawler.yml"
                    "@refs/heads/main",
                ),
                ("GITHUB_REF", "refs/heads/feature"),
                ("GITHUB_EVENT_NAME", "pull_request"),
                ("GITHUB_RUN_ID", "not-a-number"),
                ("GITHUB_RUN_ATTEMPT", "0"),
                ("GITHUB_SHA", "abc"),
            ):
                with self.subTest(name=name):
                    with patch.dict(os.environ, {name: invalid}):
                        self.assertFalse(is_writer_context_confirmed())

    def test_dry_run_is_allowed_without_writer_context(self):
        with patch.object(
            crawler_main,
            "is_writer_context_confirmed",
            return_value=False,
        ):
            crawler_main.validate_destination_write_authorization(
                True,
                False,
            )

    def test_all_write_modes_require_writer_context(self):
        for schema_migration_only in (True, False):
            with self.subTest(
                schema_migration_only=schema_migration_only,
            ):
                with patch.object(
                    crawler_main,
                    "is_writer_context_confirmed",
                    return_value=False,
                ):
                    with self.assertRaises(
                        crawler_main.LocalConfigurationError
                    ):
                        crawler_main.validate_destination_write_authorization(
                            False,
                            schema_migration_only,
                        )

    def test_schema_migration_is_allowed_in_writer_context(self):
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "workflow_dispatch"},
            ),
            patch.object(
                crawler_main,
                "is_writer_context_confirmed",
                return_value=True,
            ),
        ):
            crawler_main.validate_destination_write_authorization(
                False,
                True,
            )

    def test_schema_migration_requires_manual_dispatch(self):
        with (
            patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "schedule"},
            ),
            patch.object(
                crawler_main,
                "is_writer_context_confirmed",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(
                crawler_main.LocalConfigurationError,
                "수동 GitHub Actions",
            ):
                crawler_main.validate_destination_write_authorization(
                    False,
                    True,
                )

    def test_normal_sync_needs_no_extra_enable_variable(self):
        with patch.object(
            crawler_main,
            "is_writer_context_confirmed",
            return_value=True,
        ):
            crawler_main.validate_destination_write_authorization(
                False,
                False,
            )

    def test_missing_writer_context_performs_no_external_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            collect = Mock()
            prepare = Mock()
            apply = Mock()
            with patch.dict(
                os.environ,
                {
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                },
                clear=True,
            ):
                with patch.multiple(
                    crawler_main,
                    setup_logging=Mock(),
                    load_dotenv=Mock(),
                    log_environment_info=Mock(),
                    install_run_control=Mock(),
                    should_run_dry_run=Mock(return_value=False),
                    should_run_notion_schema_migration_only=Mock(
                        return_value=False
                    ),
                    should_use_incremental_crawl=Mock(
                        return_value=True
                    ),
                    should_full_reconcile=Mock(return_value=False),
                    get_bbs_config_fks=Mock(
                        return_value=["141", "2"]
                    ),
                    get_run_state_path=Mock(
                        return_value=root / "run-state.json"
                    ),
                    get_snapshot_path=Mock(
                        return_value=root / "snapshot.json"
                    ),
                    get_incident_path=Mock(
                        return_value=root / "incident.json"
                    ),
                    collect_report=collect,
                    prepare_destination=prepare,
                    apply_report=apply,
                ):
                    with self.assertRaises(
                        crawler_main.LocalConfigurationError
                    ):
                        crawler_main.main()

            collect.assert_not_called()
            prepare.assert_not_called()
            apply.assert_not_called()

    def test_missing_destination_credentials_perform_no_external_request(self):
        credential_sets = {
            "token": {"NOTION_TOKEN": "token"},
            "database": {"NOTION_DB_ID": "database"},
        }
        for label, credentials in credential_sets.items():
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                collect = Mock()
                prepare = Mock()
                apply = Mock()
                with patch.dict(
                    os.environ,
                    credentials,
                    clear=True,
                ):
                    with patch.multiple(
                        crawler_main,
                        setup_logging=Mock(),
                        load_dotenv=Mock(),
                        log_environment_info=Mock(),
                        install_run_control=Mock(),
                        is_writer_context_confirmed=Mock(
                            return_value=True
                        ),
                        should_run_dry_run=Mock(return_value=False),
                        should_run_notion_schema_migration_only=Mock(
                            return_value=False
                        ),
                        should_use_incremental_crawl=Mock(
                            return_value=True
                        ),
                        should_full_reconcile=Mock(return_value=False),
                        get_bbs_config_fks=Mock(
                            return_value=["141", "2"]
                        ),
                        get_run_state_path=Mock(
                            return_value=root / "run-state.json"
                        ),
                        get_snapshot_path=Mock(
                            return_value=root / "snapshot.json"
                        ),
                        get_incident_path=Mock(
                            return_value=root / "incident.json"
                        ),
                        collect_report=collect,
                        prepare_destination=prepare,
                        apply_report=apply,
                    ):
                        with self.assertRaises(
                            crawler_main.LocalConfigurationError
                        ):
                            crawler_main.main()

                collect.assert_not_called()
                prepare.assert_not_called()
                apply.assert_not_called()

    def test_schema_migration_runs_without_extra_enable_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prepare = Mock()
            collect = Mock()
            with patch.dict(
                os.environ,
                {
                    **self.authorized_environment,
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "NOTION_TOKEN": "token",
                    "NOTION_DB_ID": "database",
                },
                clear=True,
            ):
                with patch.multiple(
                    crawler_main,
                    setup_logging=Mock(),
                    load_dotenv=Mock(),
                    log_environment_info=Mock(),
                    install_run_control=Mock(),
                    should_run_dry_run=Mock(return_value=False),
                    should_run_notion_schema_migration_only=Mock(
                        return_value=True
                    ),
                    should_use_incremental_crawl=Mock(
                        return_value=True
                    ),
                    should_allow_notion_schema_migration=Mock(
                        return_value=True
                    ),
                    should_full_reconcile=Mock(return_value=False),
                    get_bbs_config_fks=Mock(
                        return_value=["141", "2"]
                    ),
                    get_run_state_path=Mock(
                        return_value=root / "run-state.json"
                    ),
                    get_snapshot_path=Mock(
                        return_value=root / "snapshot.json"
                    ),
                    get_incident_path=Mock(
                        return_value=root / "incident.json"
                    ),
                    prepare_destination=prepare,
                    collect_report=collect,
                ):
                    crawler_main.main()

            prepare.assert_called_once_with(
                "token",
                "database",
                [],
                recover_pending=False,
            )
            collect.assert_not_called()
            state = json.loads(
                (root / "run-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                state["runs"][-1]["status"],
                "schema_migration_succeeded",
            )


if __name__ == "__main__":
    unittest.main()
