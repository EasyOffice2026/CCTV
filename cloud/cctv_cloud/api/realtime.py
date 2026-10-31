"""Live event feed for the dashboard over a WebSocket (plan 4.2).

Browsers can't set headers on a WebSocket, so the access token comes in the first
message instead of the URL (URLs end up in logs):

    client -> {"type": "auth", "token": "<access token>", "tenant_id": "<super-admins only>"}
    server -> {"type": "ready"}
    server -> {"type": "event", "event_id": ..., "status": ..., "branch_id": ..., ...}
    server -> {"type": "alert", "event_id": ..., "title": ..., "body": ..., "url": ...}
    server -> {"type": "ping"}                      every 25 s
    client -> {"type": "auth", "token": "<new access token>"}   after each token refresh

Only events of branches the user can access are sent; the client loads the full event
with GET /v1/events/{id}. Alerts (plan 6.2, in-app toasts) go only to the user they were
routed to. The session is re-checked every minute, and the socket closes
with code 4401 when the token expires without being renewed or the session ends.
"""

import asyncio
import contextlib
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from cctv_cloud.api.deps import user_principal
from cctv_cloud.rbac import Permission, Principal
from cctv_cloud.security import InvalidTokenError, decode_token
from cctv_cloud.services.notify import Subscriber, get_subscriber

router = APIRouter(tags=["realtime"])

AUTH_TIMEOUT_SECONDS = 10
PING_SECONDS = 25
RECHECK_SECONDS = 60

CLOSE_UNAUTHORIZED = 4401


class _Auth:
    def __init__(self, message: dict[str, Any], principal: Principal, expires_at: float) -> None:
        self.message = message  # kept to re-check the session later
        self.principal = principal
        self.expires_at = expires_at
        self.checked_at = time.monotonic()


async def _authenticate(message: Any, current: _Auth | None = None) -> _Auth:
    if not isinstance(message, dict) or message.get("type") != "auth":
        raise PermissionError("send an auth message first")
    token = str(message.get("token") or "")
    try:
        claims = decode_token(token, "access")
        tenant = message.get("tenant_id")
        principal = await run_in_threadpool(
            user_principal, token, uuid.UUID(tenant) if tenant else None
        )
    except (InvalidTokenError, ValueError, HTTPException) as exc:
        raise PermissionError("invalid or expired token") from exc
    if not principal.can(Permission.EVENTS_VIEW) or principal.tenant_id is None:
        raise PermissionError("not allowed")
    if current is not None and (
        principal.user_id != current.principal.user_id
        or principal.tenant_id != current.principal.tenant_id
    ):
        raise PermissionError("a socket can't change user or tenant")
    # Seconds on the monotonic clock until the token expires.
    expires_at = time.monotonic() + (claims["exp"] - time.time())
    return _Auth(message, principal, expires_at)


def _visible(principal: Principal, message: dict[str, Any]) -> bool:
    recipient = message.get("user_id")
    if recipient is not None:  # an alert: routing already checked the user's branches
        return recipient == str(principal.user_id)
    try:
        return principal.can_access_branch(uuid.UUID(message["branch_id"]))
    except (KeyError, ValueError, TypeError):
        return False


@router.websocket("/v1/ws")
async def events_socket(
    websocket: WebSocket, subscriber: Annotated[Subscriber, Depends(get_subscriber)]
) -> None:
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT_SECONDS)
        auth = await _authenticate(first)
    except (TimeoutError, PermissionError, ValueError) as exc:
        await websocket.close(CLOSE_UNAUTHORIZED, str(exc)[:120] or "authentication timed out")
        return
    except WebSocketDisconnect:
        return

    async with subscriber.subscribe(f"events:{auth.principal.tenant_id}") as messages:
        await websocket.send_json({"type": "ready"})  # subscribed: nothing is missed from here
        incoming = asyncio.ensure_future(websocket.receive_json())
        published = asyncio.ensure_future(anext(messages))
        try:
            while True:
                timeout = min(PING_SECONDS, auth.expires_at - time.monotonic())
                done, _ = await asyncio.wait(
                    {incoming, published}, timeout=max(timeout, 0), return_when="FIRST_COMPLETED"
                )
                if incoming in done:
                    auth = await _authenticate(incoming.result(), auth)
                    incoming = asyncio.ensure_future(websocket.receive_json())
                if published in done:
                    message = published.result()
                    if _visible(auth.principal, message):
                        await websocket.send_json({"type": "event", **message})
                    published = asyncio.ensure_future(anext(messages))
                if not done:
                    if time.monotonic() >= auth.expires_at:
                        raise PermissionError("token expired")
                    if time.monotonic() - auth.checked_at > RECHECK_SECONDS:
                        # Session revoked, user deactivated or role changed?
                        auth = await _authenticate(auth.message, auth)
                    await websocket.send_json({"type": "ping"})
        except (PermissionError, ValueError) as exc:  # ValueError: not JSON
            await websocket.close(CLOSE_UNAUTHORIZED, str(exc)[:120])
        except (WebSocketDisconnect, RuntimeError):
            pass  # the client went away
        finally:
            for task in (incoming, published):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
