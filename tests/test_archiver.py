from __future__ import annotations

import argparse
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import archiver


class AccountIsolationTests(unittest.TestCase):
    def test_valid_account_names(self) -> None:
        for value in ("personal", "business-2", "mail_3", "0"):
            with self.subTest(value=value):
                self.assertEqual(archiver.validate_account_name(value), value)

    def test_unsafe_account_names_are_rejected(self) -> None:
        for value in ("", "Personal", "../personal", "a/b", "has space", "-bad"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    archiver.validate_account_name(value)

    def test_runtime_paths_are_isolated_by_account(self) -> None:
        personal = archiver.runtime_paths("personal")
        business = archiver.runtime_paths("business")

        self.assertEqual(
            personal.yahoo_config_path,
            Path("/etc/yah-arch/accounts/personal.env"),
        )
        self.assertEqual(
            personal.database_path,
            Path("/var/lib/yah-arch/data/personal.sqlite3"),
        )
        self.assertEqual(
            personal.temp_directory,
            Path("/var/lib/yah-arch/tmp/personal"),
        )
        self.assertEqual(personal.b2_message_prefix, "mail/personal/messages")
        self.assertNotEqual(personal.database_path, business.database_path)
        self.assertNotEqual(personal.b2_message_prefix, business.b2_message_prefix)

    def test_message_object_name_stays_in_account_namespace(self) -> None:
        metadata = archiver.MessageMetadata(
            internal_date=datetime(2026, 8, 30, 21, 52, 46, tzinfo=timezone.utc),
            message_id="<test@example.invalid>",
            sender="sender@example.invalid",
            recipients="recipient@example.invalid",
            subject="Quarterly / report",
        )
        name = archiver.make_object_name(
            metadata,
            "a" * 64,
            "mail/personal/messages",
        )
        self.assertEqual(
            name,
            "mail/personal/messages/2026/08/30/215246_Quarterly-report_"
            + "a" * 64
            + ".eml",
        )


class DatabaseTests(unittest.TestCase):
    def test_database_uses_rollback_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "account.sqlite3"
            connection = archiver.initialize_database(database_path)
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
                self.assertEqual(str(mode).lower(), "delete")
                self.assertEqual(synchronous, 2)
            finally:
                connection.close()


class ArgumentTests(unittest.TestCase):
    def test_account_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            archiver.parse_arguments(["--once"])

    def test_controlled_scan_arguments(self) -> None:
        args = archiver.parse_arguments(
            ["--account", "personal", "--once", "--max-messages", "1"]
        )
        self.assertEqual(args.account, "personal")
        self.assertTrue(args.once)
        self.assertEqual(args.max_messages, 1)


if __name__ == "__main__":
    unittest.main()
