"""Collect a small, explicit allowlist of environment metadata."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _command_version(
    command: list[str],
    pattern: str | None = None,
    mise_path: str | None = None,
    cwd: Path | None = None,
) -> str | None:
    executable = mise_path or shutil.which(command[0])
    if not executable:
        return None
    if mise_path:
        command = [mise_path, "exec", "--", *command]
    try:
        result = subprocess.run(command, cwd=cwd or ROOT, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if not output:
        return None
    if pattern:
        match = re.search(pattern, output, re.MULTILINE)
        return match.group(0)[:100] if match else None
    return next((line[:100] for line in output.splitlines() if line.strip()), None)


def _os_release() -> tuple[str | None, str | None]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VERSION_ID"}:
                values[key] = value.strip().strip('"').replace("'", "")[:32]
    except OSError:
        pass
    return values.get("ID"), values.get("VERSION_ID")


def _cpu_model() -> str | None:
    if platform.system() != "Linux":
        return None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"model name", "hardware", "processor"}:
                safe = re.sub(r"[^A-Za-z0-9 ._()+/-]", "", value).strip()
                if safe and not safe.isdigit():
                    return safe[:80]
    except OSError:
        pass
    return None


def _memory_gib(path: Path) -> float | None:
    try:
        value = path.read_text(encoding="ascii").strip()
        if value == "max":
            return None
        gib = int(value) / (1024**3)
        return round(gib * 2) / 2
    except (OSError, ValueError):
        return None


def _cpu_limit() -> float | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if quota == "max":
            return None
        cores = int(quota) / int(period)
        return round(cores * 4) / 4
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _source_status_entries_clean(entries: list[bytes]) -> bool:
    generated_prefixes = (b"benchmarks/results/", b"benchmarks/profiling/results/")
    for entry in entries:
        path = entry[3:] if len(entry) >= 3 else entry
        if not path.startswith(generated_prefixes):
            return False
    return True


def _working_tree_clean() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "-z"],
        cwd=ROOT,
        capture_output=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        return False

    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    return _source_status_entries_clean(entries)


def collect_environment() -> dict:
    distro, distro_version = _os_release()
    kernel_match = re.match(r"^(\d+(?:\.\d+){0,3})", platform.release())

    try:
        total_memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        total_memory_gib = round((total_memory / (1024**3)) * 2) / 2
    except (ValueError, OSError, AttributeError):
        total_memory_gib = None

    mise_path = shutil.which("mise")
    tools = {
        "go": _command_version(["go", "version"], r"go version go[^\s]+", mise_path),
        "rustc": _command_version(["rustc", "--version"], r"rustc [^\s]+", mise_path),
        "cargo": _command_version(["cargo", "--version"], r"cargo [^\s]+", mise_path),
        "node": _command_version(["node", "--version"], r"v\d+(?:\.\d+){1,2}", mise_path),
        "pnpm": _command_version(
            ["pnpm", "--version"], r"\d+\.\d+\.\d+", mise_path, Path(__file__).resolve().parents[1] / "node"
        ),
        "elixir": _command_version(
            ["elixir", "--version"], r"Elixir \d+(?:\.\d+){1,2}", mise_path
        ),
        "erlang_otp": _command_version(
            ["erl", "-noshell", "-eval", 'io:format("~s~n", [erlang:system_info(otp_release)]), halt().'],
            r"\d+",
            mise_path,
        ),
        "mix": _command_version(["mix", "--version"], r"Mix \d+(?:\.\d+){1,2}", mise_path),
        "oha": _command_version(["oha", "--version"], r"oha [^\s]+", mise_path),
        "golangci_lint": _command_version(
            ["golangci-lint", "--version"], r"golangci-lint has version [^\s]+", mise_path
        ),
        "mise": _command_version(["mise", "--version"], r"\d{4}\.\d+\.\d+"),
        "bpftrace": _command_version(["bpftrace", "--version"], r"bpftrace v?\d+(?:\.\d+)*", mise_path),
        "perf": _command_version(["perf", "--version"], r"perf version [^\s]+", mise_path),
    }

    commit = _command_version(
        ["git", "rev-parse", "--short=12", "HEAD"],
        r"[0-9a-f]{7,12}",
    )
    return {
        "os": {
            "family": platform.system().lower(),
            "distribution_id": distro,
            "distribution_version": distro_version,
            "kernel_version": kernel_match.group(1) if kernel_match else None,
            "architecture": platform.machine()[:32],
        },
        "hardware": {
            "cpu_model": _cpu_model(),
            "logical_cores": os.cpu_count(),
            "memory_gib_rounded": total_memory_gib,
        },
        "container_limits": {
            "cpu_cores_rounded": _cpu_limit(),
            "memory_gib_rounded": _memory_gib(Path("/sys/fs/cgroup/memory.max")),
        },
        "tools": {key: value for key, value in tools.items() if value is not None},
        "git_commit": commit,
        "working_tree_clean": _working_tree_clean(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }


if __name__ == "__main__":
    print(json.dumps(collect_environment(), indent=2, sort_keys=True))
