#!/usr/bin/env python3
"""Interactive global settings wizard for Yahoo Mail Archiver."""

from __future__ import annotations

import getpass
import grp
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

CONFIG_DIRECTORY = Path("/etc/yah-arch")
ACCOUNTS_DIRECTORY = CONFIG_DIRECTORY / "accounts"
B2_CONFIG_PATH = CONFIG_DIRECTORY / "b2.env"
SETTINGS_CONFIG_PATH = CONFIG_DIRECTORY / "settings.env"
DEFAULT_ARCHIVE_AFTER = date(2026, 8, 30)


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() and value.strip():
            values[name.strip()] = value.strip()
    return values


def safe_value(value: str, name: str) -> str:
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} cannot be empty or contain a newline")
    return value


def write_protected_env(path: Path, values: dict[str, str], order: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [name for name in order if name in values]
    names.extend(sorted(name for name in values if name not in names))
    data = "".join(f"{name}={values[name]}\n" for name in names)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")

    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        group_id = grp.getgrnam("yaharch").gr_gid
        os.chown(temporary, 0, group_id)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def account_units() -> list[str]:
    return [
        f"yah-arch@{path.stem}.service"
        for path in sorted(ACCOUNTS_DIRECTORY.glob("*.env"))
    ]


def restart_running_archivers() -> None:
    units = account_units()
    if not units:
        return
    subprocess.run(["systemctl", "try-restart", *units], check=True)
    print(f"Applied settings to {len(units)} configured account(s).")


def current_archive_after() -> date:
    value = read_env(SETTINGS_CONFIG_PATH).get("ARCHIVE_AFTER")
    if not value:
        return DEFAULT_ARCHIVE_AFTER
    return date.fromisoformat(value)


def change_archive_date() -> None:
    current = current_archive_after()
    print("\nEmail date cutoff")
    print("Messages dated on or before this date are completely ignored.")
    entered = input(f"Latest date to ignore [{current.isoformat()}]: ").strip()
    selected = date.fromisoformat(entered) if entered else current

    values = read_env(SETTINGS_CONFIG_PATH)
    values["ARCHIVE_AFTER"] = selected.isoformat()
    write_protected_env(SETTINGS_CONFIG_PATH, values, ["ARCHIVE_AFTER"])
    restart_running_archivers()
    print(
        "Saved. Yahoo IMAP searches will include messages starting "
        f"{(selected + timedelta(days=1)).isoformat()}."
    )


def validate_b2(key_id: str, application_key: str, bucket_name: str):
    from b2sdk.v3 import AuthInfoCache, B2Api, InMemoryAccountInfo

    account_info = InMemoryAccountInfo()
    api = B2Api(account_info, cache=AuthInfoCache(account_info))
    api.authorize_account(
        application_key_id=key_id,
        application_key=application_key,
    )
    bucket = api.get_bucket_by_name(bucket_name)

    allowed = account_info.get_allowed()
    capabilities = set(allowed.get("capabilities") or [])
    if "writeFiles" not in capabilities:
        raise ValueError("This application key does not have writeFiles permission")

    bucket_details = getattr(bucket, "bucket_dict", {}) or {}
    if bucket_details.get("isFileLockEnabled") is False:
        raise ValueError("The selected bucket does not have Object Lock enabled")

    return bucket, capabilities


def change_b2_destination() -> None:
    current = read_env(B2_CONFIG_PATH)
    print("\nBackblaze B2 destination")
    print("Create a bucket-restricted application key before continuing.")
    print("The key needs writeFiles permission and should not have deleteFiles permission.")
    print("Input for the application key is hidden.")

    current_key_id = current.get("B2_KEY_ID", "")
    current_bucket = current.get("B2_BUCKET", "")
    key_prompt = f"B2 key ID [{current_key_id}]: " if current_key_id else "B2 key ID: "
    bucket_prompt = (
        f"B2 bucket name [{current_bucket}]: "
        if current_bucket
        else "B2 bucket name: "
    )

    key_id = safe_value(input(key_prompt).strip() or current_key_id, "B2 key ID")
    entered_key = getpass.getpass(
        "B2 application key [press Enter to keep the current key]: "
    )
    application_key = safe_value(
        entered_key or current.get("B2_APPLICATION_KEY", ""),
        "B2 application key",
    )
    bucket_name = safe_value(
        input(bucket_prompt).strip() or current_bucket,
        "B2 bucket name",
    )

    print("Verifying credentials, bucket access, Object Lock, and upload permission...")
    bucket, capabilities = validate_b2(key_id, application_key, bucket_name)

    updated = dict(current)
    updated["B2_KEY_ID"] = key_id
    updated["B2_APPLICATION_KEY"] = application_key
    updated["B2_BUCKET"] = bucket_name
    write_protected_env(
        B2_CONFIG_PATH,
        updated,
        [
            "B2_KEY_ID",
            "B2_APPLICATION_KEY",
            "B2_BUCKET",
            "B2_ENDPOINT",
            "B2_REGION",
        ],
    )
    restart_running_archivers()

    print(f"Saved verified destination: {bucket.name} (bucket ID {bucket.id_})")
    if "deleteFiles" in capabilities:
        print("Warning: this key can delete files. A write-only key is safer.")


def show_current() -> None:
    b2 = read_env(B2_CONFIG_PATH)
    cutoff = current_archive_after()
    print("\nCurrent settings")
    print(f"Ignore email dated on or before: {cutoff.isoformat()}")
    print(f"First included email date: {(cutoff + timedelta(days=1)).isoformat()}")
    print(f"B2 bucket: {b2.get('B2_BUCKET', 'not configured')}")
    print(f"B2 key ID: {b2.get('B2_KEY_ID', 'not configured')}")
    print(f"Configured Yahoo accounts: {len(account_units())}")


def main() -> int:
    if os.geteuid() != 0:
        print("Run this wizard with sudo.", file=sys.stderr)
        return 1

    print("Yahoo Mail Archiver - Global Settings")
    print("\n1. Change email date cutoff")
    print("2. Change Backblaze B2 destination")
    print("3. Show current settings")
    choice = input("\nChoose 1, 2, or 3: ").strip()

    try:
        if choice == "1":
            change_archive_date()
        elif choice == "2":
            change_b2_destination()
        elif choice == "3":
            show_current()
        else:
            print("No changes made.")
            return 1
    except KeyboardInterrupt:
        print("\nNo changes made.")
        return 1
    except Exception as error:
        print(f"Could not apply settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
