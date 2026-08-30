#!/usr/bin/env python3
"""Interactive onboarding for one Yahoo Mail archive account."""

from __future__ import annotations

import argparse
import getpass
import grp
import os
from pathlib import Path
import re
import shutil
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


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively add or update a Yahoo archive account"
    )
    parser.add_argument(
        "--account",
        help="optional account ID to prefill, such as personal or business",
    )
    return parser.parse_args()


def ask_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(prompt + suffix).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please enter y or n.")


def ask_account_id(prefill: str | None) -> str:
    while True:
        prompt = "Account ID"
        if prefill:
            prompt += f" [{prefill}]"
        account = input(prompt + ": ").strip() or (prefill or "")
        if ACCOUNT_PATTERN.fullmatch(account):
            return account
        print(
            "Use 1-32 lowercase letters, numbers, underscores, or hyphens; "
            "start with a letter or number."
        )


def ask_email() -> str:
    while True:
        email = input("Yahoo email address: ").strip()
        if "@" in email and not any(character.isspace() for character in email):
            return email
        print("Enter the complete Yahoo email address.")


def ask_app_password() -> str:
    while True:
        password = getpass.getpass("Yahoo app password (input is hidden): ")
        password = password.replace(" ", "").strip()
        if len(password) >= 8:
            return password
        print("The app password appears incomplete. Please try again.")


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
    print("Create a Yahoo app password before continuing:")
    print(YAHOO_APP_PASSWORD_HELP)
    print("Do not use your normal Yahoo password.")
    print()

    account = ask_account_id(arguments.account)
    destination = ACCOUNT_DIRECTORY / f"{account}.env"
    if destination.exists() and not ask_yes_no(
        f"Account '{account}' already exists. Replace its Yahoo credentials?",
        default=False,
    ):
        print("No changes made.")
        return 0

    email = ask_email()
    app_password = ask_app_password()

    print()
    print(f"Account ID: {account}")
    print(f"Yahoo address: {email}")
    print("App password: hidden")
    if not ask_yes_no("Save this account?", default=True):
        print("No changes made.")
        return 0

    prepare_config_directory(group_id)
    destination = write_account_config(account, email, app_password, group_id)
    install_service_template()
    print(f"Saved protected configuration: {destination}")

    if ask_yes_no("Start or restart continuous archiving now?", default=True):
        start_account(account)
        print(f"Archiving started: yah-arch@{account}.service")
    else:
        print(
            "Configuration saved. Start later with: "
            f"sudo systemctl enable --now yah-arch@{account}.service"
        )

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
