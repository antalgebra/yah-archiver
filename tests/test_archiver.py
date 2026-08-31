from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import archiver


class FakeImapClient:
    def __init__(self, uids: list[int], uidvalidity: int = 77) -> None:
        self.uids = uids
        self.uidvalidity = uidvalidity

    def select(self, _mailbox: str, readonly: bool = False):
        if not readonly:
            raise AssertionError("mailbox must be selected read-only")
        return "OK", [str(len(self.uids)).encode("ascii")]

    def response(self, name: str):
        if name != "UIDVALIDITY":
            raise AssertionError(f"unexpected response request: {name}")
        return "OK", [str(self.uidvalidity).encode("ascii")]

    def uid(self, command: str, *_arguments):
        if command != "SEARCH":
            raise AssertionError(f"unexpected UID command: {command}")
        values = " ".join(str(uid) for uid in self.uids).encode("ascii")
        return "OK", [values]


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
            "Sent",
        )
        self.assertEqual(
            name,
            "mail/personal/messages/2026/2026-08-30_215246_Sent_Quarterly-report_"
            + "a" * 16
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

    def test_legacy_folder_state_is_migrated_without_losing_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(database_path)
            legacy.execute(
                "CREATE TABLE folder_state ("
                "folder TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL, "
                "last_uid INTEGER NOT NULL DEFAULT 0, checked_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO folder_state VALUES ('INBOX', 10, 42, 'old')"
            )
            legacy.commit()
            legacy.close()

            connection = archiver.initialize_database(database_path)
            try:
                row = connection.execute(
                    "SELECT live_cursor_uid, backfill_before_uid, "
                    "backfill_complete FROM folder_state WHERE folder = 'INBOX'"
                ).fetchone()
                self.assertEqual(row["live_cursor_uid"], 42)
                self.assertEqual(row["backfill_before_uid"], 1)
                self.assertEqual(row["backfill_complete"], 1)
            finally:
                connection.close()


class ScanOrderingTests(unittest.TestCase):
    def test_high_risk_mailboxes_are_ordered_first(self) -> None:
        client = mock.Mock()
        client.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "Archive"',
                b'(\\HasNoChildren \\Sent) "/" "Sent"',
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Trash) "/" "Trash"',
            ],
        )
        mailboxes = archiver.list_selectable_mailboxes(client)
        self.assertEqual(
            [mailbox.key for mailbox in mailboxes],
            ["Trash", "INBOX", "Sent", "Archive"],
        )

    def test_initial_backfill_is_newest_first_then_live_mail_is_contiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(Path(directory) / "scan.sqlite3")
            client = FakeImapClient([2, 5, 9])
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            successful = archiver.MessageAttempt(
                uploaded=True,
                retryable_failure=False,
            )
            try:
                with mock.patch.object(
                    archiver, "attempt_message", return_value=successful
                ) as attempt:
                    result = archiver.archive_mailbox(
                        client,
                        connection,
                        object(),
                        paths,
                        mailbox,
                        None,
                        2,
                    )
                    self.assertEqual(result, (2, 2))
                    self.assertEqual(
                        [call.args[-1] for call in attempt.call_args_list],
                        [9, 5],
                    )

                row = connection.execute(
                    "SELECT live_cursor_uid, backfill_before_uid, "
                    "backfill_complete FROM folder_state WHERE folder = 'INBOX'"
                ).fetchone()
                self.assertEqual(row["live_cursor_uid"], 9)
                self.assertEqual(row["backfill_before_uid"], 5)
                self.assertEqual(row["backfill_complete"], 0)

                with mock.patch.object(
                    archiver, "attempt_message", return_value=successful
                ) as attempt:
                    archiver.archive_mailbox(
                        client,
                        connection,
                        object(),
                        paths,
                        mailbox,
                        None,
                        2,
                    )
                    self.assertEqual(
                        [call.args[-1] for call in attempt.call_args_list],
                        [2],
                    )

                client.uids = [2, 5, 9, 10, 12]
                with mock.patch.object(
                    archiver, "attempt_message", return_value=successful
                ) as attempt:
                    archiver.archive_mailbox(
                        client,
                        connection,
                        object(),
                        paths,
                        mailbox,
                        None,
                        2,
                    )
                    self.assertEqual(
                        [call.args[-1] for call in attempt.call_args_list],
                        [10, 12],
                    )

                row = connection.execute(
                    "SELECT live_cursor_uid, backfill_complete "
                    "FROM folder_state WHERE folder = 'INBOX'"
                ).fetchone()
                self.assertEqual(row["live_cursor_uid"], 12)
                self.assertEqual(row["backfill_complete"], 1)
            finally:
                connection.close()


class FailureIsolationTests(unittest.TestCase):
    def test_one_failed_message_does_not_block_the_next_uid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(Path(directory) / "continue.sqlite3")
            client = FakeImapClient([5, 9])
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            failed = archiver.MessageAttempt(
                uploaded=False,
                retryable_failure=True,
            )
            successful = archiver.MessageAttempt(
                uploaded=True,
                retryable_failure=False,
            )
            try:
                with mock.patch.object(
                    archiver,
                    "attempt_message",
                    side_effect=[failed, successful],
                ) as attempt:
                    result = archiver.archive_mailbox(
                        client,
                        connection,
                        object(),
                        paths,
                        mailbox,
                        None,
                        10,
                    )
                    self.assertEqual(result, (2, 1))
                    self.assertEqual(
                        [call.args[-1] for call in attempt.call_args_list],
                        [9, 5],
                    )
            finally:
                connection.close()

    def test_message_specific_failure_is_recorded_and_later_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(Path(directory) / "fail.sqlite3")
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            try:
                with (
                    mock.patch.object(
                        archiver,
                        "fetch_raw_message",
                        return_value=(b'meta INTERNALDATE "30-Aug-2026 12:00:00 +0000"', b"raw"),
                    ),
                    mock.patch.object(
                        archiver,
                        "archive_message",
                        side_effect=[ValueError("bad MIME"), True],
                    ),
                ):
                    with self.assertLogs("yah-arch", level="ERROR"):
                        first = archiver.attempt_message(
                            object(), connection, object(), paths, mailbox, 77, 9
                        )
                    self.assertTrue(first.retryable_failure)
                    row = connection.execute(
                        "SELECT attempts, retryable, last_error FROM message_failures"
                    ).fetchone()
                    self.assertEqual(row["attempts"], 1)
                    self.assertEqual(row["retryable"], 1)
                    self.assertIn("bad MIME", row["last_error"])

                    second = archiver.attempt_message(
                        object(), connection, object(), paths, mailbox, 77, 9
                    )
                    self.assertTrue(second.uploaded)
                    remaining = connection.execute(
                        "SELECT COUNT(*) FROM message_failures"
                    ).fetchone()[0]
                    self.assertEqual(remaining, 0)
            finally:
                connection.close()


class ArgumentTests(unittest.TestCase):
    def test_account_is_required(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
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
