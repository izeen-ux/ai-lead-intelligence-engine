"""Switch the running app between SAFE mode and LIVE mode (one command, nothing to remember).

  uv run python scripts/live_mode.py on       # dashboard Run button works, credits can be spent
  uv run python scripts/live_mode.py off      # safe mode: nothing can spend credits
  uv run python scripts/live_mode.py status

LIVE = real searches, real provider credits (inside the per-run budgets) and real Google Sheet
writes. The switch applies to the app containers only; ``.env`` is not changed, so a plain
``docker compose up -d`` (or a reboot) always comes back in SAFE mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

SWITCHES = ("PROVIDER_MODE", "ALLOW_PAID_PROVIDER_CALLS", "ALLOW_SHEETS_WRITES")
LIVE = {"PROVIDER_MODE": "live", "ALLOW_PAID_PROVIDER_CALLS": "true", "ALLOW_SHEETS_WRITES": "true"}


def docker() -> str:
    path = shutil.which("docker")
    if path is None:
        raise SystemExit("Docker is not installed or not running - open Docker Desktop first.")
    return path


def current() -> dict[str, str]:
    done = subprocess.run(  # noqa: S603
        [docker(), "compose", "exec", "-T", "app", "printenv", *SWITCHES],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    values = done.stdout.split()
    return dict(zip(SWITCHES, values, strict=False)) if len(values) == len(SWITCHES) else {}


def describe(state: dict[str, str]) -> str:
    if not state:
        return "the app is not running (start it with: docker compose up -d)"
    live = state == LIVE
    return (
        "LIVE mode - the dashboard Run button works and can spend provider credits"
        if live
        else "SAFE mode - nothing can spend credits; the dashboard Run button is switched off"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    action = args[0].lower() if args else "status"
    if action not in ("on", "off", "status"):
        print(__doc__)
        return 2
    if action != "status":
        env = {k: v for k, v in os.environ.items() if k not in SWITCHES}
        if action == "on":
            env.update(LIVE)
        done = subprocess.run([docker(), "compose", "up", "-d"], env=env, check=False)  # noqa: S603
        if done.returncode != 0:
            print("docker compose failed - is Docker Desktop running?")
            return 1
    print(describe(current()))
    if action == "on":
        print("Refresh the dashboard. Switch back with: uv run python scripts/live_mode.py off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
