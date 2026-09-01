# yah-archiver

An always-on, read-only Yahoo Mail collector that preserves complete RFC822
messages as `.eml` objects in a private Backblaze B2 bucket.

This repository owns two related guarantees:

1. **Archive durability:** fetch exact message bytes and verify the B2 upload.
2. **Deletion evidence:** record mailbox-presence changes without changing Yahoo.

Deletion evidence shares the collector's coherent IMAP scan but has separate
state and health timestamps. Evidence is stored in SQLite and copied to the
Object-Locked B2 bucket. After that upload succeeds, a compact Pushover alert is
sent with title `YahArch:<first 7 account characters>` and the first 50
normalized characters of the visible message body. Plain text is preferred;
attachments and HTML markup are excluded. This deliberately shares that short
excerpt with Pushover. Messages archived before preview support may instead say
`Body unavailable`. The companion
`yah-archive-ops` project monitors archive health, `yah-deleted-onedrive`
provides a copy-only OneDrive mirror, and `yah-archive-crawler` is reserved for
read-only analysis.

## Safety model

- Each Yahoo account runs as a separate service instance with its own credential
  file, SQLite catalog, temporary directory, and B2 namespace.
- Yahoo IMAP is used only for `LIST`, read-only `SELECT`, date-limited
  `UID SEARCH`, and `UID FETCH` operations.
- Yahoo Bulk/Spam/Junk folders are searched, archived, and tracked as quarantine
  folders. Their arrivals and disappearances do not generate deletion alerts.
- The archiver never marks, moves, deletes, or expunges Yahoo messages.
- A message is recorded in SQLite only after B2 accepts and verifies its upload.
- Removing a message from Yahoo never removes its archived B2 object.
- SHA-256 identifies message content; folder, UIDVALIDITY, and UID are retained
  for audit history.
- Backblaze Object Lock supplies immutable retention independently of this code.
- Deduplication performs one indexed SHA-256 lookup in the local SQLite catalog.
  It does not list B2 objects or interpret old and new object-name layouts.
- Upload verification calculates checksums from the message bytes already in
  memory instead of rereading the staged file before each upload.

This remains a best-effort IMAP archive: no poller can recover a message that is
permanently deleted before its first successful fetch. Keep external health
monitoring enabled so IMAP, B2, or notification failures are surfaced promptly.

## Catch-up behavior

- The global `ARCHIVE_AFTER` setting excludes messages dated on or before the
  chosen date. Yahoo applies the cutoff server-side using IMAP `SINCE`.
- Changing the cutoff resets only live scan/presence state, preventing older
  catalog entries from creating disappearance alerts. Existing B2 objects and
  durable catalog history are retained.
- Trash is scanned first, followed by Bulk/Spam/Junk, Inbox, Sent, and other
  folders.
- On the first scan of a folder, existing messages are archived newest first.
- Historical catch-up is limited to `BACKFILL_BATCH_SIZE` messages per folder per
  cycle (default `10`), so the service returns quickly to checking for new mail.
- Inbox, Sent, Trash, and Bulk/Spam/Junk are scanned every `POLL_SECONDS`
  (default `5` seconds). New Trash arrivals are recorded and alerted from these
  rapid scans unless Spam/Junk remains the content's latest meaningful
  classification.
- After a folder's historical backfill completes, rapid scans ask Yahoo only for
  UIDs above the folder's durable live cursor. The complete date-qualified UID
  snapshot is still retrieved during every full scan. When Yahoo's `UIDNEXT`
  response proves that no new UID exists, the rapid pass skips `SEARCH`
  entirely.
- All other included folders and complete disappearance reconciliation run every
  `FULL_SCAN_SECONDS` (default `60` seconds).
- The IMAP folder list is cached between full scans instead of being requested
  every five seconds.
- Arrival-only reconciliation writes presence state only for new or successfully
  retried messages. Existing rows are refreshed by the complete scan, avoiding
  thousands of redundant SQLite statements in large folders. Idle rapid passes
  retain the health heartbeat without producing routine `INFO` log noise.
- New messages are always attempted before historical catch-up.
- A message-specific parse or upload failure is written to `message_failures` and
  does not block later UIDs. Failed messages are retried in bounded batches.
- Three consecutive message failures stop the cycle and trigger the normal retry
  delay, preventing a broad outage from creating an unbounded failure queue.
- An IMAP UID visible during `SEARCH` but gone before `FETCH` is retained as a
  non-retryable `disappeared_before_fetch` record.

## Deletion evidence

- The first complete scan establishes a baseline without generating a flood of
  historical Trash events.
- A new UID appearing in Trash creates a `trash_observed` event unless the same
  content is still classified as Spam/Junk.
- Spam/Junk arrivals, disappearances, folder resets, and folder disappearance do
  not create alerts.
- A missing message is not classified until it is absent from two complete,
  successful scans.
- A confirmed disappearance from Trash creates `trash_disappeared` unless the
  same content is still classified as Spam/Junk.
