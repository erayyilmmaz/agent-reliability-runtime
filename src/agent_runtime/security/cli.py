"""Offline credential provisioning; never runs a server or connects to a database."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from agent_runtime.security.credentials import issue_credential, registry_entry


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a credential and its registry entry")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--expires-at", type=datetime.fromisoformat)
    args = parser.parse_args()
    pepper = os.environ.get("APP_AUTH_PEPPER", "")
    try:
        raw_key, record = issue_credential(
            principal_id=args.principal,
            tenant_id=args.tenant,
            pepper=pepper,
            expires_at=args.expires_at,
        )
    except ValueError:
        parser.error(
            "Use valid identities, a timezone-aware expiry and APP_AUTH_PEPPER (32+ chars)"
        )
    # This command intentionally prints the newly issued secret ONCE for secure delivery.
    print(json.dumps({"api_key": raw_key, "credential": registry_entry(record)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
