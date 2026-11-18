"""Background session keepalive.

muse.ai's workspace cookie (``hatch_vml``) lives a fixed ~48 h and using the site
does not extend it; only the session endpoint re-issues it. An account that is
not renewed in time stops working, and a while later its login is rejected for
good. This loop checks every few minutes and renews each account well before
its session runs out, merging the rotated cookies back into the pool.

Drivers that do not support renewal raise ``FeatureNotImplemented`` and are
skipped. When an account is rejected, or keeps failing to renew close to the end,
an optional notify command tells a human to log in again.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass

from ..drivers.base import MuseDriver
from ..errors import FeatureNotImplemented, UpstreamAuthError
from .model import Account, AccountStatus
from .pool import AccountPool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeepalivePolicy:
    check_interval: float = 15 * 60
    renew_before: float = 36 * 3600
    max_age: float = 24 * 3600
    warn_before: float = 12 * 3600
    notify_cmd: str = ""


def needs_renewal(acc: Account, policy: KeepalivePolicy, now: float | None = None) -> bool:
    """Renew when the expiry is unknown, closer than ``renew_before``, or the last
    renewal is older than ``max_age``."""
    if not acc.enabled or acc.status == AccountStatus.INVALID:
        return False
    now = now or time.time()
    exp = acc.session_expires_at()
    renewed = acc.meta.get("renewed_at") or 0
    return exp is None or exp - now < policy.renew_before or now - renewed > policy.max_age


async def renew_account(pool: AccountPool, driver: MuseDriver, account_id: str) -> dict:
    acc = pool.get(account_id)
    if acc is None:
        return {"ok": False, "error": "not found"}
    try:
        info = await driver.renew_session(acc)
    except FeatureNotImplemented as exc:
        return {"ok": False, "error": exc.message}
    except UpstreamAuthError as exc:
        acc.status = AccountStatus.INVALID
        acc.last_error = exc.message
        await pool.upsert(acc)
        return {"ok": False, "error": exc.message, "rejected": True}

    acc.cookies.update(info.cookies)
    acc.cookie_expires.update(info.cookie_expires)
    acc.meta.update(info.meta)
    acc.meta["renewed_at"] = time.time()
    acc.meta.pop("notified", None)
    await pool.upsert(acc)
    return {"ok": info.ok, "meta": info.meta}


def _fmt(ts: float | None) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "unknown"


def notify(policy: KeepalivePolicy, message: str) -> None:
    """Run the notify command with the message as its last argument; never raises."""
    if not policy.notify_cmd:
        return
    try:
        args = shlex.split(policy.notify_cmd, posix=False) + [message]
        subprocess.run(args, timeout=60, check=False, capture_output=True)
    except Exception:  # noqa: BLE001
        log.exception("keepalive notify command failed")


async def _maybe_notify(pool: AccountPool, acc: Account, policy: KeepalivePolicy,
                        kind: str, message: str) -> None:
    """Notify once per kind until the account renews again."""
    if acc.meta.get("notified") == kind:
        return
    acc.meta["notified"] = kind
    await pool.upsert(acc)
    await asyncio.to_thread(notify, policy, message)


async def check_once(pool: AccountPool, driver: MuseDriver, policy: KeepalivePolicy) -> list[dict]:
    results = []
    now = time.time()
    for acc in pool.all():
        if not needs_renewal(acc, policy, now):
            continue
        name = acc.label or acc.id
        try:
            result = await renew_account(pool, driver, acc.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("keepalive failed for %s", acc.id)
            result = {"ok": False, "error": str(exc)}
        results.append({"id": acc.id, **result})
        fresh = pool.get(acc.id) or acc
        if result.get("ok"):
            log.info("keepalive %s renewed; session until %s", acc.id, _fmt(fresh.session_expires_at()))
        elif result.get("rejected"):
            log.warning("keepalive %s rejected: %s", acc.id, result.get("error"))
            await _maybe_notify(pool, fresh, policy, "rejected",
                                f"muse2api: tài khoản {name} bị muse.ai từ chối ({result.get('error')}). "
                                "Cần đăng nhập lại muse.ai và nạp cookie mới; ảnh đang chuyển sang antigravity.")
        else:
            exp = fresh.session_expires_at()
            log.warning("keepalive %s not renewed (%s); session until %s",
                        acc.id, result.get("error") or result.get("meta"), _fmt(exp))
            if exp is not None and exp - time.time() < policy.warn_before:
                await _maybe_notify(pool, fresh, policy, "expiring",
                                    f"muse2api: gia hạn phiên {name} thất bại, phiên hết lúc {_fmt(exp)}. "
                                    f"Lỗi: {str(result.get('error') or result.get('meta'))[:200]}")
    return results


async def keepalive_loop(pool: AccountPool, driver: MuseDriver, policy: KeepalivePolicy) -> None:
    log.info("keepalive loop started (check every %.0fs, renew %.0fh before expiry)",
             policy.check_interval, policy.renew_before / 3600)
    await asyncio.sleep(30)  # let the driver finish starting
    while True:
        try:
            await check_once(pool, driver, policy)
        except Exception:  # noqa: BLE001
            log.exception("keepalive check failed")
        await asyncio.sleep(policy.check_interval)
