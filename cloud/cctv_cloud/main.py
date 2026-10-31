from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AI CCTV Cloud API", version=__version__, description=DESCRIPTION)
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
