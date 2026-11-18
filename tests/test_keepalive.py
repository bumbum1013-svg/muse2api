from __future__ import annotations

import time

from muse2api.accounts import Account, AccountPool, AccountStatus, JsonAccountStore
from muse2api.accounts import keepalive as ka
from muse2api.drivers.base import SessionInfo
from muse2api.drivers.mock import MockDriver
from muse2api.errors import UpstreamAuthError, UpstreamError
from muse2api.upstream.muse import WORKSPACE_TTL, missing_session_cookies, session_expires_at

H = 3600
POLICY = ka.KeepalivePolicy(renew_before=36 * H, max_age=24 * H, warn_before=12 * H, notify_cmd="notify")


def test_only_hatch_sess_is_required():
    assert missing_session_cookies({"hatch_sess": "x"}) == []
    assert missing_session_cookies({"hatch_vml": "x"}) == ["hatch_sess"]


def test_session_expiry_prefers_real_cookie_expiry():
    assert session_expires_at({"hatch_vml": 1000}, 50) == 1000
    assert session_expires_at({}, 50) == 50 + WORKSPACE_TTL
    assert session_expires_at({}, None) is None


def _acc(**kw) -> Account:
    return Account(id="a", cookies={"hatch_sess": "x"}, **kw)


def test_needs_renewal():
    now = time.time()
    assert ka.needs_renewal(_acc(), POLICY, now)  # expiry unknown
    fresh = _acc(cookie_expires={"hatch_vml": int(now + 47 * H)}, meta={"renewed_at": now - H})
    assert not ka.needs_renewal(fresh, POLICY, now)
    assert ka.needs_renewal(_acc(cookie_expires={"hatch_vml": int(now + 30 * H)},
                                 meta={"renewed_at": now - H}), POLICY, now)  # under 36 h left
    assert ka.needs_renewal(_acc(cookie_expires={"hatch_vml": int(now + 47 * H)},
                                 meta={"renewed_at": now - 25 * H}), POLICY, now)  # renewed too long ago
    assert not ka.needs_renewal(_acc(status=AccountStatus.INVALID), POLICY, now)
    assert not ka.needs_renewal(_acc(enabled=False), POLICY, now)


class _Driver(MockDriver):
    def __init__(self, outcome):
        super().__init__(delay=0)
        self.outcome = outcome
        self.calls = 0

    async def renew_session(self, account):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def _pool(tmp_path, acc: Account) -> AccountPool:
    pool = AccountPool(JsonAccountStore(tmp_path / "acc.json"))
    await pool.upsert(acc)
    return pool


async def test_check_once_renews_and_stores_rotated_cookie(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ka, "notify", lambda policy, msg: sent.append(msg))
    exp = int(time.time() + 48 * H)
    pool = await _pool(tmp_path, _acc())
    driver = _Driver(SessionInfo(ok=True, cookies={"hatch_vml": "new"}, cookie_expires={"hatch_vml": exp}))
    await ka.check_once(pool, driver, POLICY)
    acc = pool.get("a")
    assert acc.cookies["hatch_vml"] == "new"
    assert acc.session_expires_at() == exp
    assert driver.calls == 1
    await ka.check_once(pool, driver, POLICY)  # fresh now: no second call
    assert driver.calls == 1 and sent == []


async def test_rejected_account_is_invalidated_and_notified_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ka, "notify", lambda policy, msg: sent.append(msg))
    pool = await _pool(tmp_path, _acc(label="primary"))
    driver = _Driver(UpstreamAuthError("session rejected by upstream (HTTP 401)"))
    await ka.check_once(pool, driver, POLICY)
    assert pool.get("a").status == AccountStatus.INVALID
    assert len(sent) == 1 and "primary" in sent[0]
    await ka.check_once(pool, driver, POLICY)  # invalid accounts are skipped
    assert driver.calls == 1 and len(sent) == 1


async def test_failed_renewal_near_expiry_notifies(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(ka, "notify", lambda policy, msg: sent.append(msg))
    now = time.time()
    pool = await _pool(tmp_path, _acc(cookie_expires={"hatch_vml": int(now + 20 * H)}))
    await ka.check_once(pool, _Driver(UpstreamError("HTTP 503")), POLICY)
    assert sent == []  # 20 h left: keep trying quietly
    acc = pool.get("a")
    acc.cookie_expires["hatch_vml"] = int(now + 5 * H)
    await pool.upsert(acc)
    await ka.check_once(pool, _Driver(UpstreamError("HTTP 503")), POLICY)
    await ka.check_once(pool, _Driver(UpstreamError("HTTP 503")), POLICY)
    assert len(sent) == 1 and "HTTP 503" in sent[0]


def test_readyz_reports_session_expiry(client, admin):
    exp = int(time.time() + 40 * H)
    client.post("/admin/accounts", headers=admin,
                json={"label": "a1", "cookies": {"hatch_sess": "x"}, "cookie_expires": {"hatch_vml": exp}})
    body = client.get("/readyz").json()["accounts"]
    assert body["session_expires_at"] == exp
    assert body["keepalive"] is True
