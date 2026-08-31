#!/usr/bin/env python3
"""Interactive onboarding for one Yahoo Mail archive account."""

from __future__ import annotations

import argparse
import getpass
import grp
import imaplib
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile


ACCOUNT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CONFIG_DIRECTORY = Path("/etc/yah-arch")
ACCOUNT_DIRECTORY = CONFIG_DIRECTORY / "accounts"
B2_CONFIG_PATH = CONFIG_DIRECTORY / "b2.env"
PUSHOVER_CONFIG_PATH = CONFIG_DIRECTORY / "pushover.env"
SERVICE_SOURCE = Path(__file__).resolve().parent / "deploy" / "yah-arch@.service"
SERVICE_DESTINATION = Path("/etc/systemd/system/yah-arch@.service")
SERVICE_GROUP = "yaharch"
YAHOO_APP_PASSWORD_HELP = "https://help.yahoo.com/kb/SLN15241.html"
IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
OPTIONAL_ACCOUNT_SETTINGS = (
    "POLL_SECONDS",
    "RETRY_SECONDS",
    "BACKFILL_BATCH_SIZE",
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively add or update a Yahoo archive account"
    )
    parser.add_argument(
        "--account",
        help=(
            "Yahoo account name or full address; a name such as hoffmas "
            "implies hoffmas@yahoo.com"
        ),
    )
    return parser.parse_args()


def account_id_from_local_part(local_part: str) -> str:
    account = re.sub(r"[^a-z0-9_-]+", "-", local_part.lower())
    account = re.sub(r"-+", "-", account).strip("-_")[:32]
    if not ACCOUNT_PATTERN.fullmatch(account):
        raise ValueError("Could not derive a safe account ID from that Yahoo address")
    return account


def resolve_identity(value: str) -> tuple[str, str]:
    value = value.strip().lower()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Enter a Yahoo account name or complete email address")

    if "@" in value:
        if value.count("@") != 1:
            raise ValueError("Enter a valid Yahoo email address")
        local_part, domain = value.split("@", 1)
        if not local_part or not domain or "." not in domain:
            raise ValueError("Enter a valid Yahoo email address")
        email = value
    else:
        local_part = value
        email = f"{value}@yahoo.com"

    return account_id_from_local_part(local_part), email


def ask_identity(provided: str | None) -> tuple[str, str]:
    if provided:
        return resolve_identity(provided)

    while True:
        value = input("Yahoo email address or account name: ")
        try:
            return resolve_identity(value)
        except ValueError as error:
            print(error)


def ask_app_password() -> str:
    while True:
        password = getpass.getpass("Yahoo app password (input is hidden): ")
        password = password.replace(" ", "").strip()
        if len(password) >= 8:
            return password
        print("The app password appears incomplete. Please try again.")


def read_account_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    settings: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        settings[name.strip()] = value.strip()
    return settings


def verify_yahoo_login(email: str, app_password: str) -> None:
    context = ssl.create_default_context()
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = imaplib.IMAP4_SSL(
            IMAP_HOST,
            IMAP_PORT,
            ssl_context=context,
            timeout=30,
        )
        client.login(email, app_password)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Run this wizard with sudo.")


def require_shared_configuration() -> None:
    missing = [
        path
        for path in (B2_CONFIG_PATH, PUSHOVER_CONFIG_PATH)
        if not path.is_file()
    ]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise RuntimeError(f"Complete shared setup first; missing: {names}")


def prepare_config_directory(group_id: int) -> None:
    CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    ACCOUNT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for path in (CONFIG_DIRECTORY, ACCOUNT_DIRECTORY):
        os.chown(path, 0, group_id)
        os.chmod(path, 0o750)


def write_account_config(
    account: str,
    email: str,
    app_password: str,
    group_id: int,
    existing_settings: dict[str, str],
) -> Path:
    destination = ACCOUNT_DIRECTORY / f"{account}.env"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{account}.", suffix=".tmp", dir=ACCOUNT_DIRECTORY
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
            os.fchown(file.fileno(), 0, group_id)
            os.fchmod(file.fileno(), 0o640)
            file.write(f"YAHOO_USERNAME={email}\n")
            file.write(f"YAHOO_APP_PASSWORD={app_password}\n")
            for name in OPTIONAL_ACCOUNT_SETTINGS:
                if existing_settings.get(name):
                    file.write(f"{name}={existing_settings[name]}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, destination)
        directory_descriptor = os.open(ACCOUNT_DIRECTORY, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return destination


def install_service_template() -> None:
    if not SERVICE_SOURCE.is_file():
        raise RuntimeError(f"Service template not found: {SERVICE_SOURCE}")

    temporary_destination = SERVICE_DESTINATION.with_suffix(".service.tmp")
    shutil.copyfile(SERVICE_SOURCE, temporary_destination)
    os.chown(temporary_destination, 0, 0)
    os.chmod(temporary_destination, 0o644)
    os.replace(temporary_destination, SERVICE_DESTINATION)
    subprocess.run(["systemctl", "daemon-reload"], check=True)


def start_account(account: str) -> None:
    unit = f"yah-arch@{account}.service"
    subprocess.run(["systemctl", "enable", unit], check=True)
    subprocess.run(["systemctl", "restart", unit], check=True)


def main() -> int:
    arguments = parse_arguments()
    require_root()
    require_shared_configuration()
    group_id = grp.getgrnam(SERVICE_GROUP).gr_gid

    print("Yahoo Mail Archiver - Account Onboarding")
    print()
    account, email = ask_identity(arguments.account)
    destination = ACCOUNT_DIRECTORY / f"{account}.env"
    existing_settings = read_account_config(destination)
    if existing_settings:
        email = existing_settings.get("YAHOO_USERNAME", email)
        print(f"Existing account found: {account} ({email})")
        print("A verified app password will refresh its credentials.")
    else:
        print(f"New account: {account} ({email})")

    print()
    print("Create the Yahoo app password:")
    print(f"1. Open {YAHOO_APP_PASSWORD_HELP}")
    print("2. Sign in and find External connections.")
    print("3. Choose Create app password.")
    print(f"4. Name it yah-arch-{account}, create it, and copy the password.")
    print("Use that app password below, not your normal Yahoo password.")
    print()

    while True:
        app_password = ask_app_password()
        print("Checking the Yahoo IMAP login...")
        try:
            verify_yahoo_login(email, app_password)
        except imaplib.IMAP4.error:
            print("Yahoo rejected that address or app password. No changes were made.")
            print("Try again, or press Ctrl+C to cancel.")
            continue
        except (OSError, ssl.SSLError) as error:
            raise RuntimeError(f"Could not reach Yahoo IMAP: {error}") from error
        print("Yahoo IMAP login verified.")
        break

    prepare_config_directory(group_id)
    destination = write_account_config(
        account,
        email,
        app_password,
        group_id,
        existing_settings,
    )
    install_service_template()
    action = "Refreshed" if existing_settings else "Saved"
    print(f"{action} protected configuration: {destination}")
    start_account(account)
    print(f"Archiving started: yah-arch@{account}.service")

    print()
    print("Run this wizard again to add another Yahoo account.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nOnboarding cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Onboarding failed: {error}", file=sys.stderr)
        raise SystemExit(1)
