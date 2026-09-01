from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import archiver


class FakeImapClient:
    def __init__(self, uids: list[int], uidvalidity: int = 77) -> None:
        self.uids = uids
        self.uidvalidity = uidvalidity
        self.uidnext = max(uids, default=0) + 1
        self.search_arguments: list[tuple[object, ...]] = []

    def select(self, _mailbox: str, readonly: bool = False):
        if not readonly:
            raise AssertionError("mailbox must be selected read-only")
        return "OK", [str(len(self.uids)).encode("ascii")]

    def response(self, name: str):
        values = {
            "UIDVALIDITY": self.uidvalidity,
            "UIDNEXT": self.uidnext,
        }
        if name not in values:
            raise AssertionError(f"unexpected response request: {name}")
        value = values[name]
        return "OK", [None if value is None else str(value).encode("ascii")]

    def uid(self, command: str, *arguments):
        if command != "SEARCH":
            raise AssertionError(f"unexpected UID command: {command}")
        self.search_arguments.append(arguments)
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


class MessageBodyPreviewTests(unittest.TestCase):
    def test_plain_text_is_normalized_and_bounded(self) -> None:
        body = (
            "  This is a body preview with extra   whitespace,\n"
            "controls\x00, and more than fifty characters.  "
        )
        raw_message = (
            b"From: sender@example.invalid\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            + body.encode("utf-8")
        )

        preview = archiver.message_body_preview(raw_message)

        self.assertEqual(
            preview,
            "This is a body preview with extra whitespace, cont",
        )
        self.assertEqual(len(preview), 50)

    def test_multipart_prefers_plain_text_and_ignores_attachments(self) -> None:
        raw_message = b"\r\n".join(
            (
                b"MIME-Version: 1.0",
                b"Content-Type: multipart/mixed; boundary=outer",
                b"",
                b"--outer",
                b"Content-Type: text/plain; name=decoy.txt",
                b"Content-Disposition: attachment; filename=decoy.txt",
                b"",
                b"Attachment text must not appear.",
                b"--outer",
                b"Content-Type: multipart/alternative; boundary=alternative",
                b"",
                b"--alternative",
                b"Content-Type: text/html; charset=utf-8",
                b"",
                b"<p>HTML alternative must not win.</p>",
                b"--alternative",
                b"Content-Type: text/plain; charset=utf-8",
                b"",
                b"Preferred plain body text wins over HTML.",
                b"--alternative--",
                b"--outer--",
                b"",
            )
        )

        self.assertEqual(
            archiver.message_body_preview(raw_message),
            "Preferred plain body text wins over HTML.",
        )

    def test_html_fallback_keeps_only_visible_text(self) -> None:
        raw_message = b"\r\n".join(
            (
                b"MIME-Version: 1.0",
                b"Content-Type: text/html; charset=utf-8",
                b"",
                b"<html><head><style>hidden css</style></head>",
                b"<body>Hello&nbsp;<b>team</b> &amp; welcome.",
                b"<script>hidden script</script></body></html>",
            )
        )

        self.assertEqual(
            archiver.message_body_preview(raw_message),
            "Hello team & welcome.",
        )

    def test_new_archive_object_caches_body_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "body-preview.sqlite3"
            )
            paths = replace(
                archiver.runtime_paths("personal"),
                temp_directory=Path(directory) / "tmp",
            )
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            raw_message = (
                b"Subject: Test\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                b"This newly fetched body is cached for later alerts."
            )
            bucket = mock.Mock()

            def upload(local_path, object_name, **kwargs):
                data = Path(local_path).read_bytes()
                return mock.Mock(
                    id_="file-1",
                    content_sha1=kwargs["sha1_sum"],
                    file_name=object_name,
                    size=len(data),
                    file_info=kwargs["file_info"],
                )

            bucket.upload_local_file.side_effect = upload
            try:
                uploaded = archiver.archive_message(
                    connection,
                    bucket,
                    paths,
                    mailbox,
                    77,
                    1,
                    b'INTERNALDATE "01-Sep-2026 00:00:00 +0000"',
                    raw_message,
                    "destination-1",
                )

                row = connection.execute(
                    "SELECT body_preview FROM archive_objects"
                ).fetchone()
                self.assertTrue(uploaded)
                self.assertEqual(
                    row["body_preview"],
                    "This newly fetched body is cached for later alerts",
                )
            finally:
                connection.close()


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

    def test_legacy_archive_objects_add_body_preview_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy-archive.sqlite3"
            legacy = sqlite3.connect(database_path)
            legacy.execute(
                "CREATE TABLE archive_objects ("
                "sha256 TEXT PRIMARY KEY, object_name TEXT NOT NULL UNIQUE, "
                "size_bytes INTEGER NOT NULL, b2_file_id TEXT NOT NULL, "
                "b2_sha1 TEXT NOT NULL, archived_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO archive_objects VALUES "
                "(?, 'mail/object.eml', 10, 'file-1', ?, 'old')",
                ("a" * 64, "b" * 40),
            )
            legacy.commit()
            legacy.close()

            connection = archiver.initialize_database(database_path)
            try:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(archive_objects)"
                    ).fetchall()
                }
                row = connection.execute(
                    "SELECT object_name, body_preview FROM archive_objects"
                ).fetchone()
                self.assertIn("body_preview", columns)
                self.assertEqual(row["object_name"], "mail/object.eml")
                self.assertIsNone(row["body_preview"])
            finally:
                connection.close()

    def test_legacy_presence_schema_adds_junk_lineage_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "presence.sqlite3"
            legacy = sqlite3.connect(database_path)
            legacy.executescript(
                """
                CREATE TABLE presence_folders (
                    folder TEXT PRIMARY KEY,
                    uidvalidity INTEGER NOT NULL,
                    is_trash INTEGER NOT NULL,
                    present INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing_clean_scans INTEGER NOT NULL DEFAULT 0,
                    disappeared_at TEXT
                );
                INSERT INTO presence_folders VALUES (
                    'INBOX', 77, 0, 1, 'first', 'last', 0, NULL
                );
                CREATE TABLE message_presence (
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    sha256 TEXT,
                    is_trash INTEGER NOT NULL,
                    present INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    missing_clean_scans INTEGER NOT NULL DEFAULT 0,
                    disappeared_at TEXT,
                    PRIMARY KEY (folder, uidvalidity, uid)
                );
                INSERT INTO message_presence VALUES (
                    'INBOX', 77, 1, NULL, 0, 1, 'first', 'last', 0, NULL
                );
                """
            )
            legacy.commit()
            legacy.close()

            connection = archiver.initialize_database(
                database_path
            )
            try:
                for table in ("presence_folders", "message_presence"):
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    self.assertIn("is_junk", columns)
                    row = connection.execute(
                        f"SELECT is_junk, present FROM {table}"
                    ).fetchone()
                    self.assertEqual(row["is_junk"], 0)
                    self.assertEqual(row["present"], 1)
                indexes = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA index_list(message_presence)"
                    ).fetchall()
                }
                self.assertIn("message_presence_junk_sha_idx", indexes)
                self.assertIn("message_presence_current_folder_idx", indexes)
                audit_indexes = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA index_list(audit_events)"
                    ).fetchall()
                }
                self.assertIn("audit_events_unalerted_idx", audit_indexes)
            finally:
                connection.close()

    def test_archived_hash_lookup_batches_large_uid_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "hash-batches.sqlite3"
            )
            sha256 = "a" * 64
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO archive_objects("
                        "sha256, object_name, size_bytes, b2_file_id, b2_sha1, "
                        "archived_at) VALUES "
                        "(?, 'mail/object.eml', 10, 'file-1', ?, 'now')",
                        (sha256, "b" * 40),
                    )
                    connection.executemany(
                        "INSERT INTO imap_messages VALUES "
                        "('INBOX', 77, ?, ?, 'now', '', '', '', '', 'now')",
                        ((1, sha256), (901, sha256)),
                    )

                statements: list[str] = []
                connection.set_trace_callback(statements.append)
                result = archiver.archived_message_sha256s(
                    connection,
                    "INBOX",
                    77,
                    tuple(range(1, 1001)),
                )
                connection.set_trace_callback(None)

                self.assertEqual(result, {1: sha256, 901: sha256})
                selects = [
                    statement
                    for statement in statements
                    if statement.startswith("SELECT uid, sha256")
                ]
                self.assertEqual(len(selects), 2)
            finally:
                connection.close()


