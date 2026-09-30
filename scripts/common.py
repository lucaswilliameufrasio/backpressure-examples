"""Shared local process helpers. Never serializes the inherited environment."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
STACKS = ("go", "rust", "node", "elixir")
DEFAULT_CONFIG = {
    "queue_capacity": 8,
    "workers": 2,
    "downstream_concurrency": 1,
    "process_delay_ms": 50,
    "job_timeout_ms": 5_000,
    "rate_per_second": 10,
    "rate_burst": 20,
    "tenant_outstanding_limit": 0,
    "max_retries": 3,
    "retry_base_ms": 25,
    "enable_pprof": False,
    "pprof_port": 6060,
}


@dataclass
class RunningStack:
    name: str
    port: int
    process: subprocess.Popen[str]
    log_file: TextIO

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class ProcessResourceSampler:
    """Sample only CPU time and RSS for a server process group on Linux."""

    def __init__(self, process_group: int, interval_seconds: float = 0.1):
        self.process_group = process_group
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[tuple[float, float, int]] = []

    def start(self) -> None:
        if not Path("/proc/self/stat").exists():
            return
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        while not self._stop_event.is_set():
            timestamp = time.monotonic()
            cpu_seconds, rss_kib = self._read_group(ticks_per_second)
            self._samples.append((timestamp, cpu_seconds, rss_kib))
            self._stop_event.wait(self.interval_seconds)

    def _read_group(self, ticks_per_second: int) -> tuple[float, int]:
        total_cpu_ticks = 0
        total_rss_kib = 0
        try:
            proc_entries = Path("/proc").iterdir()
            for entry in proc_entries:
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text(encoding="ascii")
                    fields = stat.rsplit(")", 1)[1].strip().split()
                    process_group = int(fields[2])
                    if process_group != self.process_group:
                        continue
                    total_cpu_ticks += int(fields[11]) + int(fields[12])
                    for line in (entry / "status").read_text(encoding="ascii").splitlines():
                        if line.startswith("VmRSS:"):
                            total_rss_kib += int(line.split()[1])
                            break
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            return 0.0, 0
        return total_cpu_ticks / ticks_per_second, total_rss_kib

    def stop(self) -> dict[str, int | float | bool | None]:
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=2)
            self._sample_loop_once()

        if not self._samples:
            return {
                "sampled": False,
                "average_cpu_cores": None,
                "peak_rss_mib": None,
                "sample_interval_ms": int(self.interval_seconds * 1000),
            }

        first_time, first_cpu, _ = self._samples[0]
        last_time, last_cpu, _ = self._samples[-1]
        elapsed = max(last_time - first_time, 0.001)
        peak_rss = max(sample[2] for sample in self._samples)
        return {
            "sampled": True,
            "average_cpu_cores": round(max(0.0, last_cpu - first_cpu) / elapsed, 3),
            "peak_rss_mib": round(peak_rss / 1024, 2),
            "sample_interval_ms": int(self.interval_seconds * 1000),
        }

    def _sample_loop_once(self) -> None:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        timestamp = time.monotonic()
        cpu_seconds, rss_kib = self._read_group(ticks_per_second)
        self._samples.append((timestamp, cpu_seconds, rss_kib))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stack_environment(name: str, port: int, overrides: dict[str, int] | None = None) -> dict[str, str]:
    if name not in STACKS:
        raise ValueError(f"unknown stack: {name}")

    config = {**DEFAULT_CONFIG, **(overrides or {})}
    env = os.environ.copy()
    env.update(
        {
            "QUEUE_CAPACITY": str(config["queue_capacity"]),
            "WORKERS": str(config["workers"]),
            "DOWNSTREAM_CONCURRENCY": str(config["downstream_concurrency"]),
            "PROCESS_DELAY_MS": str(config["process_delay_ms"]),
            "JOB_TIMEOUT_MS": str(config["job_timeout_ms"]),
            "RATE_PER_SECOND": str(config["rate_per_second"]),
            "RATE_BURST": str(config["rate_burst"]),
            "TENANT_OUTSTANDING_LIMIT": str(config["tenant_outstanding_limit"]),
            "MAX_RETRIES": str(config["max_retries"]),
            "RETRY_BASE_MS": str(config["retry_base_ms"]),
            "ENABLE_PPROF": "1" if config["enable_pprof"] else "0",
            "PPROF_ADDR": f"127.0.0.1:{config['pprof_port']}",
            "HOST": "127.0.0.1",
        }
    )
    if name == "go":
        env["ADDR"] = f"127.0.0.1:{port}"
    else:
        env["PORT"] = str(port)
    return env


def stack_command(name: str) -> tuple[list[str], Path]:
    directories = {
        "go": ROOT / "go",
        "rust": ROOT / "rust",
        "node": ROOT / "node",
        "elixir": ROOT / "elixir",
    }
    commands = {
        "go": ["go", "run", "."],
        "rust": ["cargo", "run", "--release", "--locked"],
        "node": ["pnpm", "start"],
        "elixir": ["mix", "run", "--no-halt"],
    }
    return commands[name], directories[name]


def start_stack(
    name: str,
    port: int,
    log_path: Path,
    overrides: dict[str, int] | None = None,
) -> RunningStack:
    command, cwd = stack_command(name)
    log_file = log_path.open("w", encoding="utf-8")
    try:
        env = stack_environment(name, port, overrides)
        if name == "go":
            executable = log_path.parent / ("backpressure-go.exe" if os.name == "nt" else "backpressure-go")
            build = subprocess.run(
                ["go", "build", "-o", str(executable), "."],
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"Go build failed; see temporary log {log_path}")
            command = [str(executable)]
        elif name == "rust":
            build = subprocess.run(
                ["cargo", "build", "--release", "--locked"],
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
            if build.returncode != 0:
                raise RuntimeError(f"Rust build failed; see temporary log {log_path}")
            binary = "backpressure-examples-rust.exe" if os.name == "nt" else "backpressure-examples-rust"
            command = [str(cwd / "target" / "release" / binary)]
        elif name == "node":
            command = ["node", "src/server.js"]

        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(os.name == "posix"),
        )
    except BaseException:
        log_file.close()
        raise
    return RunningStack(name=name, port=port, process=process, log_file=log_file)


def request_json(url: str, method: str = "GET", timeout: float = 2.0) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            payload = json.loads(body) if body else {}
            return response.status, payload
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}
        return error.code, payload


def wait_ready(server: RunningStack, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{server.base_url}/healthz"
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            raise RuntimeError(f"{server.name} exited before health check passed:\n{read_log_tail(server)}")
        try:
            status, _ = request_json(health_url, timeout=0.5)
            if status == 200:
                return
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise TimeoutError(f"{server.name} did not become healthy:\n{read_log_tail(server)}")


def wait_idle(server: RunningStack, timeout_seconds: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        status, latest = request_json(f"{server.base_url}/metrics")
        if status == 200 and latest.get("queue_depth", 0) == 0 and latest.get("jobs_in_flight", 0) == 0:
            return latest
        time.sleep(0.05)
    return latest


def stop_stack(server: RunningStack, timeout_seconds: float = 10.0) -> dict[str, float | int | bool]:
    started = time.monotonic()
    if server.process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(server.process.pid, signal.SIGTERM)
            else:
                server.process.terminate()
            server.process.wait(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(server.process.pid, signal.SIGKILL)
            else:
                server.process.kill()
            server.process.wait(timeout=2)
    else:
        timed_out = False

    duration = time.monotonic() - started
    result: dict[str, float | int | bool] = {
        "duration_seconds": round(duration, 6),
        "exit_code": int(server.process.returncode or 0),
        "timed_out": timed_out,
    }
    server.log_file.close()
    return result


def read_log_tail(server: RunningStack, max_lines: int = 30) -> str:
    try:
        server.log_file.flush()
        lines = server.log_file.name and Path(server.log_file.name).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        return "\n".join(lines[-max_lines:]) if lines else "(no server output)"
    except OSError:
        return "(server output unavailable)"
