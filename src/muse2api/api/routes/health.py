from fastapi import APIRouter, Depends

from ... import __version__
from ...services.container import Services
from ..deps import get_services

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/readyz")
async def readyz(svc: Services = Depends(get_services)) -> dict:
    driver = await svc.driver.health()
    pool = svc.pool.stats()
    ready = driver.get("ok", False) and (pool["available"] > 0 or not svc.driver.requires_account)
    # Latest session end among usable accounts: the gateway works until then even if renewal stops.
    ends = [e for a in svc.pool.all() if a.is_available() and (e := a.session_expires_at())]
    pool["session_expires_at"] = int(max(ends)) if ends else None
    pool["keepalive"] = svc.settings.keepalive_enabled
    return {"ready": ready, "driver": driver, "accounts": pool}
