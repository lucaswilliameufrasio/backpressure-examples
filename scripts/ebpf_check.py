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


def run_probe(bpftrace: str | None, enabled: bool) -> dict:
    if not enabled:
        return {"requested": False, "status": "not-requested"}
    if platform.system().lower() != "linux":
        return {"requested": True, "status": "unsupported-os"}
    if not bpftrace:
        return {"requested": True, "status": "tool-not-installed"}

    try:
        result = subprocess.run(
            [bpftrace, "-q", "-e", "BEGIN { exit(); }"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"requested": True, "status": "timed-out"}
    except OSError:
        return {"requested": True, "status": "could-not-start"}

    if result.returncode == 0:
        status = "passed"
    else:
        detail = f"{result.stderr} {result.stdout}".lower()
        status = "permission-denied" if any(
            marker in detail for marker in ("operation not permitted", "permission denied", "not authorized")
        ) else "failed"
    # stderr/stdout are intentionally discarded; only a bounded status is recorded.
    return {"requested": True, "status": status}


def create_report(probe_requested: bool = False) -> dict:
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
        "probe": run_probe(shutil.which("bpftrace"), probe_requested),
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
    if set(report["probe"]) != set(schema["properties"]["probe"]["properties"]):
        raise ValueError("eBPF probe fields do not match the schema")
    if report["probe"]["status"] not in schema["properties"]["probe"]["properties"]["status"]["enum"]:
        raise ValueError("eBPF probe status is not allowlisted")
    _assert_allowlisted(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-probe", action="store_true", help="attempt a tiny eBPF program without sudo")
    parser.add_argument("--output-dir", default="benchmarks/profiling/results")
    args = parser.parse_args()

    report = create_report(args.run_probe)
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
