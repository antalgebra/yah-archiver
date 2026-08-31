#!/usr/bin/env python3
"""Read-only Yahoo IMAP archiver for Backblaze B2.

The program only reads from IMAP. It never marks, moves, deletes, or expunges
Yahoo messages. Raw RFC822 bytes are staged locally, uploaded to B2, verified,
and then recorded in SQLite.
"""

from __future__ import annotations

import argparse
import email.policy
import hashlib
import imaplib
import json
import logging
import os
import re
import signal
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

CONFIG_DIRECTORY = Path("/etc/yah-arch")
STATE_DIRECTORY = Path("/var/lib/yah-arch")
B2_CONFIG_PATH = CONFIG_DIRECTORY / "b2.env"
SETTINGS_CONFIG_PATH = CONFIG_DIRECTORY / "settings.env"
ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
DEFAULT_POLL_SECONDS = 5
DEFAULT_RETRY_SECONDS = 30
DEFAULT_BACKFILL_BATCH_SIZE = 10
DEFAULT_ARCHIVE_AFTER = date(2026, 8, 30)
FAILURE_RETRY_BATCH_SIZE = 25
MAX_CONSECUTIVE_MESSAGE_FAILURES = 3
PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"

LOG = logging.getLogger("yah-arch")
STOP_REQUESTED = False

LIST_PATTERN = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$"
)
INTERNAL_DATE_PATTERN = re.compile(rb'INTERNALDATE "(?P<value>[^"]+)"', re.IGNORECASE)


@dataclass(frozen=True)
class Mailbox:
    key: str
    select_argument: str
    flags: frozenset[str]


@dataclass(frozen=True)
class MessageMetadata:
    internal_date: datetime
    message_id: str
    sender: str
    recipients: str
    subject: str


@dataclass(frozen=True)
class RuntimePaths:
    """Account-specific paths and the account's immutable B2 namespace."""

    account: str
    b2_config_path: Path
    yahoo_config_path: Path
    pushover_config_path: Path
    database_path: Path
    temp_directory: Path
    b2_message_prefix: str
    b2_event_prefix: str


@dataclass(frozen=True)
class FolderState:
    uidvalidity: int
    live_cursor_uid: int
    backfill_before_uid: int
    backfill_complete: bool


@dataclass(frozen=True)
class MessageAttempt:
    uploaded: bool
    retryable_failure: bool


@dataclass(frozen=True)
class MailboxScan:
    attempted: int
    uploaded: int
    folder: str
    uidvalidity: int
    current_uids: tuple[int, ...]
    is_trash: bool


class ImapCommandError(RuntimeError):
    """An IMAP command failed in a way that requires reconnecting."""


