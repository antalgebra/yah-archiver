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
import logging
import os
import re
import signal
import sqlite3
import ssl
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from b2sdk.v3 import AuthInfoCache, B2Api, InMemoryAccountInfo


B2_CONFIG_PATH = Path("/etc/yah-arch/b2.env")
YAHOO_CONFIG_PATH = Path("/etc/yah-arch/yahoo.env")
DATABASE_PATH = Path("/var/lib/yah-arch/data/catalog.sqlite3")
TEMP_DIRECTORY = Path("/var/lib/yah-arch/tmp")

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
DEFAULT_POLL_SECONDS = 20
DEFAULT_RETRY_SECONDS = 30

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


def initialize_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
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
            checked_at TEXT NOT NULL
        );
        """
    )
    return connection


def create_b2_client(config: dict[str, str]):
    require_settings(config, ("B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"), B2_CONFIG_PATH)
    account_info = InMemoryAccountInfo()
    api = B2Api(account_info, cache=AuthInfoCache(account_info))
    api.authorize_account(
        application_key_id=config["B2_KEY_ID"],
        application_key=config["B2_APPLICATION_KEY"],
    )
    return api.get_bucket_by_name(config["B2_BUCKET"])


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
        if mailbox is not None:
            mailboxes.append(mailbox)

    def priority(mailbox: Mailbox) -> tuple[int, str]:
        upper_name = mailbox.key.upper()
        if upper_name == "INBOX":
            rank = 0
        elif "\\SENT" in mailbox.flags or upper_name in {"SENT", "SENT ITEMS"}:
            rank = 1
        elif "\\TRASH" in mailbox.flags or upper_name == "TRASH":
            rank = 3
        else:
            rank = 2
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


def get_folder_state(
    database: sqlite3.Connection, folder: str, uidvalidity: int
) -> int:
    row = database.execute(
        "SELECT uidvalidity, last_uid FROM folder_state WHERE folder = ?", (folder,)
    ).fetchone()

    if row is None:
        with database:
            database.execute(
                "INSERT INTO folder_state(folder, uidvalidity, last_uid, checked_at) "
                "VALUES (?, ?, 0, ?)",
                (folder, uidvalidity, iso_utc()),
            )
        return 0

    if row["uidvalidity"] != uidvalidity:
        LOG.warning(
            "UIDVALIDITY changed for %s: %s -> %s; rescanning folder",
            folder,
            row["uidvalidity"],
            uidvalidity,
        )
        with database:
            database.execute(
                "UPDATE folder_state SET uidvalidity = ?, last_uid = 0, checked_at = ? "
                "WHERE folder = ?",
                (uidvalidity, iso_utc(), folder),
            )
        return 0

    return int(row["last_uid"])


def update_folder_state(
    database: sqlite3.Connection, folder: str, uidvalidity: int, last_uid: int
) -> None:
    database.execute(
        "UPDATE folder_state SET uidvalidity = ?, last_uid = ?, checked_at = ? "
        "WHERE folder = ?",
        (uidvalidity, last_uid, iso_utc(), folder),
    )


def search_new_uids(client: imaplib.IMAP4_SSL, last_uid: int) -> list[int]:
    start_uid = last_uid + 1
    status, data = client.uid("SEARCH", None, f"UID {start_uid}:*")
    if status != "OK" or data is None:
        raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
    values = data[0].split() if data and isinstance(data[0], bytes) else []
    return sorted(uid for uid in (int(value) for value in values) if uid > last_uid)


def fetch_raw_message(
    client: imaplib.IMAP4_SSL, uid: int
) -> tuple[bytes, bytes] | None:
    status, parts = client.uid("FETCH", str(uid), "(UID INTERNALDATE BODY.PEEK[])")
    if status != "OK" or parts is None:
        raise RuntimeError(f"IMAP UID FETCH failed for UID {uid}: {status}")

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
    value = message.get(name)
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


def make_object_name(metadata: MessageMetadata, sha256: str) -> str:
    value = metadata.internal_date.astimezone(timezone.utc)
    return (
        f"mail/{value:%Y/%m/%d}/{value:%H%M%S}_"
        f"{safe_subject(metadata.subject)}_{sha256}.eml"
    )


def stage_message(raw_message: bytes, sha256: str) -> Path:
    TEMP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    final_path = TEMP_DIRECTORY / f"{sha256}.eml"

    if final_path.exists():
        existing_hash = hashlib.sha256(final_path.read_bytes()).hexdigest()
        if existing_hash != sha256:
            raise RuntimeError(f"Temporary file hash mismatch: {final_path}")
        return final_path

    partial_path = TEMP_DIRECTORY / f".{sha256}.{os.getpid()}.part"
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


def find_archived_object(database: sqlite3.Connection, sha256: str):
    return database.execute(
        "SELECT object_name, b2_file_id, b2_sha1, size_bytes "
        "FROM archive_objects WHERE sha256 = ?",
        (sha256,),
    ).fetchone()


def upload_and_verify(bucket, staged_path: Path, object_name: str, sha256: str):
    raw_sha1 = hashlib.sha1(staged_path.read_bytes(), usedforsecurity=False).hexdigest()
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


def archive_message(
    database: sqlite3.Connection,
    bucket,
    mailbox: Mailbox,
    uidvalidity: int,
    uid: int,
    fetch_metadata: bytes,
    raw_message: bytes,
) -> bool:
    sha256 = hashlib.sha256(raw_message).hexdigest()
    metadata = message_metadata(raw_message, fetch_metadata)
    existing = find_archived_object(database, sha256)

    if existing is not None:
        with database:
            record_occurrence(database, mailbox.key, uidvalidity, uid, sha256, metadata)
            update_folder_state(database, mailbox.key, uidvalidity, uid)
        LOG.info("Already archived: folder=%s uid=%s sha256=%s", mailbox.key, uid, sha256)
        return False

    object_name = make_object_name(metadata, sha256)
    staged_path = stage_message(raw_message, sha256)
    uploaded = upload_and_verify(bucket, staged_path, object_name, sha256)

    with database:
        database.execute(
            "INSERT INTO archive_objects("
            "sha256, object_name, size_bytes, b2_file_id, b2_sha1, archived_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                sha256,
                object_name,
                len(raw_message),
                uploaded.id_,
                uploaded.content_sha1,
                iso_utc(),
            ),
        )
        record_occurrence(database, mailbox.key, uidvalidity, uid, sha256, metadata)
        update_folder_state(database, mailbox.key, uidvalidity, uid)

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


def archive_mailbox(
    client: imaplib.IMAP4_SSL,
    database: sqlite3.Connection,
    bucket,
    mailbox: Mailbox,
    remaining_limit: int | None,
) -> tuple[int, int]:
    status, _data = client.select(mailbox.select_argument, readonly=True)
    if status != "OK":
        raise RuntimeError(f"IMAP read-only SELECT failed for {mailbox.key}: {status}")

    uidvalidity = selected_uidvalidity(client)
    last_uid = get_folder_state(database, mailbox.key, uidvalidity)
    uids = search_new_uids(client, last_uid)
    fetched = 0
    uploaded = 0

    for uid in uids:
        if STOP_REQUESTED or (remaining_limit is not None and fetched >= remaining_limit):
            break

        fetched_message = fetch_raw_message(client, uid)
        if fetched_message is None:
            LOG.warning(
                "Message disappeared before fetch: folder=%s uid=%s", mailbox.key, uid
            )
            with database:
                update_folder_state(database, mailbox.key, uidvalidity, uid)
            continue

        fetch_metadata, raw_message = fetched_message
        if not raw_message:
            raise RuntimeError(f"Yahoo returned an empty message for {mailbox.key} UID {uid}")

        if archive_message(
            database,
            bucket,
            mailbox,
            uidvalidity,
            uid,
            fetch_metadata,
            raw_message,
        ):
            uploaded += 1
        fetched += 1

    with database:
        database.execute(
            "UPDATE folder_state SET checked_at = ? WHERE folder = ?",
            (iso_utc(), mailbox.key),
        )
    return fetched, uploaded


def archive_cycle(
    client: imaplib.IMAP4_SSL,
    database: sqlite3.Connection,
    bucket,
    max_messages: int | None,
) -> tuple[int, int]:
    total_fetched = 0
    total_uploaded = 0
    mailboxes = list_selectable_mailboxes(client)
    LOG.info("Scanning %s selectable Yahoo folders", len(mailboxes))

    for mailbox in mailboxes:
        if STOP_REQUESTED:
            break
        remaining = None if max_messages is None else max_messages - total_fetched
        if remaining is not None and remaining <= 0:
            break
        fetched, uploaded = archive_mailbox(
            client, database, bucket, mailbox, remaining
        )
        total_fetched += fetched
        total_uploaded += uploaded

    LOG.info(
        "Scan complete: fetched=%s newly-uploaded=%s", total_fetched, total_uploaded
    )
    return total_fetched, total_uploaded


def connect_yahoo(config: dict[str, str]) -> imaplib.IMAP4_SSL:
    require_settings(config, ("YAHOO_USERNAME", "YAHOO_APP_PASSWORD"), YAHOO_CONFIG_PATH)
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
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument(
        "--max-messages",
        type=int,
        help="fetch at most this many messages during the scan (for testing)",
    )
    args = parser.parse_args(argv)
    if args.max_messages is not None and args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    if args.max_messages is not None and not args.once:
        parser.error("--max-messages requires --once")
    return args


def run(argv: list[str]) -> int:
    args = parse_arguments(argv)
    b2_config = read_env(B2_CONFIG_PATH)
    yahoo_config = read_env(YAHOO_CONFIG_PATH)
    poll_seconds = parse_positive_int(
        yahoo_config.get("POLL_SECONDS"), DEFAULT_POLL_SECONDS, "POLL_SECONDS"
    )
    retry_seconds = parse_positive_int(
        yahoo_config.get("RETRY_SECONDS"), DEFAULT_RETRY_SECONDS, "RETRY_SECONDS"
    )

    database = initialize_database(DATABASE_PATH)
    bucket = create_b2_client(b2_config)
    LOG.info("B2 authorization succeeded for bucket %s", b2_config["B2_BUCKET"])

    if args.once:
        client = None
        try:
            client = connect_yahoo(yahoo_config)
            archive_cycle(client, database, bucket, args.max_messages)
            return 0
        finally:
            close_imap(client)
            database.close()

    while not STOP_REQUESTED:
        client = None
        try:
            client = connect_yahoo(yahoo_config)
            while not STOP_REQUESTED:
                archive_cycle(client, database, bucket, None)
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
