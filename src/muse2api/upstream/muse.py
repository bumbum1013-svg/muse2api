"""Facts about the muse.ai web app shared by all drivers.

Everything here was observed from the public web client and may change without
notice. Keep upstream-specific constants in this module so that a site update
is a one-file fix.
"""

from __future__ import annotations

import uuid

import httpx

from ..drivers.base import SessionInfo
from ..errors import UpstreamAuthError, UpstreamError

SITE = "https://muse.ai"
NEW_THREAD_URL = f"{SITE}/thread/new"
SESSION_API = f"{SITE}/api/session"
VM_WAKE_API = f"{SITE}/api/hatch/vm/wake"

# The login itself (about 30 days). Everything else is optional: the session
# endpoint re-issues the short-lived workspace cookie from this one.
SESSION_COOKIES = ("hatch_sess",)

# Workspace cookie that decides how long an account keeps working. It lives a
# fixed ~48 h, using the site does not extend it, and only GET /api/session
# re-issues it (observed by github.com/czg86389-hub/muse2api, MIT, and here:
# an account left unrenewed from 08/11 was rejected for good by 18/11/2026).
WORKSPACE_COOKIE = "hatch_vml"
WORKSPACE_TTL = 48 * 3600


def session_expires_at(cookie_expires: dict[str, int], renewed_at: float | None) -> float | None:
    """When the workspace cookie runs out: its real expiry, else the last renewal
    plus its fixed lifetime, else unknown."""
    exp = cookie_expires.get(WORKSPACE_COOKIE) or 0
    if exp > 0:
        return float(exp)
    return renewed_at + WORKSPACE_TTL if renewed_at else None

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def missing_session_cookies(cookies: dict[str, str]) -> list[str]:
    return [name for name in SESSION_COOKIES if not cookies.get(name)]


async def renew_session(
    cookies: dict[str, str], *, wake_vm: bool = True, timeout: float = 15.0
) -> SessionInfo:
    """Hit the session endpoint, which rotates short-lived cookies via Set-Cookie,
    and optionally wake the account's cloud workspace so the first request is fast."""
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": SITE,
        "Referer": NEW_THREAD_URL,
        "Accept": "application/json",
    }
    jar = dict(cookies)
    expires: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(SESSION_API, headers={"Cookie": cookie_header(jar)})
        if resp.status_code in (401, 403):
            raise UpstreamAuthError(f"session rejected by upstream (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise UpstreamError(f"session endpoint returned HTTP {resp.status_code}")

        for c in resp.cookies.jar:
            if c.value:
                jar[c.name] = c.value
            if c.expires:
                expires[c.name] = int(c.expires)

        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        meta = {"session_status": body.get("status"), "vm_id": body.get("vm_id"),
                "vm_state": body.get("vm_state")}

        if wake_vm and meta["vm_id"] and meta["vm_state"] != "DISABLED":
            try:
                w = await client.post(
                    VM_WAKE_API,
                    headers={"Cookie": cookie_header(jar)},
                    json={"vm_id": meta["vm_id"], "retry_count": 0,
                          "connect_attempt_id": str(uuid.uuid4())},
                )
                meta["vm_wake_ok"] = w.status_code == 200
            except httpx.HTTPError:
                meta["vm_wake_ok"] = False

    return SessionInfo(ok=meta["session_status"] == "assigned", cookies=jar,
                       cookie_expires=expires, meta=meta)
