#!/usr/bin/env python3
"""Run repeatable oha scenarios and write an allowlisted, sanitized report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_environment import collect_environment
from scripts.common import (
    STACKS,
    ProcessResourceSampler,
    free_port,
    request_json,
    start_stack,
    stop_stack,
    wait_idle,
    wait_ready,
)

SCENARIOS = (
    "queue-saturation",
    "constant-rate",
    "burst",
    "rate-limit",
    "slow-downstream",
    "cpu",
    "streaming",
    "retry-storm",
    "tenant-fairness",
    "shutdown-drain",
)
METRIC_KEYS = {
    "queue_depth",
    "queue_capacity",
    "workers",
    "jobs_in_flight",
    "in_flight",
    "max_outstanding",
    "downstream_in_use",
    "downstream_concurrency",
    "downstream_queue_depth",
    "sync_requests_in_flight",
    "jobs_enqueued_total",
    "requests_rejected_total",
    "jobs_rejected_total",
    "jobs_processed_total",
    "jobs_failed_total",
    "jobs_retries_total",
    "retries_total",
    "tenant_rejected_total",
    "tenant_outstanding_limit",
    "rate_limited_total",
    "processing_duration_ms_total",
}
FORBIDDEN_KEYS = {
    "hostname",
    "username",
    "user",
    "pid",
    "ip",
    "ip_address",
    "mac",
    "mac_address",
    "serial",
    "serial_number",
    "environment_variables",
    "argv",
    "command_line",
}


def _percentiles(payload: dict) -> dict[str, float | None]:
    values = payload.get("latencyPercentiles", {})
    return {
        key: round(values.get(source, 0.0) * 1000, 3)
        for key, source in (("p50", "p50"), ("p95", "p95"), ("p99", "p99"))
    }


def summarize_oha(payload: dict) -> dict:
    summary = payload.get("summary", {})
    statuses = {str(key): int(value) for key, value in payload.get("statusCodeDistribution", {}).items()}
    errors = sum(int(value) for value in payload.get("errorDistribution", {}).values())
    total = sum(statuses.values()) + errors
    successful = sum(count for code, count in statuses.items() if code.startswith("2"))
    return {
        "requests": total,
        "status_codes": statuses,
        "transport_errors": errors,
        "http_2xx_rate": round(successful / total, 6) if total else 0.0,
        "oha_success_rate": round(float(summary.get("successRate", 0.0)), 6),
        "requests_per_second": round(float(summary.get("requestsPerSec", 0.0)), 3),
        "duration_seconds": round(float(summary.get("total", 0.0)), 6),
        "latency_ms": _percentiles(payload),
        "bytes_per_second": round(float(summary.get("sizePerSec", 0.0)), 2),
    }


def validate_report(report: dict) -> None:
    schema = json.loads((ROOT / "benchmarks/schema.json").read_text(encoding="utf-8"))
    _validate_json_schema(report, schema)
    required = set(schema["required"])
    missing = required.difference(report)
    if missing:
        raise ValueError(f"benchmark report missing fields: {sorted(missing)}")
    if set(report) != set(schema["properties"]):
        raise ValueError("benchmark report fields do not match benchmarks/schema.json")
    if report["schema_version"] != 1 or report["stack"] not in STACKS or report["scenario"] not in SCENARIOS:
        raise ValueError("benchmark report has an unsupported schema, stack, or scenario")
    try:
        dt.datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("started_at must be an ISO-8601 timestamp") from error
    if not isinstance(report["run_id"], str) or len(report["run_id"]) != 12:
        raise ValueError("run_id must be a 12-character identifier")
    if not isinstance(report["repetitions"], list) or not report["repetitions"]:
        raise ValueError("benchmark report must contain at least one repetition")

    environment = report["environment"]
    environment_properties = schema["properties"]["environment"]["properties"]
    if set(environment) != set(environment_properties):
        raise ValueError("environment metadata does not match the safe allowlist")
    if not isinstance(environment["working_tree_clean"], bool):
        raise ValueError("working_tree_clean must be a boolean")
    for key in ("os", "hardware", "container_limits"):
        if set(environment[key]) != set(environment_properties[key]["properties"]):
            raise ValueError(f"environment.{key} does not match the safe allowlist")
    allowed_tools = {
        "go", "rustc", "cargo", "node", "pnpm", "elixir", "erlang_otp", "mix",
        "oha", "golangci_lint", "mise", "bpftrace", "perf",
    }
    if set(environment["tools"]).difference(allowed_tools):
        raise ValueError("environment.tools contains an unapproved tool field")
    if any(not isinstance(version, str) for version in environment["tools"].values()):
        raise ValueError("tool versions must be strings")

    config_keys = {
        "queue_capacity", "workers", "downstream_concurrency", "process_delay_ms",
        "job_timeout_ms", "tenant_outstanding_limit", "max_retries", "retry_base_ms",
        "repeats", "scenario_parameters",
    }
    if set(report["configuration"]) != config_keys:
        raise ValueError("benchmark configuration does not match the safe allowlist")
    allowed_parameters = {
        "requests", "concurrency", "duration_seconds", "rate_per_second", "burst_rate",
        "cpu_ms", "stream_items", "stream_delay_ms", "failures_injected", "tenant_probe_tenant",
    }
    if set(report["configuration"]["scenario_parameters"]).difference(allowed_parameters):
        raise ValueError("scenario parameters contain an unapproved field")
    if set(report["aggregate"]) != {"requests_per_second_median", "latency_ms_median"}:
        raise ValueError("aggregate metrics do not match the schema")
    if set(report["aggregate"]["latency_ms_median"]) != {"p50", "p95", "p99"}:
        raise ValueError("aggregate latency percentiles do not match the schema")

    if set(report["load_generator"]) != {"name", "version"} or report["load_generator"]["name"] != "oha":
        raise ValueError("load generator metadata is not allowlisted")
    for repetition in report["repetitions"]:
        required_repetition = {
            "index", "parameters", "http", "service_resources", "application_metrics",
            "tenant_probe", "graceful_shutdown",
        }
        if set(repetition) != required_repetition:
            raise ValueError("repetition fields do not match the benchmark schema")
        if set(repetition["parameters"]).difference(allowed_parameters):
            raise ValueError("repetition parameters contain an unapproved field")
        if set(repetition["http"]) != {
            "requests", "status_codes", "transport_errors", "http_2xx_rate", "oha_success_rate",
            "requests_per_second", "duration_seconds", "latency_ms", "bytes_per_second",
        }:
            raise ValueError("HTTP summary contains an unapproved field")
        if set(repetition["http"]["latency_ms"]) != {"p50", "p95", "p99"}:
            raise ValueError("HTTP latency summary has unexpected percentiles")
        if any(not str(code).isdigit() for code in repetition["http"]["status_codes"]):
            raise ValueError("HTTP status codes must be numeric")
        if set(repetition["service_resources"]) != {
            "sampled", "average_cpu_cores", "peak_rss_mib", "sample_interval_ms"
        }:
            raise ValueError("service resource summary contains an unapproved field")
        if set(repetition["application_metrics"]) != {"before", "after_load", "after_drain"}:
            raise ValueError("application metric snapshots do not match the schema")
        for snapshot in repetition["application_metrics"].values():
            if snapshot is not None and set(snapshot).difference(METRIC_KEYS):
                raise ValueError("application metrics contain an unapproved field")
        if repetition["tenant_probe"] is not None and set(repetition["tenant_probe"]) != {
            "http_status", "error_code"
        }:
            raise ValueError("tenant probe contains an unapproved field")
        if repetition["tenant_probe"] is not None and repetition["tenant_probe"]["error_code"] not in {
            None, "TENANT_BUSY", "QUEUE_FULL"
        }:
            raise ValueError("tenant probe error code is not allowlisted")
        if repetition["graceful_shutdown"] is not None and set(repetition["graceful_shutdown"]) != {
            "duration_seconds", "exit_code", "timed_out"
        }:
            raise ValueError("shutdown result contains an unapproved field")
    _assert_allowlisted(report)


def _validate_json_schema(value, schema: dict, path: str = "$") -> None:
    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]

        def matches(kind: str) -> bool:
            if kind == "object":
                return isinstance(value, dict)
            if kind == "array":
                return isinstance(value, list)
            if kind == "string":
                return isinstance(value, str)
            if kind == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            if kind == "number":
                return isinstance(value, (int, float)) and not isinstance(value, bool)
            if kind == "boolean":
                return isinstance(value, bool)
            if kind == "null":
                return value is None
            return False

        if not any(matches(kind) for kind in expected_types):
            raise ValueError(f"{path} does not match JSON schema type {expected}")

    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not match its schema constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed schema value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below its schema minimum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} is shorter than its schema minimum")
        if len(value) > schema.get("maxLength", float("inf")):
            raise ValueError(f"{path} is longer than its schema maximum")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match its schema pattern")
        if schema.get("format") == "date-time":
            try:
                dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError(f"{path} is not an ISO-8601 date-time") from error

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path} has fewer items than its schema minimum")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, dict):
        missing = set(schema.get("required", ())).difference(value)
        if missing:
            raise ValueError(f"{path} is missing schema fields: {sorted(missing)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_json_schema(item, properties[key], f"{path}.{key}")
            elif additional is False:
                raise ValueError(f"{path}.{key} is not an allowed schema field")
            elif isinstance(additional, dict):
                _validate_json_schema(item, additional, f"{path}.{key}")


def _assert_allowlisted(value, path: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError(f"sensitive field is not allowed in benchmark report: {path}.{key}")
            _assert_allowlisted(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_allowlisted(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if "/home/" in lowered or "c:\\users\\" in lowered:
            raise ValueError(f"personal filesystem path found in benchmark report: {path}")


def _metrics(server) -> dict:
    status, payload = request_json(f"{server.base_url}/metrics")
    if status != 200:
        raise RuntimeError(f"{server.name} metrics endpoint returned HTTP {status}")
    return {key: value for key, value in payload.items() if key in METRIC_KEYS}


def _oha_args(scenario: str, server, args) -> tuple[list[str], dict]:
    base = ["oha", "--no-tui", "--no-color", "--output-format", "json"]
    params = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "duration_seconds": args.duration,
        "rate_per_second": args.rate,
    }
    url = server.base_url

    if scenario in {"queue-saturation", "retry-storm", "tenant-fairness", "shutdown-drain"}:
        query = ""
        if scenario == "retry-storm":
            query = f"?failures={args.failures}"
        elif scenario == "tenant-fairness":
            query = "?tenant=alpha"
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), "-m", "POST", f"{url}/jobs{query}"], params

    if scenario == "constant-rate":
        return base + ["-z", f"{args.duration}s", "-w", "-c", str(args.concurrency), "-q", str(args.rate), "-m", "POST", f"{url}/jobs"], params

    if scenario == "burst":
        params["burst_rate"] = args.burst_rate
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), "--burst-delay", "1s", "--burst-rate", str(args.burst_rate), "-m", "POST", f"{url}/jobs"], params

    if scenario == "rate-limit":
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), f"{url}/limited"], params

    if scenario == "slow-downstream":
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), f"{url}/sync"], params

    if scenario == "cpu":
        params["cpu_ms"] = args.cpu_ms
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), f"{url}/cpu?ms={args.cpu_ms}"], params

    if scenario == "streaming":
        params.update({"stream_items": args.stream_items, "stream_delay_ms": args.stream_delay_ms})
        query = f"items={args.stream_items}&delay_ms={args.stream_delay_ms}"
        return base + ["-n", str(args.requests), "-c", str(args.concurrency), f"{url}/stream?{query}"], params

    raise ValueError(f"unsupported scenario: {scenario}")


def _run_oha(command: list[str], timeout_seconds: float) -> dict:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("oha exceeded its timeout") from error
    if completed.returncode != 0:
        # Deliberately do not persist oha stderr; it can include request details.
        raise RuntimeError(f"oha exited with status {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("oha did not return valid JSON output") from error


def _probe_tenant(server) -> dict:
    status, payload = request_json(f"{server.base_url}/jobs?tenant=beta", method="POST")
    return {"http_status": status, "error_code": payload.get("error_code")}


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _write_markdown(path: Path, report: dict) -> None:
    env = report["environment"]
    hardware = env["hardware"]
    os_info = env["os"]
    lines = [
        f"# Benchmark: {report['stack']} / {report['scenario']}",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Started: {report['started_at']}",
        f"- Commit: `{report['git_commit'] or 'unavailable'}`",
        f"- Source tree: `{'clean' if env['working_tree_clean'] else 'dirty'}`",
        f"- Environment: {os_info['family']} {os_info.get('distribution_id') or ''} {os_info.get('distribution_version') or ''}; kernel {os_info.get('kernel_version') or 'unknown'}; {os_info['architecture']}",
        f"- Hardware: {hardware.get('cpu_model') or 'unknown'}; {hardware.get('logical_cores') or 'unknown'} logical cores; {hardware.get('memory_gib_rounded') or 'unknown'} GiB RAM (rounded)",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(report["configuration"], indent=2, sort_keys=True),
        "```",
        "",
        "## Repetitions",
        "",
        "| Run | RPS | p50 ms | p95 ms | p99 ms | CPU cores | Peak RSS MiB | HTTP statuses |",
        "|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for result in report["repetitions"]:
        http = result["http"]
        lines.append(
            f"| {result['index']} | {http['requests_per_second']} | {http['latency_ms']['p50']} | "
            f"{http['latency_ms']['p95']} | {http['latency_ms']['p99']} | "
            f"{result['service_resources']['average_cpu_cores']} | "
            f"{result['service_resources']['peak_rss_mib']} | "
            f"`{json.dumps(http['status_codes'], sort_keys=True)}` |"
        )
    lines += [
        "",
        "## Aggregate medians",
        "",
        f"- Requests/sec: {report['aggregate']['requests_per_second_median']}",
        f"- p50/p95/p99 latency (ms): {report['aggregate']['latency_ms_median']['p50']} / "
        f"{report['aggregate']['latency_ms_median']['p95']} / {report['aggregate']['latency_ms_median']['p99']}",
        "",
        "This is a local observation, not a universal language benchmark. The environment and exact workload are part of the result.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_stack(stack: str, scenario: str, args) -> Path:
    env_overrides = {
        "queue_capacity": args.queue_capacity,
        "workers": args.workers,
        "downstream_concurrency": args.downstream_concurrency,
        "process_delay_ms": args.process_delay_ms,
        "job_timeout_ms": args.job_timeout_ms,
        "tenant_outstanding_limit": args.tenant_limit if scenario == "tenant-fairness" else 0,
        "max_retries": args.max_retries,
        "retry_base_ms": args.retry_base_ms,
    }
    if scenario == "slow-downstream":
        env_overrides["downstream_concurrency"] = 1
    if scenario == "tenant-fairness":
        env_overrides["tenant_outstanding_limit"] = args.tenant_limit

    started_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    run_id = uuid.uuid4().hex[:12]
    environment = collect_environment()
    version = environment["tools"].get("oha")
    if not version:
        raise RuntimeError("oha is required; install it with your package manager before benchmarking")

    repetitions = []
    for index in range(1, args.repeats + 1):
        port = free_port()
        with tempfile.TemporaryDirectory(prefix=f"bp-{stack}-") as temporary:
            server = start_stack(stack, port, Path(temporary) / "server.log", env_overrides)
            try:
                wait_ready(server)
                # Warm the HTTP stack without warming or filling the workload queue.
                warmup = ["oha", "--no-tui", "--output-format", "quiet", "-n", "5", "-c", "2", f"{server.base_url}/healthz"]
                subprocess.run(warmup, capture_output=True, text=True, timeout=15, check=False)
                before = _metrics(server)
                sampler = ProcessResourceSampler(server.process.pid)
                sampler.start()
                command, parameters = _oha_args(scenario, server, args)
                timeout_seconds = max(60, args.duration + 30, args.requests * args.process_delay_ms / 1000 + 30)
                oha_output = _run_oha(command, timeout_seconds)
                after_load = _metrics(server)
                probe = _probe_tenant(server) if scenario == "tenant-fairness" else None

                shutdown = None
                if scenario == "shutdown-drain":
                    service_resources = sampler.stop()
                    shutdown = stop_stack(server, timeout_seconds=15)
                else:
                    after_drain = wait_idle(server, timeout_seconds=max(30, args.requests * args.process_delay_ms / 1000 + 15))
                    service_resources = sampler.stop()

                repetitions.append(
                    {
                        "index": index,
                        "parameters": parameters,
                        "http": summarize_oha(oha_output),
                        "service_resources": service_resources,
                        "application_metrics": {
                            "before": before,
                            "after_load": after_load,
                            "after_drain": after_drain if scenario != "shutdown-drain" else None,
                        },
                        "tenant_probe": probe,
                        "graceful_shutdown": shutdown,
                    }
                )
            finally:
                if server.process.poll() is None:
                    stop_stack(server)
                elif not server.log_file.closed:
                    server.log_file.close()

    rps = [entry["http"]["requests_per_second"] for entry in repetitions]
    latency = {
        name: _median([entry["http"]["latency_ms"][name] for entry in repetitions])
        for name in ("p50", "p95", "p99")
    }
    git_commit = environment.get("git_commit")
    configuration = {
        **env_overrides,
        "repeats": args.repeats,
        "scenario_parameters": {
            "requests": args.requests,
            "concurrency": args.concurrency,
            "duration_seconds": args.duration,
            "rate_per_second": args.rate,
            "burst_rate": args.burst_rate,
            "cpu_ms": args.cpu_ms,
            "stream_items": args.stream_items,
            "stream_delay_ms": args.stream_delay_ms,
            "failures_injected": args.failures,
            "tenant_probe_tenant": "beta" if scenario == "tenant-fairness" else None,
        },
    }
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "git_commit": git_commit,
        "stack": stack,
        "scenario": scenario,
        "configuration": configuration,
        "environment": environment,
        "load_generator": {"name": "oha", "version": version},
        "repetitions": repetitions,
        "aggregate": {
            "requests_per_second_median": _median(rps),
            "latency_ms_median": latency,
        },
    }
    validate_report(report)

    output_dir = (ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    commit_fragment = (git_commit or "nogit")[:8]
    filename = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{commit_fragment}-{stack}-{scenario}-{run_id}"
    json_path = output_dir / f"{filename}.json"
    markdown_path = output_dir / f"{filename}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_markdown(markdown_path, report)
    return markdown_path


def _metrics(server) -> dict:
    status, payload = request_json(f"{server.base_url}/metrics")
    if status != 200:
        raise RuntimeError(f"{server.name} metrics endpoint returned HTTP {status}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=(*STACKS, "all"), default="go")
    parser.add_argument("--scenario", choices=SCENARIOS, default="queue-saturation")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=80)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--rate", type=int, default=50)
    parser.add_argument("--burst-rate", type=int, default=200)
    parser.add_argument("--queue-capacity", type=int, default=8)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--downstream-concurrency", type=int, default=1)
    parser.add_argument("--process-delay-ms", type=int, default=50)
    parser.add_argument("--job-timeout-ms", type=int, default=10_000)
    parser.add_argument("--tenant-limit", type=int, default=2)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-base-ms", type=int, default=10)
    parser.add_argument("--failures", type=int, default=5)
    parser.add_argument("--cpu-ms", type=int, default=100)
    parser.add_argument("--stream-items", type=int, default=100)
    parser.add_argument("--stream-delay-ms", type=int, default=5)
    parser.add_argument("--output-dir", default="benchmarks/results")
    args = parser.parse_args()

    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")

    stacks = list(STACKS) if args.stack == "all" else [args.stack]
    try:
        for stack in stacks:
            report = run_stack(stack, args.scenario, args)
            print(f"{stack}: sanitized report written to {report.relative_to(ROOT)}")
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"benchmark failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
