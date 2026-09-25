"""Passwords and tokens, kept in the operating system's secure store.

Mac: Keychain. Windows: Credential Manager. Nothing is written to files or
environment variables, and input is hidden so it never lands in shell history.
"""
from __future__ import annotations

import getpass
import os

import keyring

SERVICE = "alora-evv"

KNOWN = {
    "portal_username": ("Mobile Caregiver+ username", False),
    "portal_password": ("Mobile Caregiver+ password", True),
    "axiscare_api_key": ("AxisCare API key (only when using the API backend)", True),
    "google_client_id": ("Google OAuth client ID (web app sign-in)", False),
    "google_client_secret": ("Google OAuth client secret (web app sign-in)", True),
    "web_session_secret": ("Random secret for web sessions (any long random string)", True),
}

# Yitzi's version stored these as Windows environment variables via setx.
LEGACY_ENV = {"portal_username": "EVV_USERNAME", "portal_password": "EVV_PASSWORD"}


class MissingCredential(RuntimeError):
    pass


def get(name: str, required: bool = True) -> str | None:
    """Environment variable ALORA_EVV_<NAME> first (how a server gets secrets from
    Secret Manager), then the OS secure store (desktop)."""
    value = os.environ.get("ALORA_EVV_" + name.upper())
    if not value:
        try:
            value = keyring.get_password(SERVICE, name)
        except Exception:  # no keyring backend on a bare server
            value = None
    if not value and required:
        label = KNOWN.get(name, (name,))[0]
        raise MissingCredential(f"{label} is not set. Run: alora-evv credentials set {name}")
    return value


def set_interactive(name: str) -> None:
    if name not in KNOWN:
        raise SystemExit(f"Unknown credential '{name}'. Known: {', '.join(KNOWN)}")
    label, secret = KNOWN[name]
    value = (getpass.getpass if secret else input)(f"{label}: ").strip()
    if not value:
        raise SystemExit("Nothing entered; not saved.")
    keyring.set_password(SERVICE, name, value)
    print(f"Saved {name} to the system's secure store.")


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
        print(f"Removed {name}.")
    except keyring.errors.PasswordDeleteError:
        print(f"{name} was not set.")


def import_legacy_env() -> None:
    moved = []
    for name, env in LEGACY_ENV.items():
        value = os.environ.get(env)
        if value:
            keyring.set_password(SERVICE, name, value)
            moved.append(env)
    if not moved:
        print("No EVV_USERNAME / EVV_PASSWORD environment variables found.")
        return
    print(f"Copied {', '.join(moved)} into the secure store.")
    if os.name == "nt":
        print("Now remove the old plain-text copies. In PowerShell run:")
        for env in moved:
            print(f'  [Environment]::SetEnvironmentVariable("{env}", $null, "User")')
        print("Then clear the saved PowerShell history line that contains the password:")
        print("  notepad (Get-PSReadLineOption).HistorySavePath")


def status() -> dict[str, bool]:
    return {name: bool(get(name, required=False)) for name in KNOWN}
