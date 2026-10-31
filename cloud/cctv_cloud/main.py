from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cctv_cloud import __version__
from cctv_cloud.api import (
    admin,
    alerts,
    attendance,
    auth,
    devices,
    edge,
    events,
    features,
    health,
    integrations,
    platform,
    realtime,
    remote,
    reports,
    staff,
)
from cctv_cloud.config import get_settings

DESCRIPTION = """
Multi-tenant API for the AI CCTV system.

* **Users** sign in at `/v1/auth/login` and send `Authorization: Bearer <access token>`.
  Owners and super-admins must use two-factor authentication.
* **Super-admins** pick the tenant to act on with the `X-Tenant-Id` header.
* **Integrations** (POS, HRM) send `X-API-Key: <key>` (created under `/v1/api-keys`).
* **Edge devices** use `/v1/edge/*` with their device token (see contracts/README.md).
* **Live events**: WebSocket `/v1/ws`; send `{"type": "auth", "token": ...}` first.
"""


class SecurityHeaders:
    """Headers for every HTTP response (plan 7.1). API responses hold personal data, so
    they are never cached or framed; HSTS once the API is served over TLS."""

    DOCS = ("/docs", "/redoc", "/openapi.json")  # Swagger UI loads its scripts from a CDN

    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = scope["path"].startswith(self.DOCS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Cache-Control", "no-store")
                if not docs:
                    headers.setdefault("X-Frame-Options", "DENY")
                    headers.setdefault(
                        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
                    )
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AI CCTV Cloud API", version=__version__, description=DESCRIPTION)
    app.add_middleware(SecurityHeaders, hsts=settings.secure_cookies)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_url],
        allow_credentials=True,  # the refresh-token cookie
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
        expose_headers=["Content-Disposition"],  # file names of downloads (reports, CSV)
    )

    @app.exception_handler(IntegrityError)
    def _conflict(_request: Request, _exc: IntegrityError) -> JSONResponse:
        # Constraint violations not handled by an endpoint: report, don't leak SQL.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "conflicts with existing data"},
        )

    for module in (
        health,
        auth,
        platform,
        admin,
        devices,
        features,
        events,
        remote,
        staff,
        attendance,
        alerts,
        reports,
        integrations,
        realtime,
        edge,
    ):
        app.include_router(module.router)
    return app


app = create_app()
