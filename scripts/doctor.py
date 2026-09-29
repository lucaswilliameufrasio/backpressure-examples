#!/usr/bin/env python3
"""Report whether local tools needed by the examples are available."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_environment import collect_environment


REQUIRED = ("go", "rustc", "cargo", "node", "pnpm", "elixir", "erlang_otp", "mix", "golangci_lint")
OPTIONAL = ("mise", "oha", "bpftrace", "perf")


def ebpf_install_hint(distribution_id: str | None) -> str | None:
    distro = (distribution_id or "").lower()
    if distro in {"arch", "cachyos", "manjaro", "endeavouros"}:
        return "sudo pacman -S bpftrace"
    if distro in {"debian", "ubuntu", "linuxmint", "pop"}:
        return "sudo apt install bpftrace"
    if distro in {"fedora", "rhel", "centos", "rocky", "almalinux"}:
        return "sudo dnf install bpftrace"
    if distro in {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"}:
        return "sudo zypper install bpftrace"
    if distro == "nixos":
        return "nix-shell -p bpftrace"
    return None


def sysctl_integer(name: str) -> int | None:
    try:
        return int(Path(f"/proc/sys/kernel/{name}").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


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

    if missing:
        mise_tools = {
            "go": "go",
            "rustc": "rust",
            "cargo": "rust",
            "node": "node",
            "pnpm": "pnpm",
            "elixir": "elixir",
            "Erlang/OTP": "erlang",
            "mix": "elixir",
            "golangci-lint": "golangci-lint",
        }
        install = sorted({mise_tools[name] for name in missing if name in mise_tools})
        if install:
            print(f"[fix]      Ferramentas ausentes: mise install {' '.join(install)}")
        if "pnpm" in missing:
            print("[fix]      Alternativa para pnpm: entre em node/ e rode `corepack pnpm --version` (usa o pin do package.json).")
        print("[fix]      Depois, rode `make doctor` novamente para conferir.")

    for tool in OPTIONAL:
        version = versions.get(tool)
        display_name = tool
        label = "[ok]       " if version else "[optional] "
        print(f"{label}{display_name}: {version or 'not installed'}")

    if args.ebpf:
        if environment["os"]["family"] != "linux":
            print("[info]     eBPF profiling requires Linux")
        else:
            btf = Path("/sys/kernel/btf/vmlinux").is_file()
            print(f"[ebpf]     kernel BTF: {'available' if btf else 'not detected'}")
            if "bpftrace" not in versions:
                hint = ebpf_install_hint(environment["os"].get("distribution_id"))
                print("[optional] bpftrace is not installed and is not pinned in mise.toml")
                if hint:
                    print(f"[setup]    install with: {hint}")
                else:
                    print("[setup]    install bpftrace with your Linux distribution package manager")
            else:
                print(f"[ebpf]     bpftrace: {versions['bpftrace']}")
                unprivileged = sysctl_integer("unprivileged_bpf_disabled")
                perf_paranoid = sysctl_integer("perf_event_paranoid")
                restricted = (unprivileged is not None and unprivileged > 0) or (
                    perf_paranoid is not None and perf_paranoid > 1
                )
                if restricted:
                    print("[ebpf]     unprivileged probes are restricted; make ebpf-probe may report permission-denied")
                    print("[setup]    run `sudo -v`, then `make ebpf-probe-sudo` to test one minimal probe")
                else:
                    print("[ebpf]     run make ebpf-check for readiness or make ebpf-probe for a minimal probe")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
