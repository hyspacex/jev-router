"""Small ASGI ingress boundary; does not buffer or rewrite streamed bodies."""

from __future__ import annotations

import hmac
import ipaddress
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings


class BodyTooLarge(Exception):
    pass


class Ingress:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.token = os.environ.get(settings.admin_token_env, "")
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        if path == "/router" or path.startswith("/router/"):
            supplied = headers.get("x-router-admin-token", "")
            client = scope.get("client")
            try:
                local = bool(client and ipaddress.ip_address(client[0]).is_loopback)
            except ValueError:
                local = False
            authorized = (
                hmac.compare_digest(supplied.encode(), self.token.encode())
                if self.token
                else local
            )
            if not authorized:
                await JSONResponse(
                    {
                        "error": {
                            "type": "router_auth",
                            "message": "router endpoint authentication required",
                        }
                    },
                    status_code=401,
                )(scope, receive, send)
                return
        if self.active >= self.settings.max_concurrent_requests:
            await JSONResponse(
                {
                    "error": {
                        "type": "router_busy",
                        "message": "router concurrency limit reached",
                    }
                },
                status_code=503,
            )(scope, receive, send)
            return
        consumed = 0
        started = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.settings.max_body_bytes:
                    raise BodyTooLarge
            return message

        async def track_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        self.active += 1
        try:
            length = headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    size = 0  # actual bytes are still bounded
                if size > self.settings.max_body_bytes:
                    raise BodyTooLarge
            await self.app(scope, bounded_receive, track_send)
        except BodyTooLarge:
            if started:
                raise  # cannot change status after streaming starts
            await JSONResponse(
                {
                    "error": {
                        "type": "request_too_large",
                        "message": "request exceeds router body limit",
                    }
                },
                status_code=413,
            )(scope, receive, send)
        finally:
            self.active -= 1