- A confirmed disappearance from another folder creates
  `unexplained_disappearance` only when the same archived content is not visible
  in another current folder and is not still classified as Spam/Junk.
- Spam/Junk remains non-actionable until a later appearance in Inbox, Sent, or
  another ordinary non-Trash folder rescues the content. This suppresses
  `Inbox -> Spam/Junk -> Trash -> deleted`, while restoring protection for
  `Spam/Junk -> Inbox -> Trash -> deleted`.
- Previously queued, unsent disappearance or Trash alerts are suppressed when a
  later Spam/Junk scan correlates the same SHA-256 content.
- Folder disappearance and UIDVALIDITY reset have their own events and suppress
  mass per-message conclusions.
- Events describe observable facts only. IMAP cannot establish who acted, and a
  Trash disappearance may be manual deletion or Yahoo's automatic retention.
- JSON evidence objects are uploaded under `mail/<account>/events/...` and receive
  the bucket's default Object Lock retention.
- Event-upload or reconciliation errors never cause archived messages to be
  removed or changed.
- Alerts are normal-priority Pushover notifications and are retried after
  delivery failures. Archiving continues while Pushover is unavailable.

## Multi-account layout

For an account ID such as `personal`:

| Purpose | Location |
|---|---|
| Shared B2 credentials | `/etc/yah-arch/b2.env` |
| Shared archive settings | `/etc/yah-arch/settings.env` |
| Yahoo credentials | `/etc/yah-arch/accounts/personal.env` |
| Shared Pushover credentials | `/etc/yah-arch/pushover.env` |
| SQLite catalog | `/var/lib/yah-arch/data/personal.sqlite3` |
| Temporary message staging | `/var/lib/yah-arch/tmp/personal/` |
| B2 messages | `mail/personal/messages/YYYY/YYYY-MM-DD_HHMMSS_FOLDER_SUBJECT_HASH16.eml` |
| B2 audit events | `mail/personal/events/YYYY/YYYY-MM-DD_HHMMSS_...json` |
| systemd instance | `yah-arch@personal.service` |

Account IDs may contain 1–32 lowercase letters, numbers, underscores, or
hyphens, and must begin with a letter or number.

The B2 application key can be shared by the account instances for now. It should
permit upload only under `mail/` and must not have B2 hard-delete capability.
Messages and audit events use year folders only. The month and day remain in the
filename so the bucket stays easy to browse without deep date subfolders.
Message filenames also show the Yahoo folder where the message was first
archived, such as `Inbox` or `Sent`. The filename uses only the first 16
characters of the SHA-256 fingerprint for readability; the complete SHA-256
remains in SQLite and B2 file metadata for verification and deduplication.

## Files kept outside Git

Never commit application keys, Yahoo app passwords, catalogs, or archived mail.
The repository `.gitignore` excludes `.env`, `.eml`, and SQLite runtime files.
Use [`config/account.env.example`](config/account.env.example) only as a field
reference.

## Runtime

- Ubuntu 24.04
- Python 3.12
- `b2sdk` from `requirements.txt`

## Change the date cutoff or B2 destination

Run the global settings wizard:

```bash
sudo /opt/yah-arch/venv/bin/python /opt/yah-arch/src/settings.py
```

The date option asks for the latest date to ignore. For example, entering
`2026-08-30` makes `2026-08-31` the first included IMAP date. The cutoff
applies to every configured Yahoo account and can be changed again at any time.

The B2 option accepts a new application-key ID, hidden application key, and
bucket name. It validates authentication, bucket access, Object Lock, and
`writeFiles` permission before atomically replacing the protected
configuration. Running account services are then restarted automatically.

The SQLite catalog tracks verified copies by B2 bucket ID. Changing to another
bucket therefore creates a new destination copy of every date-qualified message
encountered, even when that content was already archived to the former bucket.
Old immutable objects are never deleted or moved.

## Add a Yahoo account

After the one-time B2 and Pushover setup is complete, run the interactive
wizard on the archive server:

```bash
sudo /opt/yah-arch/venv/bin/python /opt/yah-arch/src/onboard.py
```

The wizard accepts either the Yahoo account name or its full address. For
example, `--account hoffmas` automatically uses the service ID `hoffmas` and
the address `hoffmas@yahoo.com`, eliminating duplicate entry. It shows the
current Yahoo app-password steps and accepts the password through hidden input.

Before saving anything, the wizard verifies the address and app password by
logging in to Yahoo IMAP over TLS. After verification, it writes the account
file atomically as `root:yaharch` with mode `0640`, installs the systemd service
template, and starts the account's always-on service without additional
confirmation prompts. It never asks for the normal Yahoo password.

Run the same command again for every additional account. If the derived account
ID already exists, the wizard recognizes it as a credential refresh, preserves
its optional polling settings, verifies the replacement app password, and then
restarts the existing service. No credentials are written to this Git
repository.

Show the command interface:

```bash
python3 archiver.py --help
```

The service template is [`deploy/yah-arch@.service`](deploy/yah-arch@.service).
It should not be installed or enabled until notification delivery is configured.
