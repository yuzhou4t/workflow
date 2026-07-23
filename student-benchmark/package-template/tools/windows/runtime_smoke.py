from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import threading


class LedgerHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = b"ledger-marker\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def docker_version() -> str:
    completed = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip()


def docker_inspect(format_text: str, target: str) -> str:
    completed = subprocess.run(
        ["docker", "inspect", "--format", format_text, target],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip()


def host_source(path: Path) -> Path:
    controller_root = Path(os.environ["SIXBENCH_CONTROLLER_ROOT"]).resolve()
    host_root = Path(os.environ["SIXBENCH_DOCKER_HOST_ROOT"])
    relative = path.resolve().relative_to(controller_root)
    return host_root / relative


root = Path("/workspace").resolve()
return_dir = root / "RETURN"
return_dir.mkdir(parents=True, exist_ok=True)
report_path = return_dir / "WINDOWS_ENV_CHECK.json"
runtime_image = os.environ["SIXBENCH_RUNTIME_IMAGE"]
network = os.environ["SIXBENCH_CONTAINER_NETWORK"]
controller_alias = os.environ["SIXBENCH_CONTROLLER_ALIAS"]

report: dict[str, object] = {
    "schema_version": "sixbench-windows-environment-check-v1",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "status": "failed",
    "host_mode": "windows_wsl2_docker",
    "controller": {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "node": shutil.which("node"),
        "docker_server": docker_version(),
        "runtime_image": runtime_image,
        "runtime_image_id": docker_inspect("{{.Id}}", runtime_image),
        "internal_network": network,
        "internal_network_flag": docker_inspect("{{.Internal}}", network),
    },
    "checks": {},
}

try:
    with tempfile.TemporaryDirectory(
        prefix="environment-smoke-",
        dir=root / ".sixbench-windows",
    ) as temporary:
        smoke_root = Path(temporary)
        visible_dir = smoke_root / "visible"
        forbidden_dir = smoke_root / "forbidden"
        output_dir = smoke_root / "output"
        visible_dir.mkdir()
        forbidden_dir.mkdir()
        output_dir.mkdir()
        visible_file = visible_dir / "marker.txt"
        forbidden_file = forbidden_dir / "secret.txt"
        output_file = output_dir / "result.json"
        visible_file.write_text("visible-marker\n", encoding="utf-8")
        forbidden_file.write_text("secret-marker\n", encoding="utf-8")

        server = ThreadingHTTPServer(("0.0.0.0", 0), LedgerHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        ledger_url = f"http://{controller_alias}:{server.server_port}/health"
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=1",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m",
            "--mount",
            (
                f"type=bind,source={host_source(visible_dir)},"
                f"target=/sixbench/visible,readonly"
            ),
            "--mount",
            (
                f"type=bind,source={host_source(output_dir)},"
                f"target=/sixbench/output"
            ),
            "--env",
            "SMOKE_VISIBLE_FILE=/sixbench/visible/marker.txt",
            "--env",
            "SMOKE_FORBIDDEN_FILE=/sixbench/forbidden/secret.txt",
            "--env",
            "SMOKE_OUTPUT_FILE=/sixbench/output/result.json",
            "--env",
            f"SMOKE_LEDGER_URL={ledger_url}",
            runtime_image,
            "python3",
            "/opt/sixbench/runtime_smoke_worker.py",
        ]
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        worker_checks = (
            json.loads(output_file.read_text(encoding="utf-8"))
            if output_file.is_file()
            else {}
        )
        report["checks"] = {
            **worker_checks,
            "network_declared_internal": (
                report["controller"]["internal_network_flag"] == "true"
            ),
            "worker_exit_zero": completed.returncode == 0,
            "worker_stdout_json": bool(completed.stdout.strip()),
            "forbidden_marker_not_copied": "secret-marker" not in completed.stdout,
        }
        report["worker"] = {
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
        report["status"] = (
            "passed"
            if report["checks"] and all(report["checks"].values())
            else "failed"
        )
except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
    report["error"] = f"{type(exc).__name__}: {exc}"

report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(report, ensure_ascii=False, indent=2))
print(f"诊断文件：{report_path}")
raise SystemExit(0 if report["status"] == "passed" else 2)
