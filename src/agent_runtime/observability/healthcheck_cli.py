"""Probe entry point for background workloads (SEC-OPS-01).

Used as the Kubernetes liveness/readiness `exec` command for the dispatcher,
worker and scheduler, which serve no HTTP traffic.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime.observability.heartbeat import read_heartbeat
from agent_runtime.settings import get_settings


def main() -> None:
    args = _parser().parse_args()
    path = Path(args.path) if args.path is not None else get_settings().heartbeat_path
    if path is None:
        raise SystemExit("heartbeat path is not configured")

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
