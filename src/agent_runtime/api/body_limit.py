"""Bound actual ASGI request bytes before parsing JSON or invoking a handler."""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int, timeout_seconds: int) -> None:
        self.app, self.max_bytes, self.timeout_seconds = app, max_bytes, timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(code: int, reason: str) -> None:
            await JSONResponse(
                status_code=code,
                content={"error": {"code": reason, "message": "Request body rejected."}},
            )(scope, receive, send)

        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        transfer = any(k.lower() == b"transfer-encoding" for k, _ in scope["headers"])
        if len(lengths) > 1 or (
            lengths and (transfer or not lengths[0].isdigit() or len(lengths[0]) > 10)
        ):
            await reject(400, "INVALID_CONTENT_LENGTH")
            return
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > self.max_bytes:
            await reject(413, "REQUEST_TOO_LARGE")
            return
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        await reject(413, "REQUEST_TOO_LARGE")
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408, "REQUEST_BODY_TIMEOUT")
            return
        if declared is not None and declared != len(body):
            await reject(400, "INVALID_CONTENT_LENGTH")
            return
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)
