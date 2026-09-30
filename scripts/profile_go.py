#!/usr/bin/env python3
"""Capture a local Go CPU profile and save only sanitized summary data."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark import _assert_allowlisted, summarize_oha
from scripts.collect_environment import collect_environment
from scripts.common import (
    ProcessResourceSampler,
    DEFAULT_CONFIG,
    free_port,
    request_json,
    start_stack,
    stop_stack,
    wait_idle,
    wait_ready,
)


def wait_pprof(url: str, timeout_seconds: float = 5.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError, urllib.error.URLError):
            time.sleep(0.05)
    raise TimeoutError("loopback pprof listener did not become ready")


def summarize_pprof(output: str, duration_seconds: int) -> dict:
    total = re.search(r"Total samples\s*=\s*([0-9.]+)s", output)
    functions = []
    row = re.compile(
        r"^\s*([0-9.]+)s\s+([0-9.]+)%\s+[0-9.]+%\s+([0-9.]+)s\s+([0-9.]+)%\s+(.+?)\s*$"
    )

    for line in output.splitlines():
        match = row.match(line)
        if not match:
            continue
        symbol = match.group(5)
        # Function symbols only; drop package paths and any unexpected text.
        if re.fullmatch(r"[A-Za-z0-9_.$*<>:+() -]+", symbol) is None:
            continue
        functions.append(
            {
                "symbol": symbol[:120],
                "flat_seconds": float(match.group(1)),
                "flat_percent": float(match.group(2)),
                "cumulative_seconds": float(match.group(3)),
                "cumulative_percent": float(match.group(4)),
            }
        )
        if len(functions) >= 10:
            break

    return {
        "profile_type": "cpu",
        "sample_duration_seconds": duration_seconds,
        "total_cpu_samples_seconds": float(total.group(1)) if total else 0.0,
        "top_functions": functions,
        "raw_profile_retained": False,
    }


def validate_profile_report(report: dict) -> None:
    schema = json.loads((ROOT / "benchmarks/profiling/profile-schema.json").read_text(encoding="utf-8"))
    if set(report) != set(schema["properties"]):
        raise ValueError("Go profile report fields do not match the schema")
    if report["stack"] != "go" or report["profiler"] != "go tool pprof":
        raise ValueError("Go profile report identity is invalid")
    profile_schema = schema["properties"]["profile_summary"]["properties"]
    if set(report["profile_summary"]) != set(profile_schema):
        raise ValueError("Go profile summary fields do not match the schema")
    if report["profile_summary"].get("raw_profile_retained") is not False:
        raise ValueError("raw CPU profiles must not be included in the report")
    if len(report["profile_summary"].get("top_functions", [])) > 10:
        raise ValueError("profile summary exceeds the symbol allowlist")
    for item in report["profile_summary"].get("top_functions", []):
        if set(item) != set(profile_schema["top_functions"]["items"]["properties"]):
            raise ValueError("profile function fields do not match the schema")
        if re.fullmatch(r"[A-Za-z0-9_.$*<>:+() -]{1,120}", item.get("symbol", "")) is None:
            raise ValueError("profile summary contains an unsafe symbol")
    _assert_allowlisted(report)


def main() -> int:
    profile_seconds = 3
    load_seconds = 5
    cpu_ms = 500
    port = free_port()
    pprof_port = free_port()
    config = {
        **DEFAULT_CONFIG,
        "enable_pprof": True,
        "pprof_port": pprof_port,
        "downstream_concurrency": 2,
    }
    run_id = uuid.uuid4().hex[:12]
    started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    environment = collect_environment()

    with tempfile.TemporaryDirectory(prefix="backpressure-go-profile-") as temporary:
        server = start_stack("go", port, Path(temporary) / "server.log", config)
        sampler = None
        load = None
        try:
            wait_ready(server)
            pprof_url = f"http://127.0.0.1:{pprof_port}/debug/pprof/profile?seconds={profile_seconds}"
            wait_pprof(f"http://127.0.0.1:{pprof_port}/debug/pprof/")
            pprof_environment = os.environ.copy()
            pprof_environment["HOME"] = temporary
            pprof_environment["TMPDIR"] = temporary
            # First use may initialize the pprof tool; warm it before the load window.
            subprocess.run(
                ["go", "tool", "pprof", "-h"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=pprof_environment,
            )
            sampler = ProcessResourceSampler(server.process.pid)
            sampler.start()
            load_command = [
                "oha", "--no-tui", "--no-color", "--output-format", "json",
                "-z", f"{load_seconds}s", "-w", "-c", "8",
                f"{server.base_url}/cpu?ms={cpu_ms}",
            ]
            load = subprocess.Popen(load_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pprof = subprocess.run(
                [
                    "go", "tool", "pprof", "-top", f"-seconds={profile_seconds}",
                    "-nodecount=10", pprof_url,
                ],
                capture_output=True,
                text=True,
                timeout=profile_seconds + 30,
                check=False,
                env=pprof_environment,
            )
            load_stdout, _load_stderr = load.communicate(timeout=load_seconds + 30)
            if load.returncode != 0 or pprof.returncode != 0:
                raise RuntimeError("Go CPU profiling or oha load failed; raw tool output was not saved")
            profile_summary = summarize_pprof(pprof.stdout, profile_seconds)
            if profile_summary["total_cpu_samples_seconds"] == 0:
                raise RuntimeError("Go pprof captured no CPU samples; increase load or profile duration")

            oha_result = summarize_oha(json.loads(load_stdout))
            metrics_status, app_metrics = request_json(f"{server.base_url}/metrics")
            if metrics_status != 200:
                raise RuntimeError("Go metrics endpoint failed during profiling")
            wait_idle(server, timeout_seconds=15)
            resource_summary = sampler.stop()
            sampler = None
            shutdown = stop_stack(server)
        finally:
            if sampler is not None:
                sampler.stop()
            if load is not None and load.poll() is None:
                load.terminate()
                try:
                    load.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    load.kill()
            if server.process.poll() is None:
                stop_stack(server)
            elif not server.log_file.closed:
                server.log_file.close()

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "git_commit": environment.get("git_commit"),
        "working_tree_clean": environment["working_tree_clean"],
        "stack": "go",
        "profiler": "go tool pprof",
        "environment": environment,
        "configuration": {
            "cpu_load_seconds": load_seconds,
            "profile_seconds": profile_seconds,
            "cpu_request_ms": cpu_ms,
            "concurrency": 8,
            "downstream_concurrency": config["downstream_concurrency"],
            "pprof_bind_scope": "loopback",
        },
        "load_result": oha_result,
        "application_metrics": app_metrics,
        "service_resources": resource_summary,
        "profile_summary": profile_summary,
        "graceful_shutdown": shutdown,
    }
    validate_profile_report(report)

    output_dir = ROOT / "benchmarks/profiling/results"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{(environment.get('git_commit') or 'nogit')[:8]}-go-pprof-{run_id}"
    json_path = output_dir / f"{filename}.json"
    markdown_path = output_dir / f"{filename}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    profile = report["profile_summary"]
    top = profile["top_functions"][:5]
    lines = [
        "# Go pprof CPU profile summary",
        "",
        f"- Run: `{run_id}`",
        f"- Commit: `{report['git_commit'] or 'unavailable'}`",
        f"- Source tree clean: `{report['working_tree_clean']}`",
        f"- Environment: {environment['os']['family']} {environment['os'].get('distribution_id') or ''} {environment['os']['architecture']}; {environment['hardware'].get('cpu_model') or 'CPU unknown'}",
        f"- Profile duration: {profile_seconds}s; CPU samples: {profile['total_cpu_samples_seconds']}s",
        f"- Load: {oha_result['requests_per_second']} requests/s; HTTP codes `{json.dumps(oha_result['status_codes'], sort_keys=True)}`",
        "",
        "## Top symbols (raw profile discarded)",
        "",
    ]
    lines.extend(
        f"- `{item['symbol']}` — flat {item['flat_percent']}%, cumulative {item['cumulative_percent']}%"
        for item in top
    )
    lines += ["", "The raw pprof file was created inside a temporary directory and discarded.", ""]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"sanitized Go pprof summary written to {markdown_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
