"""Run a credentials-free tour of the local Agent Reliability Runtime stack."""

from __future__ import annotations

import argparse
import json

from agent_runtime.client import AgentRuntimeClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local recruiter demo.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--client-id", default="recruiter-demo")
    parser.add_argument("--api-key")
    parser.add_argument("--timeout-seconds", default=30.0, type=float)
    args = parser.parse_args()

    with AgentRuntimeClient(
        base_url=args.base_url, client_id=args.client_id, api_key=args.api_key
    ) as client:
        print("1/4 API health:", json.dumps(client.healthz(), sort_keys=True))
        submitted = client.run({"prompt": "recruiter demo: durable agent work"})
        print(
            "2/4 Accepted:",
            json.dumps(
                {
                    "run_id": str(submitted.run_id),
                    "status": submitted.execution_status,
                    "idempotency_key": submitted.idempotency_key,
                    "routing": submitted.routing_decision,
                },
                sort_keys=True,
            ),
        )
        completed = client.wait_for_terminal(submitted.run_id, timeout_seconds=args.timeout_seconds)
        print(
            "3/4 Completed:",
            json.dumps(
                {
                    "status": completed.execution_status,
                    "evaluation_status": completed.evaluation_status,
                    "error_code": completed.error_code,
                },
                sort_keys=True,
            ),
        )
        print(
            "4/4 Durable history:",
            json.dumps(
                {
                    "attempts": client.get_attempts(submitted.run_id),
                    "events": client.get_events(submitted.run_id),
                },
                sort_keys=True,
            ),
        )

    print("Observability: http://localhost:3000 (Grafana dashboard: Agent Reliability Runtime)")
    print("Failure/retry/fallback evidence: docs/testing/failure-matrix.md")


if __name__ == "__main__":
    main()