class ScanOrderingTests(unittest.TestCase):
    @staticmethod
    def seed_completed_folder(
        connection: sqlite3.Connection,
        *,
        folder: str = "INBOX",
        uidvalidity: int = 77,
        live_cursor_uid: int,
    ) -> None:
        with connection:
            connection.execute(
                "INSERT INTO folder_state("
                "folder, uidvalidity, last_uid, live_cursor_uid, "
                "backfill_before_uid, backfill_complete, checked_at"
                ") VALUES (?, ?, ?, ?, 1, 1, 'old')",
                (folder, uidvalidity, live_cursor_uid, live_cursor_uid),
            )

    def test_high_risk_mailboxes_are_ordered_first(self) -> None:
        client = mock.Mock()
        client.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "Archive"',
                b'(\\HasNoChildren \\Sent) "/" "Sent"',
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Trash) "/" "Trash"',
                b'(\\HasNoChildren \\Junk) "/" "Bulk Mail"',
                b'(\\HasNoChildren) "/" "Spam"',
            ],
        )
        mailboxes = archiver.list_selectable_mailboxes(client)
        self.assertEqual(
            [mailbox.key for mailbox in mailboxes],
            ["Trash", "Bulk Mail", "Spam", "INBOX", "Sent", "Archive"],
        )
        self.assertTrue(archiver.mailbox_is_priority(mailboxes[1]))
        self.assertTrue(archiver.mailbox_is_junk(mailboxes[1]))
        self.assertTrue(archiver.mailbox_is_junk(mailboxes[2]))

    def test_junk_name_fallback_does_not_match_unrelated_folders(self) -> None:
        for folder in ("Bulk invoices", "Junkyard", "Spam reports"):
            with self.subTest(folder=folder):
                mailbox = archiver.Mailbox(folder, f'"{folder}"', frozenset())
                self.assertFalse(archiver.mailbox_is_junk(mailbox))

        quarantine = archiver.Mailbox(
            "Quarantine", '"Quarantine"', frozenset({"\\JUNK"})
        )
        self.assertTrue(archiver.mailbox_is_junk(quarantine))

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
                        date(2026, 8, 31),
                        "destination-1",
                    )
                    self.assertEqual(result, (2, 2))
                    self.assertEqual(
                        [call.args[-2] for call in attempt.call_args_list],
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
                        date(2026, 8, 31),
                        "destination-1",
                    )
                    self.assertEqual(
                        [call.args[-2] for call in attempt.call_args_list],
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
                        date(2026, 8, 31),
                        "destination-1",
                    )
                    self.assertEqual(
                        [call.args[-2] for call in attempt.call_args_list],
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

    def test_completed_folder_uses_incremental_search_on_priority_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "incremental.sqlite3"
            )
            client = FakeImapClient([2, 5, 9, 10, 12])
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            scans: list[archiver.MailboxScan] = []
            successful = archiver.MessageAttempt(
                uploaded=True,
                retryable_failure=False,
            )
            try:
                self.seed_completed_folder(connection, live_cursor_uid=9)
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
                        10,
                        date(2026, 8, 31),
                        "destination-1",
                        scans,
                        incremental_search=True,
                    )

                self.assertEqual(
                    client.search_arguments,
                    [
                        (
                            None,
                            "SINCE",
                            "31-Aug-2026",
                            "UID",
                            "10:4294967295",
                        )
                    ],
                )
                self.assertEqual(
                    [call.args[-2] for call in attempt.call_args_list],
                    [10, 12],
                )
                self.assertEqual(scans[0].current_uids, (10, 12))
                self.assertFalse(scans[0].complete_snapshot)
            finally:
                connection.close()

    def test_full_scan_keeps_complete_uid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "full-snapshot.sqlite3"
            )
            client = FakeImapClient([2, 5, 9])
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            scans: list[archiver.MailboxScan] = []
            try:
                self.seed_completed_folder(connection, live_cursor_uid=9)
                archiver.archive_mailbox(
                    client,
                    connection,
                    object(),
                    paths,
                    mailbox,
                    None,
                    10,
                    date(2026, 8, 31),
                    "destination-1",
                    scans,
                    incremental_search=False,
                )

                self.assertEqual(
                    client.search_arguments,
                    [(None, "SINCE", "31-Aug-2026")],
                )
                self.assertEqual(scans[0].current_uids, (2, 5, 9))
                self.assertTrue(scans[0].complete_snapshot)
            finally:
                connection.close()

    def test_idle_priority_pass_uses_uidnext_to_skip_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "uidnext.sqlite3"
            )
            client = FakeImapClient([2, 5, 9])
            mailbox = archiver.Mailbox("INBOX", '"INBOX"', frozenset())
            paths = archiver.runtime_paths("personal")
            scans: list[archiver.MailboxScan] = []
            try:
                self.seed_completed_folder(connection, live_cursor_uid=12)
                archiver.archive_mailbox(
                    client,
                    connection,
                    object(),
                    paths,
                    mailbox,
                    None,
                    10,
                    date(2026, 8, 31),
                    "destination-1",
                    scans,
                    incremental_search=True,
                )

                self.assertEqual(client.search_arguments, [])
                self.assertEqual(scans[0].current_uids, ())
                self.assertFalse(scans[0].complete_snapshot)
            finally:
                connection.close()

    def test_incremental_search_stops_at_protocol_uid_limit(self) -> None:
        client = FakeImapClient([archiver.MAX_IMAP_UID])
        result = archiver.search_all_uids(
            client,
            date(2026, 8, 31),
            archiver.MAX_IMAP_UID + 1,
        )
        self.assertEqual(result, [])
        self.assertEqual(client.search_arguments, [])


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
                        date(2026, 8, 31),
                        "destination-1",
                    )
                    self.assertEqual(result, (2, 1))
                    self.assertEqual(
                        [call.args[-2] for call in attempt.call_args_list],
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
                            object(),
                            connection,
                            object(),
                            paths,
                            mailbox,
                            77,
                            9,
                            "destination-1",
                        )
                    self.assertTrue(first.retryable_failure)
                    row = connection.execute(
                        "SELECT attempts, retryable, last_error FROM message_failures"
                    ).fetchone()
                    self.assertEqual(row["attempts"], 1)
                    self.assertEqual(row["retryable"], 1)
                    self.assertIn("bad MIME", row["last_error"])

                    second = archiver.attempt_message(
                        object(),
                        connection,
                        object(),
                        paths,
                        mailbox,
                        77,
                        9,
                        "destination-1",
                    )
                    self.assertTrue(second.uploaded)
                    remaining = connection.execute(
                        "SELECT COUNT(*) FROM message_failures"
                    ).fetchone()[0]
                    self.assertEqual(remaining, 0)
            finally:
                connection.close()

    def test_junk_disappeared_before_fetch_is_recorded_without_alerting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "junk-fetch.sqlite3"
            )
            mailbox = archiver.Mailbox(
                "Bulk Mail", '"Bulk Mail"', frozenset({"\\JUNK"})
            )
            paths = archiver.runtime_paths("personal")
            try:
                with mock.patch.object(
                    archiver, "fetch_raw_message", return_value=None
                ):
                    with self.assertLogs("yah-arch", level="WARNING"):
                        result = archiver.attempt_message(
                            object(),
                            connection,
                            object(),
                            paths,
                            mailbox,
                            77,
                            9,
                            "destination-1",
                        )
                self.assertFalse(result.retryable_failure)
                self.assertEqual(
                    connection.execute(
                        "SELECT failure_kind FROM message_failures"
                    ).fetchone()["failure_kind"],
                    "disappeared_before_fetch",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_events"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()


class PresenceReconciliationTests(unittest.TestCase):
    SHA256 = "a" * 64

    @staticmethod
    def scan(
        folder: str,
        uids: tuple[int, ...],
        *,
        uidvalidity: int = 77,
        is_trash: bool = False,
        is_junk: bool = False,
        complete_snapshot: bool = True,
        refreshed_uids: tuple[int, ...] = (),
    ) -> archiver.MailboxScan:
        return archiver.MailboxScan(
            attempted=0,
            uploaded=0,
            folder=folder,
            uidvalidity=uidvalidity,
            current_uids=uids,
            is_trash=is_trash,
            is_junk=is_junk,
            complete_snapshot=complete_snapshot,
            refreshed_uids=refreshed_uids,
        )

    def record_occurrence(
        self,
        connection: sqlite3.Connection,
        folder: str,
        uid: int,
        uidvalidity: int = 77,
    ) -> None:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO archive_objects("
                "sha256, object_name, size_bytes, b2_file_id, b2_sha1, archived_at"
                ") VALUES (?, ?, 10, 'file-1', ?, '2026-09-01T00:00:00+00:00')",
                (self.SHA256, f"mail/{self.SHA256}.eml", "b" * 40),
            )
            connection.execute(
                "INSERT INTO imap_messages("
                "folder, uidvalidity, uid, sha256, internal_date, message_id, "
                "sender, recipients, subject, first_seen_at"
                ") VALUES (?, ?, ?, ?, '2026-09-01T00:00:00+00:00', "
                "'<same@example.invalid>', 'from@example.invalid', "
                "'to@example.invalid', 'Test', '2026-09-01T00:00:00+00:00')",
                (folder, uidvalidity, uid, self.SHA256),
            )

    def test_inbox_to_junk_to_trash_to_deleted_creates_no_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "junk-chain.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "INBOX", 1)
                self.record_occurrence(connection, "Bulk Mail", 2)
                self.record_occurrence(connection, "Trash", 3)

                def reconcile(
                    inbox: tuple[int, ...],
                    junk: tuple[int, ...],
                    trash: tuple[int, ...],
                ) -> None:
                    archiver.reconcile_presence(
                        connection,
                        paths,
                        [
                            self.scan("Trash", trash, is_trash=True),
                            self.scan("Bulk Mail", junk, is_junk=True),
                            self.scan("INBOX", inbox),
                        ],
                    )

                reconcile((1,), (), ())
                reconcile((), (2,), ())
                reconcile((), (2,), ())
                reconcile((), (), (3,))
                reconcile((), (), (3,))
                reconcile((), (), ())
                reconcile((), (), ())

                events = connection.execute(
                    "SELECT event_type FROM audit_events"
                ).fetchall()
                self.assertEqual(events, [])
            finally:
                connection.close()

    def test_junk_to_inbox_rescue_restores_trash_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "rescued-chain.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Bulk Mail", 2)
                self.record_occurrence(connection, "INBOX", 4)
                self.record_occurrence(connection, "Trash", 5)

                def reconcile(
                    inbox: tuple[int, ...],
                    junk: tuple[int, ...],
                    trash: tuple[int, ...],
                ) -> None:
                    archiver.reconcile_presence(
                        connection,
                        paths,
                        [
                            self.scan("Trash", trash, is_trash=True),
                            self.scan("Bulk Mail", junk, is_junk=True),
                            self.scan("INBOX", inbox),
                        ],
                    )

                reconcile((), (2,), ())
                with connection:
                    connection.execute(
                        "UPDATE message_presence SET first_seen_at = ? "
                        "WHERE folder = 'Bulk Mail' AND uid = 2",
                        ("2026-09-01T00:00:01.000000+00:00",),
                    )

                reconcile((4,), (), ())
                with connection:
                    connection.execute(
                        "UPDATE message_presence SET first_seen_at = ? "
                        "WHERE folder = 'INBOX' AND uid = 4",
                        ("2026-09-01T00:00:02.000000+00:00",),
                    )
                self.assertFalse(
                    archiver.content_is_junk_suppressed(
                        connection, self.SHA256
                    )
                )

                reconcile((), (), (5,))
                reconcile((), (), (5,))
                reconcile((), (), ())
                reconcile((), (), ())

                events = connection.execute(
                    "SELECT event_type, alerted_at FROM audit_events"
                ).fetchall()
                self.assertEqual(
                    {row["event_type"] for row in events},
                    {"trash_observed", "trash_disappeared"},
                )
                self.assertTrue(
                    all(row["alerted_at"] is None for row in events)
                )
            finally:
                connection.close()

    def test_junk_to_inbox_rescue_restores_direct_deletion_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "rescued-direct-delete.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Bulk Mail", 2)
                self.record_occurrence(connection, "INBOX", 4)

                def reconcile(
                    inbox: tuple[int, ...], junk: tuple[int, ...]
                ) -> None:
                    archiver.reconcile_presence(
                        connection,
                        paths,
                        [
                            self.scan("Bulk Mail", junk, is_junk=True),
                            self.scan("INBOX", inbox),
                        ],
                    )

                reconcile((), (2,))
                with connection:
                    connection.execute(
                        "UPDATE message_presence SET first_seen_at = ? "
                        "WHERE folder = 'Bulk Mail' AND uid = 2",
                        ("2026-09-01T00:00:01.000000+00:00",),
                    )
                reconcile((4,), ())
                with connection:
                    connection.execute(
                        "UPDATE message_presence SET first_seen_at = ? "
                        "WHERE folder = 'INBOX' AND uid = 4",
                        ("2026-09-01T00:00:02.000000+00:00",),
                    )

                reconcile((), ())
                reconcile((), ())

                events = connection.execute(
                    "SELECT event_type, alerted_at FROM audit_events"
                ).fetchall()
                self.assertEqual(
                    [row["event_type"] for row in events],
                    ["unexplained_disappearance"],
                )
                self.assertIsNone(events[0]["alerted_at"])
            finally:
                connection.close()

    def test_same_snapshot_junk_correlation_is_scan_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "same-snapshot.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Bulk Mail", 2)
                self.record_occurrence(connection, "Trash", 3)
                baseline = [
                    self.scan("Trash", (), is_trash=True),
                    self.scan("Bulk Mail", (), is_junk=True),
                ]
                archiver.reconcile_presence(connection, paths, baseline)

                # Trash deliberately comes first. Correlation must use the
                # complete snapshot rather than depending on scan order.
                moved = [
                    self.scan("Trash", (3,), is_trash=True),
                    self.scan("Bulk Mail", (2,), is_junk=True),
                ]
                archiver.reconcile_presence(connection, paths, moved)

                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_events"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_genuine_inbox_disappearance_still_creates_an_alert_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "real-disappearance.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "INBOX", 1)
                archiver.reconcile_presence(
                    connection, paths, [self.scan("INBOX", (1,))]
                )
                archiver.reconcile_presence(
                    connection, paths, [self.scan("INBOX", ())]
                )
                archiver.reconcile_presence(
                    connection, paths, [self.scan("INBOX", ())]
                )

                events = connection.execute(
                    "SELECT event_type FROM audit_events"
                ).fetchall()
                self.assertEqual(
                    [row["event_type"] for row in events],
                    ["unexplained_disappearance"],
                )
            finally:
                connection.close()

    def test_genuine_trash_arrival_and_disappearance_still_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "real-trash.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Trash", 3)
                empty = [self.scan("Trash", (), is_trash=True)]
                present = [self.scan("Trash", (3,), is_trash=True)]
                archiver.reconcile_presence(connection, paths, empty)
                archiver.observe_priority_arrivals(connection, paths, present)
                archiver.reconcile_presence(connection, paths, empty)
                archiver.reconcile_presence(connection, paths, empty)

                events = connection.execute(
                    "SELECT event_type FROM audit_events ORDER BY observed_at, event_key"
                ).fetchall()
                self.assertEqual(
                    {row["event_type"] for row in events},
                    {"trash_observed", "trash_disappeared"},
                )
            finally:
                connection.close()

    def test_queued_false_alert_is_suppressed_after_junk_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "queued-alert.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Bulk Mail", 2)
                with connection:
                    archiver.record_audit_event(
                        connection,
                        "legacy-unexplained",
                        "unexplained_disappearance",
                        "2026-09-01T00:00:00+00:00",
                        "INBOX",
                        77,
                        1,
                        self.SHA256,
                        {"account": "personal"},
                    )

                archiver.observe_priority_arrivals(
                    connection,
                    paths,
                    [self.scan("Bulk Mail", (2,), is_junk=True)],
                )
                row = connection.execute(
                    "SELECT alerted_at, last_alert_error FROM audit_events "
                    "WHERE event_key = 'legacy-unexplained'"
                ).fetchone()
                self.assertIsNotNone(row["alerted_at"])
                self.assertEqual(
                    row["last_alert_error"],
                    "suppressed: latest classification is spam/junk",
                )
            finally:
                connection.close()

    def test_junk_lineage_survives_folder_reclassification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "junk-lineage.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Bulk Mail", 2)
                archiver.reconcile_presence(
                    connection,
                    paths,
                    [self.scan("Bulk Mail", (2,), is_junk=True)],
                )
                self.record_occurrence(connection, "Bulk Mail", 3)
                archiver.reconcile_presence(
                    connection,
                    paths,
                    [self.scan("Bulk Mail", (2, 3), is_junk=False)],
                )

                folder = connection.execute(
                    "SELECT is_junk FROM presence_folders "
                    "WHERE folder = 'Bulk Mail'"
                ).fetchone()
                message = connection.execute(
                    "SELECT is_junk FROM message_presence "
                    "WHERE folder = 'Bulk Mail' ORDER BY uid"
                ).fetchall()
                self.assertEqual(folder["is_junk"], 1)
                self.assertEqual(
                    [row["is_junk"] for row in message],
                    [1, 1],
                )
                self.assertTrue(
                    archiver.content_is_junk_suppressed(
                        connection, self.SHA256
                    )
                )
            finally:
                connection.close()

    def test_priority_scan_defers_non_junk_uidvalidity_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "priority-reset.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "Trash", 3, uidvalidity=77)
                archiver.reconcile_presence(
                    connection,
                    paths,
                    [self.scan("Trash", (3,), is_trash=True)],
                )
                self.record_occurrence(connection, "Trash", 4, uidvalidity=88)

                archiver.observe_priority_arrivals(
                    connection,
                    paths,
                    [
                        self.scan(
                            "Trash",
                            (4,),
                            uidvalidity=88,
                            is_trash=True,
                            complete_snapshot=False,
                        )
                    ],
                )
                folder = connection.execute(
                    "SELECT uidvalidity FROM presence_folders "
                    "WHERE folder = 'Trash'"
                ).fetchone()
                old_message = connection.execute(
                    "SELECT present FROM message_presence "
                    "WHERE folder = 'Trash' AND uidvalidity = 77 AND uid = 3"
                ).fetchone()
                self.assertEqual(folder["uidvalidity"], 77)
                self.assertEqual(old_message["present"], 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_events"
                    ).fetchone()[0],
                    0,
                )

                archiver.reconcile_presence(
                    connection,
                    paths,
                    [
                        self.scan(
                            "Trash", (4,), uidvalidity=88, is_trash=True
                        )
                    ],
                )
                event_types = {
                    row["event_type"]
                    for row in connection.execute(
                        "SELECT event_type FROM audit_events"
                    ).fetchall()
                }
                self.assertEqual(event_types, {"uidvalidity_changed"})
                self.assertEqual(
                    connection.execute(
                        "SELECT present FROM message_presence "
                        "WHERE folder = 'Trash' AND uidvalidity = 77 AND uid = 3"
                    ).fetchone()["present"],
                    0,
                )
            finally:
                connection.close()

    def test_complete_reconciliation_rejects_incremental_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "partial-reconcile.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                with self.assertRaisesRegex(ValueError, "complete UID snapshots"):
                    archiver.reconcile_presence(
                        connection,
                        paths,
                        [
                            self.scan(
                                "INBOX",
                                (2,),
                                complete_snapshot=False,
                            )
                        ],
                    )
            finally:
                connection.close()

    def test_priority_scan_skips_unchanged_presence_upserts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "unchanged-priority.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                self.record_occurrence(connection, "INBOX", 1)
                scan = self.scan("INBOX", (1,))
                archiver.reconcile_presence(connection, paths, [scan])

                with (
                    mock.patch.object(
                        archiver,
                        "upsert_presence_folder",
                        wraps=archiver.upsert_presence_folder,
                    ) as folder_upsert,
                    mock.patch.object(
                        archiver,
                        "upsert_message_presences",
                        wraps=archiver.upsert_message_presences,
                    ) as message_upsert,
                    mock.patch.object(
                        archiver,
                        "suppress_junk_related_pending_alerts",
                        wraps=archiver.suppress_junk_related_pending_alerts,
                    ) as suppress_junk_alerts,
                ):
                    archiver.observe_priority_arrivals(connection, paths, [scan])
                folder_upsert.assert_not_called()
                message_upsert.assert_not_called()
                suppress_junk_alerts.assert_not_called()
            finally:
                connection.close()

    def test_successful_junk_retry_refreshes_hash_before_alerting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "junk-retry.sqlite3"
            )
            paths = archiver.runtime_paths("personal")
            try:
                archiver.reconcile_presence(
                    connection,
                    paths,
                    [self.scan("Bulk Mail", (2,), is_junk=True)],
                )
                self.record_occurrence(connection, "Bulk Mail", 2)
                with connection:
                    archiver.record_audit_event(
                        connection,
                        "pending-before-retry",
                        "unexplained_disappearance",
                        "2026-09-01T00:00:00+00:00",
                        "INBOX",
                        77,
                        1,
                        self.SHA256,
                        {"account": "personal"},
                    )

                archiver.observe_priority_arrivals(
                    connection,
                    paths,
                    [
                        self.scan(
                            "Bulk Mail",
                            (),
                            is_junk=True,
                            complete_snapshot=False,
                            refreshed_uids=(2,),
                        )
                    ],
                )

                presence = connection.execute(
                    "SELECT sha256 FROM message_presence "
                    "WHERE folder = 'Bulk Mail' AND uid = 2"
                ).fetchone()
                event = connection.execute(
                    "SELECT alerted_at, last_alert_error FROM audit_events "
                    "WHERE event_key = 'pending-before-retry'"
                ).fetchone()
                self.assertEqual(presence["sha256"], self.SHA256)
                self.assertIsNotNone(event["alerted_at"])
                self.assertEqual(
                    event["last_alert_error"],
                    "suppressed: latest classification is spam/junk",
                )
            finally:
                connection.close()


