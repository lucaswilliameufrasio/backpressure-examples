#!/usr/bin/env python3
"""Start one or all local example servers and stop them together on Ctrl-C."""

from __future__ import annotations

import argparse
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.common import STACKS, RunningStack, free_port, read_log_tail, start_stack, stop_stack, wait_ready


def _stop_signal(_signum, _frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=(*STACKS, "all"), default="all")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _stop_signal)
    selected = list(STACKS) if args.stack == "all" else [args.stack]

    with tempfile.TemporaryDirectory(prefix="backpressure-examples-") as temporary:
        handles: list[RunningStack] = []
        try:
            for stack in selected:
                port = free_port()
                server = start_stack(stack, port, Path(temporary) / f"{stack}.log")
                handles.append(server)
                wait_ready(server)
                print(f"{stack}: {server.base_url}  logs=<temporary>", flush=True)

            while True:
                for server in handles:
                    exit_code = server.process.poll()
                    if exit_code is not None:
                        details = read_log_tail(server)
                        raise RuntimeError(f"{server.name} exited with code {exit_code}:\n{details}")
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\nStopping local example servers...", flush=True)
        except (OSError, RuntimeError, TimeoutError) as error:
            print(f"startup/runtime error: {error}")
            return 1
        finally:
            for server in reversed(handles):
                stop_stack(server)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
