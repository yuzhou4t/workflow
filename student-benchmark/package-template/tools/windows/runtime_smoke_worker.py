from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request


def denied_read(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return True
    return False


def denied_write(path: Path) -> bool:
    try:
        path.write_text("must-not-write\n", encoding="utf-8")
    except (PermissionError, OSError):
        return True
    return False


def external_network_denied() -> bool:
    try:
        urllib.request.urlopen("https://example.com/", timeout=4)
    except Exception:
        return True
    return False


visible = Path(os.environ["SMOKE_VISIBLE_FILE"])
forbidden = Path(os.environ["SMOKE_FORBIDDEN_FILE"])
output = Path(os.environ["SMOKE_OUTPUT_FILE"])
ledger_url = os.environ["SMOKE_LEDGER_URL"]

with urllib.request.urlopen(ledger_url, timeout=4) as response:
    ledger_body = response.read().decode("utf-8")

checks = {
    "visible_read": visible.read_text(encoding="utf-8") == "visible-marker\n",
    "forbidden_read_denied": denied_read(forbidden),
    "output_write": False,
    "root_write_denied": denied_write(Path("/sixbench-must-not-write")),
    "external_network_denied": external_network_denied(),
    "ledger_only_network_reachable": ledger_body == "ledger-marker\n",
    "docker_socket_absent": not Path("/var/run/docker.sock").exists(),
}
output.write_text(json.dumps(checks, sort_keys=True) + "\n", encoding="utf-8")
checks["output_write"] = output.is_file()
output.write_text(json.dumps(checks, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(checks, sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 2)
