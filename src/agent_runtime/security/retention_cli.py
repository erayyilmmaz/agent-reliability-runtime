"""Operator-only, dry-run-first retention preview for the envelope prototype."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.security.envelope import erase_tenant_key, retention_candidates
from agent_runtime.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview tenant key retention; no row deletion.")
    parser.add_argument(
        "--apply", action="store_true", help="Destroy one explicitly confirmed live wrapped key"
    )
    parser.add_argument("--tenant-id")
    parser.add_argument("--confirm-tenant")
    parser.add_argument("--principal-id", default="local-operator")
    args = parser.parse_args()
    if args.apply and (not args.tenant_id or args.confirm_tenant != args.tenant_id):
        parser.error("--apply requires matching --tenant-id and --confirm-tenant")
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    cutoff = datetime.now(UTC) - timedelta(days=settings.payload_retention_days)
    try:
        async with sessions.begin() as session:
            if args.apply:
                changed = await erase_tenant_key(
                    session,
                    tenant_id=args.tenant_id,
                    principal_id=args.principal_id,
                    dry_run=False,
                    confirm_tenant=args.confirm_tenant,
                    cutoff=cutoff,
                )
                result = {"dry_run": False, "destroyed": changed, "tenant_id": args.tenant_id}
            else:
                result = {
                    "dry_run": True,
                    "candidates": await retention_candidates(session, cutoff=cutoff),
                }
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()
