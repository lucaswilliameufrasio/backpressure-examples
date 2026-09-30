#!/usr/bin/env python3
"""Report whether local tools needed by the examples are available."""

from __future__ import annotations

import argparse
import sys

from collect_environment import collect_environment


REQUIRED = ("go", "rustc", "cargo", "node", "pnpm", "elixir", "erlang_otp", "mix", "golangci_lint")
OPTIONAL = ("mise", "oha", "bpftrace", "perf")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ebpf", action="store_true", help="include Linux eBPF tool readiness")
    args = parser.parse_args()

    environment = collect_environment()
    versions = environment["tools"]
    missing = []
    for tool in REQUIRED:
        version = versions.get(tool)
        display_name = {"golangci_lint": "golangci-lint", "erlang_otp": "Erlang/OTP"}.get(tool, tool)
        if version:
            print(f"[ok]       {display_name}: {version}")
        else:
            print(f"[missing]  {display_name}")
            missing.append(display_name)

    for tool in OPTIONAL:
        version = versions.get(tool)
        display_name = tool
        label = "[ok]       " if version else "[optional] "
        print(f"{label}{display_name}: {version or 'not installed'}")

    if args.ebpf:
        if environment["os"]["family"] != "linux":
            print("[info]     eBPF profiling requires Linux")
        elif "bpftrace" not in versions and "perf" not in versions:
            print("[optional] install bpftrace or perf to run the profiling guide")
        else:
            print("[info]     kernel permissions/capabilities still require local validation")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
