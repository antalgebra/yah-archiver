# yah-archiver

An always-on, read-only Yahoo Mail collector that preserves complete RFC822
messages as `.eml` objects in a private Backblaze B2 bucket.

This repository owns two related guarantees:

1. **Archive durability:** fetch exact message bytes and verify the B2 upload.
2. **Deletion evidence:** record mailbox-presence changes without changing Yahoo.

Deletion evidence is not implemented yet. It will share the collector's coherent
IMAP scan, but it will have its own state, tests, and health signal. A later
`yah-archive-ops` project will provide B2 verification and a copy-only OneDrive
mirror; a separate `yah-archive-crawler` project will provide read-only analysis.

## Safety model

- Each Yahoo account runs as a separate service instance with its own credential
  file, SQLite catalog, temporary directory, and B2 namespace.
- Yahoo IMAP is used only for `LIST`, read-only `SELECT`, `UID SEARCH`, and
  `UID FETCH` operations.
- The archiver never marks, moves, deletes, or expunges Yahoo messages.
- A message is recorded in SQLite only after B2 accepts and verifies its upload.
- Removing a message from Yahoo never removes its archived B2 object.
- SHA-256 identifies message content; folder, UIDVALIDITY, and UID are retained
  for audit history.
- Backblaze Object Lock supplies immutable retention independently of this code.

This remains a best-effort IMAP archive: no poller can recover a message that is
permanently deleted before its first successful fetch. Do not connect production
Yahoo accounts until the initial-backfill and failure-isolation work is complete.

## Multi-account layout

For an account ID such as `personal`:

| Purpose | Location |
|---|---|
| Shared B2 credentials | `/etc/yah-arch/b2.env` |
| Yahoo credentials | `/etc/yah-arch/accounts/personal.env` |
| SQLite catalog | `/var/lib/yah-arch/data/personal.sqlite3` |
| Temporary message staging | `/var/lib/yah-arch/tmp/personal/` |
| B2 messages | `mail/personal/messages/...` |
| systemd instance | `yah-arch@personal.service` |

Account IDs may contain 1–32 lowercase letters, numbers, underscores, or
hyphens, and must begin with a letter or number.

The B2 application key can be shared by the account instances for now. It should
permit upload only under `mail/` and must not have B2 hard-delete capability.

## Files kept outside Git

Never commit application keys, Yahoo app passwords, catalogs, or archived mail.
The repository `.gitignore` excludes `.env`, `.eml`, and SQLite runtime files.
Use [`config/account.env.example`](config/account.env.example) only as a field
reference.

## Runtime and tests

- Ubuntu 24.04
- Python 3.12
- `b2sdk` from `requirements.txt`

Run the dependency-free unit tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Show the command interface:

```bash
python3 archiver.py --help
```

The future controlled live test for one account will be:

```bash
/opt/yah-arch/venv/bin/python /opt/yah-arch/src/archiver.py \
  --account personal --once --max-messages 1
```

Do not run that live test until its Yahoo credential file exists with restricted
permissions and the production-readiness checkpoints above are complete.

The service template is [`deploy/yah-arch@.service`](deploy/yah-arch@.service).
It is committed for review but should not be installed or enabled yet.

