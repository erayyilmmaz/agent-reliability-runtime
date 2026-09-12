"""Probe entry point for background workloads (SEC-OPS-01).

Used as the Kubernetes liveness/readiness `exec` command for the dispatcher,
worker and scheduler, which serve no HTTP traffic.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from agent_runtime.observability.heartbeat import read_heartbeat


def main() -> None:
    # Deliberately reads the environment directly instead of building Settings.
    # A liveness probe must not fail because some unrelated part of the
    # configuration is invalid -- that would report the process as dead when it
    # is running fine, and would restart it into the same broken config.
    args = _parser().parse_args()
    configured = args.path or os.environ.get("APP_HEARTBEAT_PATH")
    if not configured:
        raise SystemExit("heartbeat path is not configured; set APP_HEARTBEAT_PATH or --path")
    path = Path(configured)

    status = read_heartbeat(path)
    if status is None:
        raise SystemExit(f"no readable heartbeat at {path}")
    if not status.is_fresh(args.max_age):
        raise SystemExit(
            f"{status.component} heartbeat is {status.age_seconds:.1f}s old "
            f"(limit {args.max_age:.1f}s)"
        )
    print(f"{status.component} heartbeat is {status.age_seconds:.1f}s old")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exit non-zero when a background process has stopped making progress."
    )
    parser.add_argument("--path", help="Heartbeat file path; defaults to APP_HEARTBEAT_PATH")
    parser.add_argument(
        "--max-age",
        type=float,
        default=60.0,
        help="Maximum accepted heartbeat age in seconds (default: 60)",
    )
    return parser


if __name__ == "__main__":
    main()