class AlertFormattingTests(unittest.TestCase):
    def test_compact_alert_uses_account_prefix_and_body_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = archiver.initialize_database(
                Path(directory) / "compact-alert.sqlite3"
            )
            sha256 = "c" * 64
            body = "Missing email body with enough text to exceed fifty characters."
            stored_body = f"  {body.replace(' ', '   ')}  "
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO archive_objects("
                        "sha256, object_name, size_bytes, b2_file_id, b2_sha1, "
                        "archived_at, body_preview) VALUES "
                        "(?, 'mail/object.eml', 10, 'file-1', ?, 'now', ?)",
                        (sha256, "d" * 40, stored_body),
                    )
                    archiver.record_audit_event(
                        connection,
                        "missing-message",
                        "unexplained_disappearance",
                        "2026-09-01T00:00:00+00:00",
                        "INBOX",
                        77,
                        1,
                        sha256,
                        {"account": "amanda-hoffmaster"},
                    )
                    connection.execute(
                        "UPDATE audit_events SET b2_uploaded_at = 'now' "
                        "WHERE event_key = 'missing-message'"
                    )

                config = {
                    "PUSHOVER_APP_TOKEN": "test-token",
                    "PUSHOVER_USER_KEY": "test-user",
                }
                statements: list[str] = []
                connection.set_trace_callback(statements.append)
                try:
                    with mock.patch.object(
                        archiver, "send_pushover_message"
                    ) as send:
                        archiver.send_pending_alerts(
                            connection,
                            archiver.runtime_paths("amanda-hoffmaster"),
                            config,
                        )
                finally:
                    connection.set_trace_callback(None)

                send.assert_called_once_with(
                    config,
                    "YahArch:amanda-",
                    body[:50],
                )
                self.assertEqual(
                    sum(
                        statement.lstrip().upper().startswith("SELECT")
                        for statement in statements
                    ),
                    1,
                )
                self.assertEqual(
                    archiver.normalized_body_preview(None),
                    "Body unavailable",
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT alerted_at FROM audit_events "
                        "WHERE event_key = 'missing-message'"
                    ).fetchone()["alerted_at"]
                )
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
