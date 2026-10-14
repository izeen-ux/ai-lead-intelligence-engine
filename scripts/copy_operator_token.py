"""Copy the dashboard operator token to the clipboard (it is never printed).

The dashboard at http://127.0.0.1:8000/dashboard asks for this token once per browser. Run
this, then paste (Ctrl+V) into the "Unlock the dashboard" box.

  uv run python scripts/copy_operator_token.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from app.core.config import Settings


def main() -> int:
    token = Settings().operator_api_token
    if token is None or not token.get_secret_value():
        print("OPERATOR_API_TOKEN is not set in .env - nothing to copy.")
        return 2
    tool = shutil.which("clip") or shutil.which("pbcopy") or shutil.which("xclip")
    if tool is None:
        print("No clipboard tool found. Open .env and copy the value after OPERATOR_API_TOKEN=")
        return 2
    args = [tool, "-selection", "clipboard"] if tool.endswith("xclip") else [tool]
    subprocess.run(args, input=token.get_secret_value().encode(), check=True)  # noqa: S603
    print("Operator token copied to the clipboard. Paste it into the dashboard (Ctrl+V).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
