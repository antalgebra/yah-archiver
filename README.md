# yah-archiver

A small, read-only Yahoo Mail archiver that preserves complete RFC822 messages
as `.eml` objects in a private Backblaze B2 bucket.

## Safety model

- Yahoo IMAP is used only for `LIST`, read-only `SELECT`, `UID SEARCH`, and
  `UID FETCH` operations.
- The archiver never marks, moves, deletes, or expunges Yahoo messages.
- A message is recorded in SQLite only after B2 accepts and verifies its
  upload.
- Removing a message from Yahoo never removes its archived B2 object.
- SHA-256 identifies message content; Yahoo folder, UIDVALIDITY, and UID are
  retained for audit history.
- Backblaze Object Lock supplies immutable retention independently of this
  program.

This is a best-effort IMAP archive. No IMAP poller can recover a message that
is permanently deleted before its first successful fetch.

## Files kept outside Git

Runtime credentials and data belong on the server, not in this repository:

- `/etc/yah-arch/b2.env`
- `/etc/yah-arch/yahoo.env`
- `/var/lib/yah-arch/data/catalog.sqlite3`
- `/var/lib/yah-arch/tmp/`

Never commit application keys, passwords, catalogs, or archived email.

## Runtime

- Ubuntu 24.04
- Python 3.12
- `b2sdk` from `requirements.txt`

The controlled test command is:

```bash
/opt/yah-arch/venv/bin/python /opt/yah-arch/src/archiver.py --once --max-messages 1
```

Do not run that test until the server credential files have been created with
restricted permissions.
