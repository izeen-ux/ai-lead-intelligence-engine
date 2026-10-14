"""Add, check and remove provider API keys - one command, no file editing.

  uv run python scripts/keys.py list                  # every key: accepted?, used, resets on
  uv run python scripts/keys.py add apify             # paste a new key (hidden), checked, saved
  uv run python scripts/keys.py add claude            # Claude replaces the current key
  uv run python scripts/keys.py remove firecrawl 2    # drop key number 2 of Firecrawl

Providers: apify, firecrawl, hunter, apollo (several keys each: the first is used, the next
ones are backups that take over automatically when a key is rejected or out of credits),
claude, openai (one key each).

The key is typed / pasted at a hidden prompt (right-click or Ctrl+V in PowerShell, then
Enter). It is never shown, never logged and never put on the command line. Before it is saved
it is checked with the provider's FREE account endpoint: a key the provider refuses is not
saved. Pasted extras are cleaned up (spaces, quotes, "Bearer ", "NAME=").

After a change the running app is restarted in the same mode (live stays live) so the new key
is used at once. Credits of every key come back by themselves when the provider's month
starts again - `list` shows the date.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
from dotenv import dotenv_values

from app.core.config import Settings
from app.core.keyring import split_keys
from app.services.credits import (
    APIFY_LIMITS_URL,
    FIRECRAWL_BASE_URL,
    KeyReading,
    apify_reading,
    firecrawl_reading,
    hunter_reading,
)

ENV = Path(".env")
# provider -> (settings field, several keys allowed)
PROVIDERS: dict[str, tuple[str, bool]] = {
    "apify": ("apify_token", True),
    "firecrawl": ("firecrawl_api_key", True),
    "hunter": ("hunter_api_key", True),
    "apollo": ("apollo_api_key", True),
    "claude": ("anthropic_api_key", False),
    "openai": ("openai_api_key", False),
}
ALIASES = {"anthropic": "claude"}
TIMEOUT = 20.0


# ------------------------------------------------------------------ cleaning + .env
def clean(raw: str) -> str:
    """What people paste: 'APIFY_TOKEN="apify_api_x" ', 'Bearer x', with line breaks."""
    text = raw.strip().strip("﻿")
    text = re.sub(r"^[A-Z0-9_]+\s*=\s*", "", text)  # NAME=value
    text = re.sub(r"^(?i:bearer)\s+", "", text)
    return text.strip().strip("'\"").strip()


def read_lines() -> list[str]:
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def write_lines(lines: list[str]) -> None:
    for _ in range(8):
        try:
            ENV.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            return
        except OSError:  # the file can be briefly locked on Windows
            time.sleep(1)
    raise SystemExit("could not write .env - close programs that have it open and try again")


def set_value(lines: list[str], name: str, value: str) -> list[str]:
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name:
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}={value}")
    return out


def keys_of(field: str) -> list[str]:
    """Keys as saved in .env (read from the file itself: a variable that happens to be set in
    this shell must not hide what is stored for the app)."""
    values = dotenv_values(ENV)
    return split_keys(values.get(field.upper()), values.get(f"{field.upper()}_BACKUPS"))


def save_keys(field: str, keys: list[str], several: bool) -> None:
    lines = read_lines()
    lines = set_value(lines, field.upper(), keys[0] if keys else "")
    if several:
        lines = set_value(lines, f"{field.upper()}_BACKUPS", ",".join(keys[1:]))
    write_lines(lines)


# ------------------------------------------------------------------ checking a key (free)
async def check(provider: str, key: str) -> tuple[str, KeyReading | None, str]:
    """('ok' | 'refused' | 'unknown', reading, message). Free account endpoints only."""
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT) as client:
            if provider == "apify":
                r = await client.get(APIFY_LIMITS_URL, headers={"Authorization": f"Bearer {key}"})
                return verdict(r, lambda: apify_reading(r.json()))
            if provider == "firecrawl":
                base = (Settings().firecrawl_base_url or FIRECRAWL_BASE_URL).rstrip("/")
                r = await client.get(
                    f"{base}/team/credit-usage", headers={"Authorization": f"Bearer {key}"}
                )
                return verdict(r, lambda: firecrawl_reading(r.json()))
            if provider == "hunter":
                r = await client.get("https://api.hunter.io/v2/account", headers={"X-API-KEY": key})
                return verdict(r, lambda: hunter_reading(r.json()))
            if provider == "apollo":
                r = await client.get(
                    "https://api.apollo.io/v1/auth/health", headers={"X-Api-Key": key}
                )
                if r.status_code in (401, 403):
                    return "refused", None, f"Apollo refused the key (HTTP {r.status_code})"
                logged_in = isinstance(r.json(), dict) and r.json().get("is_logged_in")
                return ("ok" if logged_in else "unknown"), None, "Apollo has no credit counter"
            if provider == "claude":
                r = await client.get(
                    "https://api.anthropic.com/v1/models/claude-opus-5-5",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                if r.status_code in (401, 403):
                    return (
                        "refused",
                        None,
                        "Anthropic refused the key (a Claude key starts sk-ant-)",
                    )
                return ("ok" if r.status_code == 200 else "unknown"), None, "prepaid credits"
            if provider == "openai":
                r = await client.get(
                    "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
                )
                if r.status_code in (401, 403):
                    return "refused", None, "OpenAI refused the key"
                return ("ok" if r.status_code == 200 else "unknown"), None, ""
    except (httpx2.HTTPError, ValueError) as exc:
        return "unknown", None, f"could not reach the provider ({type(exc).__name__})"
    return "unknown", None, ""


def verdict(r: httpx2.Response, reading: Any) -> tuple[str, KeyReading | None, str]:
    if r.status_code in (401, 403):
        return "refused", None, f"the provider refused this key (HTTP {r.status_code})"
    if r.status_code != 200:
        return "unknown", None, f"the provider answered HTTP {r.status_code}"
    return "ok", reading(), ""


def describe(reading: KeyReading | None) -> str:
    if reading is None:
        return ""
    used, limit = (
        f"{reading.used:.4f}".rstrip("0").rstrip("."),
        f"{reading.limit:.4f}".rstrip("0").rstrip("."),
    )
    left = f"used {used} of {limit}"
    return left + (f", credits come back on {reading.resets_on}" if reading.resets_on else "")


# ------------------------------------------------------------------ restart (same mode)
def restart() -> None:
    """Restart the containers so they read the new key - live stays live, safe stays safe."""
    if os.environ.get("KEYS_NO_RESTART"):
        return
    here = Path(__file__).resolve().parent
    status = subprocess.run(  # noqa: S603
        [sys.executable, str(here / "live_mode.py"), "status"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    mode = "on" if "LIVE mode" in status.stdout else None
    if "not running" in status.stdout:
        print("The app is not running - the key is used the next time you start it.")
        return
    print("Restarting the app so it uses the key ...")
    if mode:
        subprocess.run([sys.executable, str(here / "live_mode.py"), "on"], check=False)  # noqa: S603
    else:
        subprocess.run(["docker", "compose", "up", "-d"], check=False)  # noqa: S607


# ------------------------------------------------------------------ commands
def cmd_list() -> int:
    for provider, (field, several) in PROVIDERS.items():
        keys = keys_of(field)
        print(f"\n{provider.upper()}  ({len(keys)} key{'s' if len(keys) != 1 else ''})")
        if not keys:
            print("   no key - add one: uv run python scripts/keys.py add " + provider)
            continue
        results = asyncio.run(_check_all(provider, keys))
        for position, (state, reading, message) in enumerate(results, start=1):
            role = "in use first" if position == 1 else f"backup {position - 1}"
            text = {"ok": "accepted", "refused": "REFUSED", "unknown": "not checked"}[state]
            extra = describe(reading) or message
            print(f"   key {position} ({role}): {text}" + (f" - {extra}" if extra else ""))
        if not several and len(keys) > 1:  # pragma: no cover - single-key fields
            print("   only the first key is used")
    print()
    return 0


async def _check_all(provider: str, keys: list[str]) -> list[tuple[str, KeyReading | None, str]]:
    return list(await asyncio.gather(*(check(provider, k) for k in keys)))


def cmd_add(provider: str, force: bool) -> int:
    field, several = PROVIDERS[provider]
    print(f"Paste the {provider} key and press Enter (it stays hidden):")
    key = clean(getpass.getpass(prompt="> "))
    if len(key) < 12 or " " in key:
        print("That does not look like an API key (too short or contains spaces). Nothing saved.")
        return 2
    keys = keys_of(field)
    if key in keys:
        print(f"This key is already saved (key {keys.index(key) + 1}). Nothing changed.")
        return 0
    state, reading, message = asyncio.run(check(provider, key))
    if state == "refused" and not force:
        print(f"Not saved: {message}. Check that you copied the whole key.")
        return 1
    if several:
        keys = [*keys, key]
        save_keys(field, keys, several)
        where = "in use now" if len(keys) == 1 else f"backup {len(keys) - 1}"
        print(f"Saved as {provider} key {len(keys)} ({where}).")
    else:
        save_keys(field, [key], several)
        print(f"Saved: this is now the {provider} key (the old one was replaced).")
    detail = describe(reading) or message
    print(
        f"Check: {'accepted' if state == 'ok' else 'not checked'}"
        + (f" - {detail}" if detail else "")
    )
    restart()
    return 0


def cmd_remove(provider: str, position: int) -> int:
    field, several = PROVIDERS[provider]
    keys = keys_of(field)
    if not 1 <= position <= len(keys):
        print(f"{provider} has {len(keys)} key(s); there is no key {position}.")
        return 2
    keys.pop(position - 1)
    save_keys(field, keys, several)
    print(f"Removed {provider} key {position}. {len(keys)} key(s) left.")
    restart()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every key: accepted?, used, when credits come back")
    add = sub.add_parser("add", help="add a key (hidden paste, checked before it is saved)")
    add.add_argument("provider", choices=[*PROVIDERS, *ALIASES])
    add.add_argument("--force", action="store_true", help="save even if the provider refuses")
    rem = sub.add_parser("remove", help="remove one key by its number (see list)")
    rem.add_argument("provider", choices=[*PROVIDERS, *ALIASES])
    rem.add_argument("number", type=int)
    args = parser.parse_args(argv)
    if not ENV.exists():
        print("Run this in the project folder (where the .env file is).")
        return 2
    if args.command == "list":
        return cmd_list()
    provider = ALIASES.get(args.provider, args.provider)
    if args.command == "add":
        return cmd_add(provider, args.force)
    return cmd_remove(provider, args.number)


if __name__ == "__main__":
    sys.exit(main())