class CycleAbortError(RuntimeError):
    """A systemic-looking failure should stop this cycle and retry later."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            raise ValueError(f"{path}:{line_number}: empty setting")
        values[name] = value
    return values


def read_optional_env(path: Path) -> dict[str, str]:
    return read_env(path) if path.exists() else {}


def parse_archive_after(value: str | None) -> date:
    if value is None:
        return DEFAULT_ARCHIVE_AFTER
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("ARCHIVE_AFTER must use YYYY-MM-DD") from error


def require_settings(config: dict[str, str], names: tuple[str, ...], source: Path) -> None:
    missing = [name for name in names if not config.get(name)]
    if missing:
        raise ValueError(f"Missing settings in {source}: {', '.join(missing)}")


def parse_positive_int(value: str | None, default: int, name: str) -> int:
    if value is None:
        return default
    result = int(value)
    if result < 1:
        raise ValueError(f"{name} must be at least 1")
    return result


def validate_account_name(value: str) -> str:
    """Return a safe account identifier or raise an argparse-friendly error."""

    if not ACCOUNT_NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "account must be 1-32 lowercase letters, numbers, underscores, or "
            "hyphens, and must start with a letter or number"
        )
    return value


def runtime_paths(account: str) -> RuntimePaths:
    """Build isolated runtime paths for one Yahoo account."""

    account = validate_account_name(account)
    return RuntimePaths(
        account=account,
        b2_config_path=B2_CONFIG_PATH,
        yahoo_config_path=CONFIG_DIRECTORY / "accounts" / f"{account}.env",
        pushover_config_path=CONFIG_DIRECTORY / "pushover.env",
        database_path=STATE_DIRECTORY / "data" / f"{account}.sqlite3",
        temp_directory=STATE_DIRECTORY / "tmp" / account,
        b2_message_prefix=f"mail/{account}/messages",
        b2_event_prefix=f"mail/{account}/events",
    )


def initialize_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(journal_mode).lower() != "delete":
        connection.close()
        raise RuntimeError(f"Could not enable SQLite rollback journal for {path}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive_objects (
            sha256 TEXT PRIMARY KEY,
            object_name TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            b2_file_id TEXT NOT NULL,
            b2_sha1 TEXT NOT NULL,
            archived_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_copies (
            sha256 TEXT NOT NULL,
            destination_id TEXT NOT NULL,
            object_name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            b2_file_id TEXT NOT NULL,
            b2_sha1 TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            PRIMARY KEY (sha256, destination_id),
            FOREIGN KEY (sha256) REFERENCES archive_objects(sha256)
        );

        CREATE INDEX IF NOT EXISTS archive_copies_destination_idx
            ON archive_copies(destination_id, archived_at);

        CREATE TABLE IF NOT EXISTS imap_messages (
            folder TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            internal_date TEXT NOT NULL,
            message_id TEXT,
            sender TEXT,
            recipients TEXT,
            subject TEXT,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (folder, uidvalidity, uid),
            FOREIGN KEY (sha256) REFERENCES archive_objects(sha256)
        );

        CREATE INDEX IF NOT EXISTS imap_messages_sha256_idx
            ON imap_messages(sha256);

        CREATE TABLE IF NOT EXISTS folder_state (
            folder TEXT PRIMARY KEY,
            uidvalidity INTEGER NOT NULL,
            last_uid INTEGER NOT NULL DEFAULT 0,
            live_cursor_uid INTEGER NOT NULL DEFAULT 0,
            backfill_before_uid INTEGER,
            backfill_complete INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_failures (
            folder TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            failure_kind TEXT NOT NULL,
            retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
            attempts INTEGER NOT NULL,
            first_failed_at TEXT NOT NULL,
            last_failed_at TEXT NOT NULL,
            last_error TEXT NOT NULL,
            PRIMARY KEY (folder, uidvalidity, uid)
        );

        CREATE INDEX IF NOT EXISTS message_failures_retry_idx
            ON message_failures(folder, uidvalidity, retryable, last_failed_at);

        CREATE TABLE IF NOT EXISTS presence_folders (
            folder TEXT PRIMARY KEY,
            uidvalidity INTEGER NOT NULL,
            is_trash INTEGER NOT NULL CHECK (is_trash IN (0, 1)),
            present INTEGER NOT NULL CHECK (present IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            missing_clean_scans INTEGER NOT NULL DEFAULT 0,
            disappeared_at TEXT
        );

        CREATE TABLE IF NOT EXISTS message_presence (
            folder TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            sha256 TEXT,
            is_trash INTEGER NOT NULL CHECK (is_trash IN (0, 1)),
            present INTEGER NOT NULL CHECK (present IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            missing_clean_scans INTEGER NOT NULL DEFAULT 0,
            disappeared_at TEXT,
            PRIMARY KEY (folder, uidvalidity, uid)
        );

        CREATE INDEX IF NOT EXISTS message_presence_sha_idx
            ON message_presence(sha256, present);

        CREATE TABLE IF NOT EXISTS audit_events (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            folder TEXT,
            uidvalidity INTEGER,
            uid INTEGER,
            sha256 TEXT,
            details_json TEXT NOT NULL,
            b2_object_name TEXT,
            b2_file_id TEXT,
            b2_uploaded_at TEXT,
            upload_attempts INTEGER NOT NULL DEFAULT 0,
            last_upload_error TEXT,
            alert_attempts INTEGER NOT NULL DEFAULT 0,
            last_alert_error TEXT,
            alerted_at TEXT
        );

        CREATE INDEX IF NOT EXISTS audit_events_pending_idx
            ON audit_events(b2_uploaded_at, observed_at);

        CREATE TABLE IF NOT EXISTS health_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    migrate_folder_state(connection)
    migrate_audit_events(connection)
    return connection


def migrate_folder_state(connection: sqlite3.Connection) -> None:
    """Upgrade the pre-multi-cursor folder table without discarding audit data."""

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(folder_state)").fetchall()
    }
    additions = {
        "live_cursor_uid": "INTEGER NOT NULL DEFAULT 0",
        "backfill_before_uid": "INTEGER",
        "backfill_complete": "INTEGER NOT NULL DEFAULT 0",
    }
    with connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE folder_state ADD COLUMN {name} {definition}"
                )

        # The old scanner processed every visible UID through last_uid in order.
        # Preserve that work as a completed historical backfill.
        connection.execute(
            "UPDATE folder_state "
            "SET live_cursor_uid = last_uid, backfill_before_uid = 1, "
            "backfill_complete = 1 "
            "WHERE last_uid > 0 AND live_cursor_uid = 0 "
            "AND backfill_before_uid IS NULL"
        )


def migrate_audit_events(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
    }
    additions = {
        "alert_attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_alert_error": "TEXT",
    }
    with connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE audit_events ADD COLUMN {name} {definition}"
                )


def create_b2_client(config: dict[str, str], source: Path):
    # Keep the optional third-party dependency out of argument parsing and tests.
    from b2sdk.v3 import AuthInfoCache, B2Api, InMemoryAccountInfo

    require_settings(config, ("B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"), source)
    account_info = InMemoryAccountInfo()
    api = B2Api(account_info, cache=AuthInfoCache(account_info))
    api.authorize_account(
        application_key_id=config["B2_KEY_ID"],
        application_key=config["B2_APPLICATION_KEY"],
    )
    return api.get_bucket_by_name(config["B2_BUCKET"])


def mailbox_is_trash(mailbox: Mailbox) -> bool:
    upper_name = mailbox.key.upper()
    return "\\TRASH" in mailbox.flags or upper_name == "TRASH"


def folder_name_is_ignored(folder: str) -> bool:
    """Return True for spam/bulk folders that are outside archive scope."""

    upper_name = folder.upper()
    return any(marker in upper_name for marker in ("BULK", "SPAM", "JUNK"))


def mailbox_is_ignored(mailbox: Mailbox) -> bool:
    return (
        "\\JUNK" in mailbox.flags
        or "\\SPAM" in mailbox.flags
        or folder_name_is_ignored(mailbox.key)
    )


def health_value(database: sqlite3.Connection, key: str) -> str | None:
    row = database.execute(
        "SELECT value FROM health_state WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row["value"])


def set_health(database: sqlite3.Connection, key: str, value: str | None = None) -> None:
    now = iso_utc()
    database.execute(
        "INSERT INTO health_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value or now, now),
    )


def initialize_archive_destination(
    database: sqlite3.Connection, destination_id: str
) -> None:
    """Associate legacy rows with the first destination seen after migration."""

    previous = health_value(database, "current_b2_destination")
    with database:
        if previous is None:
            database.execute(
                "INSERT OR IGNORE INTO archive_copies("
                "sha256, destination_id, object_name, size_bytes, b2_file_id, "
                "b2_sha1, archived_at"
                ") SELECT sha256, ?, object_name, size_bytes, b2_file_id, "
                "b2_sha1, archived_at FROM archive_objects",
                (destination_id,),
            )
        set_health(database, "current_b2_destination", destination_id)


def record_audit_event(
    database: sqlite3.Connection,
    event_key: str,
    event_type: str,
    observed_at: str,
    folder: str | None,
    uidvalidity: int | None,
    uid: int | None,
    sha256: str | None,
    details: dict,
) -> None:
    database.execute(
        "INSERT OR IGNORE INTO audit_events("
        "event_key, event_type, observed_at, folder, uidvalidity, uid, sha256, "
        "details_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_key,
            event_type,
            observed_at,
            folder,
            uidvalidity,
            uid,
            sha256,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )


def archived_message_sha256(
    database: sqlite3.Connection, folder: str, uidvalidity: int, uid: int
) -> str | None:
    row = database.execute(
        "SELECT sha256 FROM imap_messages "
        "WHERE folder = ? AND uidvalidity = ? AND uid = ?",
        (folder, uidvalidity, uid),
    ).fetchone()
    return None if row is None else str(row["sha256"])


def unquote_imap_bytes(token: bytes) -> bytes:
    token = token.strip()
    if len(token) >= 2 and token.startswith(b'"') and token.endswith(b'"'):
        body = token[1:-1]
        result = bytearray()
        escaped = False
        for byte in body:
            if escaped:
                result.append(byte)
                escaped = False
            elif byte == 0x5C:
                escaped = True
            else:
                result.append(byte)
        if escaped:
            result.append(0x5C)
        return bytes(result)
    return token


def quote_imap_mailbox(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_mailbox_list_line(line: bytes) -> Mailbox | None:
    match = LIST_PATTERN.match(line.strip())
    if not match:
        raise ValueError(f"Could not parse IMAP LIST response: {line!r}")

    flags = frozenset(
        value.decode("ascii", "replace").upper()
        for value in match.group("flags").split()
    )
    if "\\NOSELECT" in flags:
        return None

    mailbox_bytes = unquote_imap_bytes(match.group("name"))
    mailbox = mailbox_bytes.decode("ascii", "strict")
    return Mailbox(mailbox, quote_imap_mailbox(mailbox), flags)


def list_selectable_mailboxes(client: imaplib.IMAP4_SSL) -> list[Mailbox]:
    status, lines = client.list()
    if status != "OK" or lines is None:
        raise RuntimeError(f"IMAP LIST failed: {status}")

    mailboxes: list[Mailbox] = []
    for line in lines:
        if not isinstance(line, bytes):
            raise RuntimeError(f"Unexpected IMAP LIST response: {line!r}")
        mailbox = parse_mailbox_list_line(line)
        if mailbox is not None and not mailbox_is_ignored(mailbox):
            mailboxes.append(mailbox)

    def priority(mailbox: Mailbox) -> tuple[int, str]:
        upper_name = mailbox.key.upper()
        if mailbox_is_trash(mailbox):
            rank = 0
        elif upper_name == "INBOX":
            rank = 1
        elif "\\SENT" in mailbox.flags or upper_name in {"SENT", "SENT ITEMS"}:
            rank = 2
        else:
            rank = 3
        return rank, upper_name

    return sorted(mailboxes, key=priority)


def selected_uidvalidity(client: imaplib.IMAP4_SSL) -> int:
    _status, values = client.response("UIDVALIDITY")
    if not values or values[0] is None:
        raise RuntimeError("Selected mailbox did not return UIDVALIDITY")
    value = values[0]
    if isinstance(value, bytes):
        value = value.decode("ascii")
    return int(value)


def ensure_folder_state(
    database: sqlite3.Connection,
    folder: str,
    uidvalidity: int,
    current_uids: list[int],
) -> FolderState:
    """Load folder progress, initializing a newest-first historical backfill."""

    row = database.execute(
        "SELECT uidvalidity, live_cursor_uid, backfill_before_uid, "
        "backfill_complete FROM folder_state WHERE folder = ?",
        (folder,),
    ).fetchone()

    needs_initialization = row is None or row["backfill_before_uid"] is None
    uidvalidity_changed = row is not None and row["uidvalidity"] != uidvalidity
    if not needs_initialization and not uidvalidity_changed:
        return FolderState(
            uidvalidity=int(row["uidvalidity"]),
            live_cursor_uid=int(row["live_cursor_uid"]),
            backfill_before_uid=int(row["backfill_before_uid"]),
            backfill_complete=bool(row["backfill_complete"]),
        )

    high_uid = max(current_uids, default=0)
    backfill_before_uid = high_uid + 1
    backfill_complete = not current_uids

    if uidvalidity_changed:
        LOG.warning(
            "UIDVALIDITY changed for %s: %s -> %s; starting a fresh rescan",
            folder,
            row["uidvalidity"],
            uidvalidity,
        )

    with database:
        database.execute(
            "INSERT INTO folder_state("
            "folder, uidvalidity, last_uid, live_cursor_uid, "
            "backfill_before_uid, backfill_complete, checked_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(folder) DO UPDATE SET "
            "uidvalidity = excluded.uidvalidity, "
            "last_uid = excluded.last_uid, "
            "live_cursor_uid = excluded.live_cursor_uid, "
            "backfill_before_uid = excluded.backfill_before_uid, "
            "backfill_complete = excluded.backfill_complete, "
            "checked_at = excluded.checked_at",
            (
                folder,
                uidvalidity,
                high_uid,
                high_uid,
                backfill_before_uid,
                int(backfill_complete),
                iso_utc(),
            ),
        )

    return FolderState(
        uidvalidity=uidvalidity,
        live_cursor_uid=high_uid,
        backfill_before_uid=backfill_before_uid,
        backfill_complete=backfill_complete,
    )


def update_live_cursor(
    database: sqlite3.Connection, folder: str, uidvalidity: int, uid: int
) -> None:
    database.execute(
        "UPDATE folder_state SET last_uid = ?, live_cursor_uid = ? "
        "WHERE folder = ? AND uidvalidity = ?",
        (uid, uid, folder, uidvalidity),
    )


def update_backfill_cursor(
    database: sqlite3.Connection,
    folder: str,
    uidvalidity: int,
    before_uid: int,
    complete: bool = False,
) -> None:
    database.execute(
        "UPDATE folder_state SET backfill_before_uid = ?, backfill_complete = ? "
        "WHERE folder = ? AND uidvalidity = ?",
        (before_uid, int(complete), folder, uidvalidity),
    )


def search_all_uids(
    client: imaplib.IMAP4_SSL, archive_since: date
) -> list[int]:
    # Yahoo evaluates IMAP SINCE against INTERNALDATE. Filtering on the server
    # avoids downloading, hashing, or tracking older messages.
    imap_date = archive_since.strftime("%d-%b-%Y")
    status, data = client.uid("SEARCH", None, "SINCE", imap_date)
    if status != "OK" or data is None:
        raise ImapCommandError(f"IMAP UID SEARCH failed: {status}")
    values = data[0].split() if data and isinstance(data[0], bytes) else []
    return sorted(int(value) for value in values)


def fetch_raw_message(
    client: imaplib.IMAP4_SSL, uid: int
) -> tuple[bytes, bytes] | None:
    status, parts = client.uid("FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[])")
    if status != "OK" or parts is None:
        raise ImapCommandError(f"IMAP UID FETCH failed for UID {uid}: {status}")

    for part in parts:
        if (
            isinstance(part, tuple)
            and len(part) == 2
            and isinstance(part[0], bytes)
            and isinstance(part[1], bytes)
        ):
            return part[0], part[1]
    return None


def parse_internal_date(fetch_metadata: bytes) -> datetime:
    match = INTERNAL_DATE_PATTERN.search(fetch_metadata)
    if match:
        try:
            parsed = parsedate_to_datetime(match.group("value").decode("ascii"))
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return utc_now()


def bounded_header(message, name: str, limit: int = 1000) -> str:
    try:
        value = message.get(name)
    except Exception:
        # Malformed headers must not prevent preservation of the raw RFC822
        # message. raw_items() returns the original value without structured
        # header parsing.
        value = next(
            (
                raw_value
                for raw_name, raw_value in message.raw_items()
                if raw_name.lower() == name.lower()
            ),
            None,
        )
    if value is None:
        return ""
    text = " ".join(str(value).replace("\x00", "").split())
    return text[:limit]


def message_metadata(raw_message: bytes, fetch_metadata: bytes) -> MessageMetadata:
    headers = BytesHeaderParser(policy=email.policy.default).parsebytes(raw_message)
    recipients = ", ".join(
        value for value in (bounded_header(headers, "To"), bounded_header(headers, "Cc")) if value
    )
    return MessageMetadata(
        internal_date=parse_internal_date(fetch_metadata),
        message_id=bounded_header(headers, "Message-ID"),
        sender=bounded_header(headers, "From"),
        recipients=recipients[:1000],
        subject=bounded_header(headers, "Subject"),
    )


def safe_subject(subject: str) -> str:
    normalized = unicodedata.normalize("NFKD", subject).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._")
    return (normalized or "no-subject")[:80]


def make_object_name(
    metadata: MessageMetadata,
    sha256: str,
    message_prefix: str,
    source_folder: str,
) -> str:
    value = metadata.internal_date.astimezone(timezone.utc)
    return (
        f"{message_prefix}/{value:%Y}/{value:%Y-%m-%d_%H%M%S}_"
        f"{safe_subject(source_folder)}_{safe_subject(metadata.subject)}_"
        f"{sha256[:16]}.eml"
    )


def stage_message(raw_message: bytes, sha256: str, temp_directory: Path) -> Path:
    temp_directory.mkdir(parents=True, exist_ok=True)
    final_path = temp_directory / f"{sha256}.eml"

    if final_path.exists():
        existing_hash = hashlib.sha256(final_path.read_bytes()).hexdigest()
        if existing_hash != sha256:
            raise RuntimeError(f"Temporary file hash mismatch: {final_path}")
        return final_path

    partial_path = temp_directory / f".{sha256}.{os.getpid()}.part"
    descriptor = os.open(partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw_message)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial_path, final_path)
    except Exception:
        try:
            partial_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return final_path


def archived_copy_exists(
    database: sqlite3.Connection, sha256: str, destination_id: str
) -> bool:
    return database.execute(
        "SELECT 1 FROM archive_copies "
        "WHERE sha256 = ? AND destination_id = ?",
        (sha256, destination_id),
    ).fetchone() is not None


def upload_and_verify(
    bucket,
    staged_path: Path,
    object_name: str,
    sha256: str,
    raw_sha1: str,
):
    uploaded = bucket.upload_local_file(
        str(staged_path),
        object_name,
        content_type="message/rfc822",
        file_info={"sha256": sha256},
        sha1_sum=raw_sha1,
    )
    if uploaded.content_sha1 != raw_sha1:
        raise RuntimeError("B2 SHA-1 verification failed")
    if uploaded.file_name != object_name:
        raise RuntimeError("B2 returned an unexpected object name")
    if uploaded.size != staged_path.stat().st_size:
        raise RuntimeError("B2 returned an unexpected object size")
    if uploaded.file_info.get("sha256") != sha256:
        raise RuntimeError("B2 returned unexpected SHA-256 metadata")
    return uploaded


def record_occurrence(
    database: sqlite3.Connection,
    folder: str,
    uidvalidity: int,
    uid: int,
    sha256: str,
    metadata: MessageMetadata,
) -> None:
    database.execute(
        "INSERT OR IGNORE INTO imap_messages("
        "folder, uidvalidity, uid, sha256, internal_date, message_id, sender, "
        "recipients, subject, first_seen_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            folder,
            uidvalidity,
            uid,
            sha256,
            iso_utc(metadata.internal_date),
            metadata.message_id,
            metadata.sender,
            metadata.recipients,
            metadata.subject,
            iso_utc(),
        ),
    )


def record_message_failure(
    database: sqlite3.Connection,
    folder: str,
    uidvalidity: int,
    uid: int,
    failure_kind: str,
    retryable: bool,
    error: str,
) -> None:
    now = iso_utc()
    bounded_error = " ".join(error.replace("\x00", "").split())[:1000]
    database.execute(
        "INSERT INTO message_failures("
        "folder, uidvalidity, uid, failure_kind, retryable, attempts, "
        "first_failed_at, last_failed_at, last_error"
        ") VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(folder, uidvalidity, uid) DO UPDATE SET "
        "failure_kind = excluded.failure_kind, "
        "retryable = excluded.retryable, "
        "attempts = message_failures.attempts + 1, "
        "last_failed_at = excluded.last_failed_at, "
        "last_error = excluded.last_error",
        (
            folder,
            uidvalidity,
            uid,
            failure_kind,
            int(retryable),
            now,
            now,
            bounded_error,
        ),
    )


def clear_message_failure(
    database: sqlite3.Connection, folder: str, uidvalidity: int, uid: int
) -> None:
    database.execute(
        "DELETE FROM message_failures "
        "WHERE folder = ? AND uidvalidity = ? AND uid = ?",
        (folder, uidvalidity, uid),
    )


def retryable_failure_uids(
    database: sqlite3.Connection,
    folder: str,
    uidvalidity: int,
    limit: int,
) -> list[int]:
    rows = database.execute(
        "SELECT uid FROM message_failures "
        "WHERE folder = ? AND uidvalidity = ? AND retryable = 1 "
        "ORDER BY last_failed_at, uid LIMIT ?",
        (folder, uidvalidity, limit),
    ).fetchall()
    return [int(row["uid"]) for row in rows]


def archive_message(
    database: sqlite3.Connection,
    bucket,
    paths: RuntimePaths,
    mailbox: Mailbox,
    uidvalidity: int,
    uid: int,
    fetch_metadata: bytes,
    raw_message: bytes,
    destination_id: str,
) -> bool:
    sha256 = hashlib.sha256(raw_message).hexdigest()
    metadata = message_metadata(raw_message, fetch_metadata)

    if archived_copy_exists(database, sha256, destination_id):
        with database:
            record_occurrence(database, mailbox.key, uidvalidity, uid, sha256, metadata)
        LOG.info(
            "Already archived in destination: folder=%s uid=%s sha256=%s",
            mailbox.key,
            uid,
            sha256,
        )
        return False

    object_name = make_object_name(
        metadata,
        sha256,
        paths.b2_message_prefix,
        mailbox.key,
    )
    staged_path = stage_message(raw_message, sha256, paths.temp_directory)
    raw_sha1 = hashlib.sha1(raw_message, usedforsecurity=False).hexdigest()
    uploaded = upload_and_verify(
        bucket,
        staged_path,
        object_name,
        sha256,
        raw_sha1,
    )

    with database:
        archived_at = iso_utc()
        database.execute(
            "INSERT OR IGNORE INTO archive_objects("
            "sha256, object_name, size_bytes, b2_file_id, b2_sha1, archived_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                sha256,
                object_name,
                len(raw_message),
                uploaded.id_,
                uploaded.content_sha1,
                archived_at,
            ),
        )
        database.execute(
            "INSERT INTO archive_copies("
            "sha256, destination_id, object_name, size_bytes, b2_file_id, "
            "b2_sha1, archived_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sha256,
                destination_id,
                object_name,
                len(raw_message),
                uploaded.id_,
                uploaded.content_sha1,
                archived_at,
            ),
        )
        record_occurrence(database, mailbox.key, uidvalidity, uid, sha256, metadata)
        set_health(database, "last_successful_b2_upload")

    try:
        staged_path.unlink()
    except OSError:
        LOG.warning("Could not remove uploaded temporary file: %s", staged_path)

    LOG.info(
        "Archived: folder=%s uid=%s bytes=%s sha256=%s object=%s",
        mailbox.key,
        uid,
        len(raw_message),
        sha256,
        object_name,
    )
    return True


def attempt_message(
    client: imaplib.IMAP4_SSL,
    database: sqlite3.Connection,
    bucket,
    paths: RuntimePaths,
    mailbox: Mailbox,
    uidvalidity: int,
    uid: int,
    destination_id: str,
) -> MessageAttempt:
    """Attempt one message without letting a poison message block later UIDs."""

    try:
        fetched_message = fetch_raw_message(client, uid)
        if fetched_message is None:
            observed_at = iso_utc()
            with database:
                record_message_failure(
                    database,
                    mailbox.key,
                    uidvalidity,
                    uid,
                    "disappeared_before_fetch",
                    False,
                    "UID was visible in SEARCH but absent during FETCH",
                )
                record_audit_event(
                    database,
                    f"message_disappeared_before_fetch:{mailbox.key}:"
                    f"{uidvalidity}:{uid}",
                    "message_disappeared_before_fetch",
                    observed_at,
                    mailbox.key,
                    uidvalidity,
                    uid,
                    None,
                    {
                        "account": paths.account,
                        "meaning": (
                            "The UID was visible during SEARCH but its RFC822 "
                            "content was unavailable during FETCH. Cause and actor "
                            "cannot be determined from IMAP."
                        ),
                    },
                )
            LOG.warning(
                "Message disappeared before fetch: folder=%s uid=%s",
                mailbox.key,
                uid,
            )
            return MessageAttempt(uploaded=False, retryable_failure=False)

        fetch_metadata, raw_message = fetched_message
        if not raw_message:
            raise ValueError("Yahoo returned an empty RFC822 message")

        uploaded = archive_message(
            database,
            bucket,
            paths,
            mailbox,
            uidvalidity,
            uid,
            fetch_metadata,
            raw_message,
            destination_id,
        )
        with database:
            clear_message_failure(database, mailbox.key, uidvalidity, uid)
        return MessageAttempt(uploaded=uploaded, retryable_failure=False)
    except (ImapCommandError, imaplib.IMAP4.abort, ssl.SSLError, OSError):
        raise
    except Exception as error:
        with database:
            record_message_failure(
                database,
                mailbox.key,
                uidvalidity,
                uid,
                "archive_error",
                True,
                f"{type(error).__name__}: {error}",
            )
        LOG.exception(
            "Message archive failed but later UIDs may continue: folder=%s uid=%s",
            mailbox.key,
            uid,
        )
        return MessageAttempt(uploaded=False, retryable_failure=True)


def archive_mailbox(
    client: imaplib.IMAP4_SSL,
    database: sqlite3.Connection,
    bucket,
    paths: RuntimePaths,
    mailbox: Mailbox,
    remaining_limit: int | None,
    backfill_batch_size: int,
    archive_since: date,
    destination_id: str,
    presence_scans: list[MailboxScan] | None = None,
) -> tuple[int, int]:
    status, _data = client.select(mailbox.select_argument, readonly=True)
    if status != "OK":
        raise RuntimeError(f"IMAP read-only SELECT failed for {mailbox.key}: {status}")

    uidvalidity = selected_uidvalidity(client)
    current_uids = search_all_uids(client, archive_since)
    state = ensure_folder_state(database, mailbox.key, uidvalidity, current_uids)
    attempted = 0
    uploaded = 0
    consecutive_failures = 0
    attempted_uids: set[int] = set()

    def at_limit() -> bool:
        return remaining_limit is not None and attempted >= remaining_limit

    def note_attempt(uid: int, result: MessageAttempt) -> None:
        nonlocal attempted, uploaded, consecutive_failures
        attempted += 1
        attempted_uids.add(uid)
        uploaded += int(result.uploaded)
        if result.retryable_failure:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if consecutive_failures >= MAX_CONSECUTIVE_MESSAGE_FAILURES:
            raise CycleAbortError(
                f"Stopping after {consecutive_failures} consecutive message failures"
            )

    # New arrivals are handled before retries and historical catch-up. Ascending
    # order lets the live cursor move safely after each durable attempt.
    live_uids = [uid for uid in current_uids if uid > state.live_cursor_uid]
    for uid in live_uids:
        if STOP_REQUESTED or at_limit():
            break
        result = attempt_message(
            client,
            database,
            bucket,
            paths,
            mailbox,
            uidvalidity,
            uid,
            destination_id,
        )
        with database:
            update_live_cursor(database, mailbox.key, uidvalidity, uid)
        note_attempt(uid, result)

    # Existing mail is caught up newest first in bounded batches so every
    # high-risk folder gets attention during the first cycles.
    backfill_before_uid = state.backfill_before_uid
    if not state.backfill_complete and not STOP_REQUESTED and not at_limit():
        backfill_uids = [
            uid for uid in reversed(current_uids) if uid < backfill_before_uid
        ][:backfill_batch_size]
        for uid in backfill_uids:
            if STOP_REQUESTED or at_limit():
                break
            if uid in attempted_uids:
                backfill_before_uid = uid
                with database:
                    update_backfill_cursor(
                        database,
                        mailbox.key,
                        uidvalidity,
                        backfill_before_uid,
                    )
                continue
            result = attempt_message(
                client,
                database,
                bucket,
                paths,
                mailbox,
                uidvalidity,
                uid,
                destination_id,
            )
            backfill_before_uid = uid
            with database:
                update_backfill_cursor(
                    database,
                    mailbox.key,
                    uidvalidity,
                    backfill_before_uid,
                )
            note_attempt(uid, result)

        if not STOP_REQUESTED and not any(
            uid < backfill_before_uid for uid in current_uids
        ):
            with database:
                update_backfill_cursor(
                    database,
                    mailbox.key,
                    uidvalidity,
                    backfill_before_uid,
                    complete=True,
                )

    # Retry a bounded number of earlier message-specific failures after live
    # mail and backfill. Poison retries therefore cannot starve untried UIDs.
    if not STOP_REQUESTED and not at_limit():
        retry_limit = FAILURE_RETRY_BATCH_SIZE
        if remaining_limit is not None:
            retry_limit = min(retry_limit, remaining_limit - attempted)
        retry_uids = retryable_failure_uids(
            database, mailbox.key, uidvalidity, retry_limit
        )
        for uid in retry_uids:
            if STOP_REQUESTED or at_limit():
                break
            if uid in attempted_uids:
                continue
            result = attempt_message(
                client,
                database,
                bucket,
                paths,
                mailbox,
                uidvalidity,
                uid,
                destination_id,
            )
            note_attempt(uid, result)

    with database:
        database.execute(
            "UPDATE folder_state SET checked_at = ? WHERE folder = ?",
            (iso_utc(), mailbox.key),
        )
    if presence_scans is not None:
        presence_scans.append(
            MailboxScan(
                attempted=attempted,
                uploaded=uploaded,
                folder=mailbox.key,
                uidvalidity=uidvalidity,
                current_uids=tuple(current_uids),
                is_trash=mailbox_is_trash(mailbox),
            )
        )
    return attempted, uploaded


def reconcile_presence(
    database: sqlite3.Connection,
    paths: RuntimePaths,
    scans: list[MailboxScan],
) -> None:
    """Reconcile one complete IMAP snapshot and create neutral audit events."""

    observed_at = iso_utc()
    baseline_exists = health_value(database, "presence_baseline_complete") == "1"
    observed_folders = {scan.folder for scan in scans}
    reset_folders: set[str] = set()

    with database:
        previous_folders = {
            str(row["folder"]): row
            for row in database.execute(
                "SELECT * FROM presence_folders"
            ).fetchall()
        }

        for scan in scans:
            previous = previous_folders.get(scan.folder)
            if previous is None or int(previous["uidvalidity"]) != scan.uidvalidity:
                reset_folders.add(scan.folder)

            if (
                baseline_exists
                and previous is not None
                and int(previous["uidvalidity"]) != scan.uidvalidity
            ):
                old_uidvalidity = int(previous["uidvalidity"])
                record_audit_event(
                    database,
                    f"uidvalidity_changed:{scan.folder}:{old_uidvalidity}:"
                    f"{scan.uidvalidity}",
                    "uidvalidity_changed",
                    observed_at,
                    scan.folder,
                    scan.uidvalidity,
                    None,
                    None,
                    {
                        "account": paths.account,
                        "old_uidvalidity": old_uidvalidity,
                        "new_uidvalidity": scan.uidvalidity,
                        "meaning": (
                            "The folder UID namespace changed. Per-message "
                            "deletion conclusions were suppressed for this reset."
                        ),
                    },
                )
                database.execute(
                    "UPDATE message_presence SET present = 0, disappeared_at = ? "
                    "WHERE folder = ? AND uidvalidity = ? AND present = 1",
                    (observed_at, scan.folder, old_uidvalidity),
                )

            database.execute(
                "INSERT INTO presence_folders("
                "folder, uidvalidity, is_trash, present, first_seen_at, "
                "last_seen_at, missing_clean_scans, disappeared_at"
                ") VALUES (?, ?, ?, 1, ?, ?, 0, NULL) "
                "ON CONFLICT(folder) DO UPDATE SET "
                "uidvalidity = excluded.uidvalidity, "
                "is_trash = excluded.is_trash, present = 1, "
                "last_seen_at = excluded.last_seen_at, "
                "missing_clean_scans = 0, disappeared_at = NULL",
                (
                    scan.folder,
                    scan.uidvalidity,
                    int(scan.is_trash),
                    observed_at,
                    observed_at,
                ),
            )

        for folder, previous in previous_folders.items():
            if folder in observed_folders or not bool(previous["present"]):
                continue
            missing_scans = int(previous["missing_clean_scans"]) + 1
            if missing_scans < 2:
                database.execute(
                    "UPDATE presence_folders SET missing_clean_scans = ? "
                    "WHERE folder = ?",
                    (missing_scans, folder),
                )
                continue

            database.execute(
                "UPDATE presence_folders SET present = 0, "
                "missing_clean_scans = ?, disappeared_at = ? WHERE folder = ?",
                (missing_scans, observed_at, folder),
            )
            database.execute(
                "UPDATE message_presence SET present = 0, disappeared_at = ? "
                "WHERE folder = ? AND present = 1",
                (observed_at, folder),
            )
            if baseline_exists:
                record_audit_event(
                    database,
                    f"folder_disappeared:{folder}:{previous['uidvalidity']}",
                    "folder_disappeared",
                    observed_at,
                    folder,
                    int(previous["uidvalidity"]),
                    None,
                    None,
                    {
                        "account": paths.account,
                        "last_seen_at": str(previous["last_seen_at"]),
                        "meaning": (
                            "The folder was absent from two complete scans. "
                            "Per-message deletion conclusions were suppressed."
                        ),
                    },
                )

        for scan in scans:
            prior_messages = {
                int(row["uid"]): row
                for row in database.execute(
                    "SELECT * FROM message_presence "
                    "WHERE folder = ? AND uidvalidity = ?",
                    (scan.folder, scan.uidvalidity),
                ).fetchall()
            }
            for uid in scan.current_uids:
                sha256 = archived_message_sha256(
                    database, scan.folder, scan.uidvalidity, uid
                )
                previous = prior_messages.get(uid)
                database.execute(
                    "INSERT INTO message_presence("
                    "folder, uidvalidity, uid, sha256, is_trash, present, "
                    "first_seen_at, last_seen_at, missing_clean_scans, "
                    "disappeared_at"
                    ") VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, NULL) "
                    "ON CONFLICT(folder, uidvalidity, uid) DO UPDATE SET "
                    "sha256 = COALESCE(excluded.sha256, message_presence.sha256), "
                    "is_trash = excluded.is_trash, present = 1, "
                    "last_seen_at = excluded.last_seen_at, "
                    "missing_clean_scans = 0, disappeared_at = NULL",
                    (
                        scan.folder,
                        scan.uidvalidity,
                        uid,
                        sha256,
                        int(scan.is_trash),
                        observed_at,
                        observed_at,
                    ),
                )
                if (
                    baseline_exists
                    and previous is None
                    and scan.is_trash
                    and scan.folder not in reset_folders
                ):
                    record_audit_event(
                        database,
                        f"trash_observed:{scan.folder}:{scan.uidvalidity}:{uid}",
                        "trash_observed",
                        observed_at,
                        scan.folder,
                        scan.uidvalidity,
                        uid,
                        sha256,
                        {
                            "account": paths.account,
                            "meaning": (
                                "A UID appeared in Yahoo Trash after the baseline. "
                                "This does not identify who moved it."
                            ),
                        },
                    )

        for scan in scans:
            visible_uids = set(scan.current_uids)
            missing_rows = database.execute(
                "SELECT * FROM message_presence "
                "WHERE folder = ? AND uidvalidity = ? AND present = 1",
                (scan.folder, scan.uidvalidity),
            ).fetchall()
            for row in missing_rows:
                uid = int(row["uid"])
                if uid in visible_uids:
                    continue
                missing_scans = int(row["missing_clean_scans"]) + 1
                if missing_scans < 2:
                    database.execute(
                        "UPDATE message_presence SET missing_clean_scans = ? "
                        "WHERE folder = ? AND uidvalidity = ? AND uid = ?",
                        (missing_scans, scan.folder, scan.uidvalidity, uid),
                    )
                    continue

                database.execute(
                    "UPDATE message_presence SET present = 0, "
                    "missing_clean_scans = ?, disappeared_at = ? "
                    "WHERE folder = ? AND uidvalidity = ? AND uid = ?",
                    (
                        missing_scans,
                        observed_at,
                        scan.folder,
                        scan.uidvalidity,
                        uid,
                    ),
                )
                sha256 = None if row["sha256"] is None else str(row["sha256"])
                if bool(row["is_trash"]):
                    event_type = "trash_disappeared"
                    meaning = (
                        "The UID was absent from Yahoo Trash in two complete "
                        "scans. This may be manual deletion or Yahoo retention; "
                        "IMAP does not identify the actor."
                    )
                else:
                    same_content_present = False
                    if sha256:
                        same_content_present = (
                            database.execute(
                                "SELECT 1 FROM message_presence "
                                "WHERE sha256 = ? AND present = 1 LIMIT 1",
                                (sha256,),
                            ).fetchone()
                            is not None
                        )
                    if same_content_present:
                        continue
                    event_type = "unexplained_disappearance"
                    meaning = (
                        "The UID was absent from its Yahoo folder in two complete "
                        "scans and no archived copy was correlated in another "
                        "current folder. Cause and actor cannot be determined."
                    )

                record_audit_event(
                    database,
                    f"{event_type}:{scan.folder}:{scan.uidvalidity}:{uid}",
                    event_type,
                    observed_at,
                    scan.folder,
                    scan.uidvalidity,
                    uid,
                    sha256,
                    {
                        "account": paths.account,
                        "last_seen_at": str(row["last_seen_at"]),
                        "confirmed_after_clean_scans": missing_scans,
                        "meaning": meaning,
                    },
                )

        set_health(database, "presence_baseline_complete", "1")
        set_health(database, "last_successful_presence_scan", observed_at)


def stage_audit_event(data: bytes, paths: RuntimePaths, event_key: str) -> Path:
    event_directory = paths.temp_directory / "events"
    event_directory.mkdir(parents=True, exist_ok=True)
    name_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    final_path = event_directory / f"{name_digest}.json"
    partial_path = event_directory / f".{name_digest}.{os.getpid()}.part"
    descriptor = os.open(partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial_path, final_path)
    except Exception:
        try:
            partial_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return final_path


def upload_pending_audit_events(
    database: sqlite3.Connection,
    bucket,
    paths: RuntimePaths,
    limit: int = 100,
) -> None:
    rows = database.execute(
        "SELECT * FROM audit_events WHERE b2_uploaded_at IS NULL "
        "ORDER BY observed_at, event_key LIMIT ?",
        (limit,),
    ).fetchall()

    for row in rows:
        event_key = str(row["event_key"])
        event_type = str(row["event_type"])
        observed_at = str(row["observed_at"])
        payload = {
            "schema_version": 1,
            "account": paths.account,
            "event_key": event_key,
            "event_type": event_type,
            "observed_at": observed_at,
            "folder": row["folder"],
            "uidvalidity": row["uidvalidity"],
            "uid": row["uid"],
            "sha256": row["sha256"],
            "details": json.loads(str(row["details_json"])),
        }
        data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        value = datetime.fromisoformat(observed_at).astimezone(timezone.utc)
        name_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:16]
        safe_type = re.sub(r"[^a-z0-9_-]+", "-", event_type.lower())
        object_name = (
            f"{paths.b2_event_prefix}/{value:%Y}/"
            f"{value:%Y-%m-%d_%H%M%S}_{safe_type}_{name_digest}.json"
        )
        raw_sha1 = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        payload_sha256 = hashlib.sha256(data).hexdigest()

        try:
            staged_path = stage_audit_event(data, paths, event_key)
            uploaded = bucket.upload_local_file(
                str(staged_path),
                object_name,
                content_type="application/json",
                file_info={
                    "sha256": payload_sha256,
                    "eventType": event_type,
                },
                sha1_sum=raw_sha1,
            )
            if uploaded.content_sha1 != raw_sha1:
                raise RuntimeError("B2 audit-event SHA-1 verification failed")
            if uploaded.file_name != object_name or uploaded.size != len(data):
                raise RuntimeError("B2 returned unexpected audit-event metadata")
            if uploaded.file_info.get("sha256") != payload_sha256:
                raise RuntimeError("B2 returned unexpected audit-event SHA-256")

            with database:
                database.execute(
                    "UPDATE audit_events SET b2_object_name = ?, b2_file_id = ?, "
                    "b2_uploaded_at = ?, upload_attempts = upload_attempts + 1, "
                    "last_upload_error = NULL WHERE event_key = ?",
                    (object_name, uploaded.id_, iso_utc(), event_key),
                )
                set_health(database, "last_deletion_event_upload")
            try:
                staged_path.unlink()
            except OSError:
                LOG.warning("Could not remove uploaded audit-event file: %s", staged_path)
        except Exception as error:
            with database:
                database.execute(
                    "UPDATE audit_events SET upload_attempts = upload_attempts + 1, "
                    "last_upload_error = ? WHERE event_key = ?",
                    (
                        f"{type(error).__name__}: {error}"[:1000],
                        event_key,
                    ),
                )
            LOG.exception("Audit-event B2 upload failed; message archiving remains active")
            break


def alert_text(event_type: str, folder: str | None, evidence_id: str) -> str:
    messages = {
        "trash_observed": "A message appeared in Yahoo Trash.",
        "trash_disappeared": (
            "A message disappeared from Yahoo Trash after two complete scans. "
            "This may be manual deletion or Yahoo retention."
        ),
        "unexplained_disappearance": (
            "A message disappeared from a Yahoo folder after two complete scans, "
            "with no correlated current copy in another folder."
        ),
        "message_disappeared_before_fetch": (
            "A Yahoo message disappeared between IMAP search and RFC822 fetch."
        ),
        "uidvalidity_changed": (
            "Yahoo reset a folder's IMAP UID namespace. Message-level deletion "
            "conclusions were suppressed for the reset."
        ),
        "folder_disappeared": (
            "A Yahoo folder was absent from two complete scans. Message-level "
            "deletion conclusions were suppressed."
        ),
    }
    message = messages.get(event_type, f"Yahoo audit event: {event_type}.")
    location = f" Folder: {folder}." if folder else ""
    return f"{message}{location} Evidence: {evidence_id}."


def send_pushover_message(
    config: dict[str, str], title: str, message: str
) -> None:
    fields = {
        "token": config["PUSHOVER_APP_TOKEN"],
        "user": config["PUSHOVER_USER_KEY"],
        "title": title[:250],
        "message": message[:1024],
        "priority": "0",
    }
    if config.get("PUSHOVER_DEVICE"):
        fields["device"] = config["PUSHOVER_DEVICE"]
    request = urllib.request.Request(
        PUSHOVER_MESSAGES_URL,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "yah-archiver/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("status") != 1:
        raise RuntimeError("Pushover rejected the notification")


def send_pending_alerts(
    database: sqlite3.Connection,
    paths: RuntimePaths,
    pushover_config: dict[str, str],
    limit: int = 50,
) -> None:
    rows = database.execute(
        "SELECT event_key, event_type, folder FROM audit_events "
        "WHERE b2_uploaded_at IS NOT NULL AND alerted_at IS NULL "
        "ORDER BY observed_at, event_key LIMIT ?",
        (limit,),
    ).fetchall()
    for row in rows:
        event_key = str(row["event_key"])
        event_type = str(row["event_type"])
        evidence_id = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:12]
        try:
            send_pushover_message(
                pushover_config,
                f"Yahoo archive alert: {paths.account}",
                alert_text(event_type, row["folder"], evidence_id),
            )
            with database:
                database.execute(
                    "UPDATE audit_events SET alerted_at = ?, "
                    "alert_attempts = alert_attempts + 1, last_alert_error = NULL "
                    "WHERE event_key = ?",
                    (iso_utc(), event_key),
                )
                set_health(database, "last_successful_alert")
        except Exception as error:
            with database:
                database.execute(
                    "UPDATE audit_events SET alert_attempts = alert_attempts + 1, "
                    "last_alert_error = ? WHERE event_key = ?",
                    (f"{type(error).__name__}: {error}"[:1000], event_key),
                )
            LOG.exception("Pushover alert failed; message archiving remains active")
            break


def apply_archive_scope(database: sqlite3.Connection, archive_after: date) -> None:
    """Reset live presence state when the operator changes the date cutoff."""

    configured_value = archive_after.isoformat()
    if health_value(database, "archive_after") == configured_value:
        return

    changed_at = iso_utc()
    with database:
        database.execute("DELETE FROM message_presence")
        database.execute("DELETE FROM presence_folders")
        database.execute("DELETE FROM message_failures")
        database.execute("DELETE FROM folder_state")
        database.execute("DELETE FROM audit_events WHERE b2_uploaded_at IS NULL")
        database.execute(
            "UPDATE audit_events SET alerted_at = ?, "
            "last_alert_error = 'suppressed: archive date scope changed' "
            "WHERE b2_uploaded_at IS NOT NULL AND alerted_at IS NULL",
            (changed_at,),
        )
        set_health(database, "presence_baseline_complete", "0")
        set_health(database, "archive_after", configured_value)


def suppress_ignored_folder_state(database: sqlite3.Connection) -> None:
    """Remove active spam state and silence already-queued spam alerts."""

    folders = {
        str(row["folder"])
        for row in database.execute(
            "SELECT folder FROM presence_folders "
            "UNION SELECT folder FROM message_presence "
            "UNION SELECT folder FROM message_failures "
            "UNION SELECT folder FROM folder_state "
            "UNION SELECT folder FROM audit_events WHERE folder IS NOT NULL"
        ).fetchall()
        if row["folder"] is not None and folder_name_is_ignored(str(row["folder"]))
    }
    if not folders:
        return

    suppressed_at = iso_utc()
    with database:
        for folder in folders:
            database.execute("DELETE FROM message_presence WHERE folder = ?", (folder,))
            database.execute("DELETE FROM presence_folders WHERE folder = ?", (folder,))
            database.execute("DELETE FROM message_failures WHERE folder = ?", (folder,))
            database.execute("DELETE FROM folder_state WHERE folder = ?", (folder,))
            # Events that never reached immutable storage were false positives
            # from folders now explicitly outside the archive scope.
            database.execute(
                "DELETE FROM audit_events "
                "WHERE folder = ? AND b2_uploaded_at IS NULL",
                (folder,),
            )
            # Preserve already-uploaded evidence records but prevent any queued
            # Pushover notification for those ignored folders.
            database.execute(
                "UPDATE audit_events SET alerted_at = ?, "
                "last_alert_error = 'suppressed: ignored spam folder' "
                "WHERE folder = ? AND b2_uploaded_at IS NOT NULL "
                "AND alerted_at IS NULL",
                (suppressed_at, folder),
            )


def archive_cycle(
    client: imaplib.IMAP4_SSL,
    database: sqlite3.Connection,
    bucket,
    paths: RuntimePaths,
    pushover_config: dict[str, str],
    max_messages: int | None,
    backfill_batch_size: int,
    archive_since: date,
    destination_id: str,
) -> tuple[int, int]:
    total_attempted = 0
    total_uploaded = 0
    presence_scans: list[MailboxScan] = []
    complete_scan = max_messages is None
    mailboxes = list_selectable_mailboxes(client)
    apply_archive_scope(database, archive_since - timedelta(days=1))
    suppress_ignored_folder_state(database)
    LOG.info(
        "Scanning %s Yahoo folders since %s (Bulk/Spam/Junk ignored)",
        len(mailboxes),
        archive_since.isoformat(),
    )

    for mailbox in mailboxes:
        if STOP_REQUESTED:
            complete_scan = False
            break
        remaining = None if max_messages is None else max_messages - total_attempted
        if remaining is not None and remaining <= 0:
            complete_scan = False
            break
        try:
            attempted, uploaded = archive_mailbox(
                client,
                database,
                bucket,
                paths,
                mailbox,
                remaining,
                backfill_batch_size,
                archive_since,
                destination_id,
                presence_scans,
            )
        except (CycleAbortError, ImapCommandError, imaplib.IMAP4.abort, OSError):
            raise
        except Exception:
            complete_scan = False
            LOG.exception("Folder scan failed; continuing with other folders: %s", mailbox.key)
            continue
        total_attempted += attempted
        total_uploaded += uploaded

    LOG.info(
        "Scan complete: attempted=%s newly-uploaded=%s",
        total_attempted,
        total_uploaded,
    )

    if len(presence_scans) != len(mailboxes):
        complete_scan = False

    if complete_scan and not STOP_REQUESTED:
        with database:
            set_health(database, "last_successful_archive_cycle")
        try:
            reconcile_presence(database, paths, presence_scans)
        except Exception:
            LOG.exception(
                "Presence reconciliation failed; message archiving remains active"
            )

    try:
        upload_pending_audit_events(database, bucket, paths)
    except Exception:
        LOG.exception("Audit-event processing failed; message archiving remains active")

    try:
        send_pending_alerts(database, paths, pushover_config)
    except Exception:
        LOG.exception("Pushover processing failed; message archiving remains active")

    return total_attempted, total_uploaded


def connect_yahoo(config: dict[str, str], source: Path) -> imaplib.IMAP4_SSL:
    require_settings(config, ("YAHOO_USERNAME", "YAHOO_APP_PASSWORD"), source)
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context, timeout=60)
    password = config["YAHOO_APP_PASSWORD"].replace(" ", "")
    status, _data = client.login(config["YAHOO_USERNAME"], password)
    if status != "OK":
        raise RuntimeError(f"Yahoo IMAP login failed: {status}")
    LOG.info("Connected to Yahoo IMAP as %s", config["YAHOO_USERNAME"])
    return client


def close_imap(client: imaplib.IMAP4_SSL | None) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass


def request_stop(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOG.info("Stop requested by signal %s", signum)


def wait_interruptibly(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not STOP_REQUESTED:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive Yahoo IMAP mail to Backblaze B2")
    parser.add_argument(
        "--account",
        required=True,
        type=validate_account_name,
        help="short account ID used for isolated config, state, and B2 paths",
    )
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument(
        "--max-messages",
        type=int,
        help="attempt at most this many messages during the scan (for testing)",
    )
    args = parser.parse_args(argv)
    if args.max_messages is not None and args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    if args.max_messages is not None and not args.once:
        parser.error("--max-messages requires --once")
    return args


def run(argv: list[str]) -> int:
    args = parse_arguments(argv)
    paths = runtime_paths(args.account)
    b2_config = read_env(paths.b2_config_path)
    settings_config = read_optional_env(SETTINGS_CONFIG_PATH)
    yahoo_config = read_env(paths.yahoo_config_path)
    pushover_config = read_env(paths.pushover_config_path)
    require_settings(
        pushover_config,
        ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY"),
        paths.pushover_config_path,
    )
    poll_seconds = parse_positive_int(
        yahoo_config.get("POLL_SECONDS"), DEFAULT_POLL_SECONDS, "POLL_SECONDS"
    )
    retry_seconds = parse_positive_int(
        yahoo_config.get("RETRY_SECONDS"), DEFAULT_RETRY_SECONDS, "RETRY_SECONDS"
    )
    backfill_batch_size = parse_positive_int(
        yahoo_config.get("BACKFILL_BATCH_SIZE"),
        DEFAULT_BACKFILL_BATCH_SIZE,
        "BACKFILL_BATCH_SIZE",
    )
    archive_after = parse_archive_after(settings_config.get("ARCHIVE_AFTER"))
    archive_since = archive_after + timedelta(days=1)

    database = initialize_database(paths.database_path)
    bucket = create_b2_client(b2_config, paths.b2_config_path)
    destination_id = str(bucket.id_)
    initialize_archive_destination(database, destination_id)
    LOG.info(
        "B2 authorization succeeded: account=%s bucket=%s destination=%s "
        "prefix=%s archive-since=%s",
        paths.account,
        b2_config["B2_BUCKET"],
        destination_id,
        paths.b2_message_prefix,
        archive_since.isoformat(),
    )

    if args.once:
        client = None
        try:
            client = connect_yahoo(yahoo_config, paths.yahoo_config_path)
            archive_cycle(
                client,
                database,
                bucket,
                paths,
                pushover_config,
                args.max_messages,
                backfill_batch_size,
                archive_since,
                destination_id,
            )
            return 0
        finally:
            close_imap(client)
            database.close()

    while not STOP_REQUESTED:
        client = None
        try:
            client = connect_yahoo(yahoo_config, paths.yahoo_config_path)
            while not STOP_REQUESTED:
                archive_cycle(
                    client,
                    database,
                    bucket,
                    paths,
                    pushover_config,
                    None,
                    backfill_batch_size,
                    archive_since,
                    destination_id,
                )
                wait_interruptibly(poll_seconds)
                if not STOP_REQUESTED:
                    status, _data = client.noop()
                    if status != "OK":
                        raise RuntimeError(f"Yahoo IMAP NOOP failed: {status}")
        except Exception:
            LOG.exception("Archiver cycle failed; retrying in %s seconds", retry_seconds)
            close_imap(client)
            wait_interruptibly(retry_seconds)
        else:
            close_imap(client)

    database.close()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        return run(sys.argv[1:])
    except Exception:
        LOG.exception("Fatal startup error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
