from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import ALLOW_INSECURE_HTTP, MAX_REQUEST_BODY_BYTES


class RequestTooLarge(RuntimeError):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = PlainTextResponse("Invalid Content-Length", status_code=400)
                await response(scope, receive, send)
                return

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            response = PlainTextResponse("Request body too large", status_code=413)
            await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                cache_control = (
                    b"public, max-age=86400"
                    if str(scope.get("path", "")).startswith("/static/")
                    else b"no-store"
                )
                additions = {
                    b"content-security-policy": (
                        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                        b"script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                        b"base-uri 'self'; form-action 'self'"
                    ),
                    b"x-frame-options": b"DENY",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"cache-control": cache_control,
                }
                existing = {key.lower() for key, _value in headers}
                for key, value in additions.items():
                    if key not in existing:
                        headers.append((key, value))
                if scope.get("scheme") == "https":
                    headers.append(
                        (
                            b"strict-transport-security",
                            b"max-age=31536000; includeSubDomains",
                        )
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secured_send)


class RequireHTTPSMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and not ALLOW_INSECURE_HTTP
            and scope.get("path") != "/healthz"
            and scope.get("scheme") != "https"
        ):
            response = PlainTextResponse(
                "HTTPS is required. Terminate TLS at a trusted reverse proxy or "
                "set NASITRON_ALLOW_INSECURE_HTTP=true only on a trusted network.",
                status_code=426,
                headers={"Upgrade": "TLS/1.2, HTTP/1.1"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
