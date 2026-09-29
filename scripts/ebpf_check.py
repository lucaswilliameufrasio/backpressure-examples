#!/usr/bin/env python3
"""Record allowlisted eBPF tool/kernel readiness without storing raw output."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark import _assert_allowlisted
from scripts.collect_environment import collect_environment


def run_probe(bpftrace: str | None, enabled: bool, privileged: bool = False) -> dict:
    if not enabled:
        return {"requested": False, "privileged": False, "status": "not-requested"}
    if platform.system().lower() != "linux":
        return {"requested": True, "privileged": privileged, "status": "unsupported-os"}
    if not bpftrace:
        return {"requested": True, "privileged": privileged, "status": "tool-not-installed"}

    command = [bpftrace, "-q", "-e", "BEGIN { exit(); }"]
    if privileged:
        command = ["sudo", "-n", *command]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"requested": True, "privileged": privileged, "status": "timed-out"}
    except OSError:
        return {"requested": True, "privileged": privileged, "status": "could-not-start"}

    status = classify_probe(result.returncode, result.stdout, result.stderr)
    # stderr/stdout are intentionally discarded; only a bounded status is recorded.
    return {"requested": True, "privileged": privileged, "status": status}


def classify_probe(returncode: int, stdout: str, stderr: str) -> str:
    if returncode == 0:
        return "passed"
    detail = f"{stderr} {stdout}".lower()
    permission_markers = (
        "operation not permitted",
        "permission denied",
        "not authorized",
        "a password is required",
        "a terminal is required",
        "missing cap_",
        "run bpftrace as the root user",
    )
    return "permission-denied" if any(marker in detail for marker in permission_markers) else "failed"


def create_report(probe_requested: bool = False, privileged: bool = False) -> dict:
    environment = collect_environment()
    bpftrace_version = environment["tools"].get("bpftrace")
    btf_available = Path("/sys/kernel/btf/vmlinux").is_file() if platform.system() == "Linux" else False
    report = {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex[:12],
        "checked_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "git_commit": environment.get("git_commit"),
        "working_tree_clean": environment["working_tree_clean"],
        "environment": environment,
        "readiness": {
            "linux": platform.system().lower() == "linux",
            "kernel_btf_available": btf_available,
            "bpftrace_available": bpftrace_version is not None,
            "perf_available": "perf" in environment["tools"],
        },
        "probe": run_probe(shutil.which("bpftrace"), probe_requested, privileged),
    }
    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    schema = json.loads((ROOT / "benchmarks/profiling/schema.json").read_text(encoding="utf-8"))
    allowed_top = set(schema["properties"])
    if set(report) != allowed_top:
        raise ValueError("eBPF check report fields do not match the schema")
    if report["schema_version"] != schema["properties"]["schema_version"]["const"]:
        raise ValueError("unsupported eBPF report schema version")
    if set(report["readiness"]) != set(schema["properties"]["readiness"]["properties"]):
        raise ValueError("eBPF readiness fields do not match the schema")
    probe_fields = set(schema["properties"]["probe"]["properties"])
    required_probe_fields = set(schema["properties"]["probe"]["required"])
    if not required_probe_fields.issubset(report["probe"]) or set(report["probe"]).difference(probe_fields):
        raise ValueError("eBPF probe fields do not match the schema")
    if "privileged" in report["probe"] and not isinstance(report["probe"]["privileged"], bool):
        raise ValueError("probe privileged marker must be boolean")
    if report["probe"]["status"] not in schema["properties"]["probe"]["properties"]["status"]["enum"]:
        raise ValueError("eBPF probe status is not allowlisted")
    _assert_allowlisted(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-probe", action="store_true", help="attempt a tiny eBPF program without sudo")
    parser.add_argument("--sudo-probe", action="store_true", help="use sudo -n after `sudo -v`")
    parser.add_argument("--output-dir", default="benchmarks/profiling/results")
    args = parser.parse_args()

    if args.sudo_probe and not args.run_probe:
        parser.error("--sudo-probe requires --run-probe")
    report = create_report(args.run_probe or args.sudo_probe, privileged=args.sudo_probe)
    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{report['checked_at'][:19].replace(':', '').replace('-', '').replace('T', 'T')}-{report['run_id']}"
    json_path = output_dir / f"{filename}.json"
    markdown_path = output_dir / f"{filename}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    readiness = report["readiness"]
    probe = report["probe"]
    summary = [
        "# eBPF readiness check",
        "",
        f"- Checked: {report['checked_at']}",
        f"- Commit: `{report['git_commit'] or 'unavailable'}`",
        f"- Source tree clean: `{report['working_tree_clean']}`",
        f"- Linux: `{readiness['linux']}`",
        f"- Kernel BTF: `{readiness['kernel_btf_available']}`",
        f"- bpftrace available: `{readiness['bpftrace_available']}`",
        f"- perf available: `{readiness['perf_available']}`",
        f"- Probe status: `{probe['status']}`",
        f"- Privileged probe requested: `{probe['privileged']}`",
        "",
        "No hostname, username, PID, executable path, command output, capabilities dump, or raw profile is recorded.",
        "",
    ]
    markdown_path.write_text("\n".join(summary), encoding="utf-8")
    print(f"eBPF readiness report written to {markdown_path.relative_to(ROOT)}")
    print(json.dumps({"readiness": readiness, "probe": probe}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
