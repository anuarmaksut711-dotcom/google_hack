"""Windows offline evidence: real ASGI handlers, explicit local identity/store.

No auth SDK/Google data/network; transactional fakes establish repository logic,
not deployed Firestore isolation. UI script is executed in a Node DOM harness.
"""

import asyncio
import base64
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import warnings

import httpx
import pytest

from campuspulse_recovery.domain.ports import PortError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.voice import device_auth as module
from campuspulse_recovery.voice.device_auth import (
    DeviceAuth, MAX_PROOF_ATTEMPTS, Pairing, VoiceCredential, proof_challenge,
)
from campuspulse_recovery.web import voice_pairing
from campuspulse_recovery.web.auth import CSRF_COOKIE, SESSION_COOKIE, WebAuth, digest
from campuspulse_recovery.web.voice_pairing import (
    API_PATH, PAGE_CSP, PAGE_PATH, PAGE_SCRIPT, PairingPageHeaders, create_pairing_router,
)
from tests.storage.test_plans import FakeDocuments, NOW, run
from tests.web.test_approval import ORIGIN, ScheduledDocuments, Setup, login

VERIFIER = "v" * 64  # explicit synthetic client proof


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network denied in W03 local tests")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


class PairSetup(Setup):
    def __init__(self, documents=None):
        super().__init__(documents)
        self.auth = WebAuth(self.documents, self.policy, verifier=self.verifier,
            origin=ORIGIN, environment="local", clock=self.clock)
        self.devices = DeviceAuth(self.auth)

    def paired_app(self):
        app = super().app()
        app.include_router(create_pairing_router(device_auth=self.devices))
        app.add_middleware(PairingPageHeaders)
        return app


def clients(h):
    app = h.paired_app()
    return (h.client(app), h.client(app))


async def start(native, name="Учебный Windows"):
    response = await native.post(API_PATH + "/start", json={"challenge": proof_challenge(VERIFIER),
        "client_kind": "windows", "device_name": name})
    assert response.status_code == 200, response.text
    return response.json()


async def approved(browser, headers, native):
    offer = await start(native)
    response = await browser.post(API_PATH + "/preview", headers=headers, json={"code": offer["pairing_code"]})
    assert response.status_code == 200, response.text
    response = await browser.post(API_PATH + "/decision", headers=headers,
        json={"code": offer["pairing_code"], "decision": "approve"})
    assert response.status_code == 200, response.text
    return offer


async def exchange(native, offer, verifier=VERIFIER):
    return await native.post(API_PATH + "/exchange", json={"grant": offer["grant"], "verifier": verifier})


def pair_record(h, offer):
    return _decode(h.documents.data["cp_voice_pairings/" + digest(offer["grant"])], "voice_pairing", Pairing)


def test_current_sources_and_s256_known_vector():
    root = Path(__file__).resolve().parents[2]
    for mod, relative in ((module, "voice/device_auth.py"), (voice_pairing, "web/voice_pairing.py")):
        expected = root / "src/campuspulse_recovery" / relative
        print("CURRENT_SOURCE_MODULE =", Path(mod.__file__).resolve())
        assert Path(mod.__file__).resolve() == expected
    for name, relative in (("web.auth", "web/auth.py"), ("web.app", "web/app.py"),
            ("web.approval", "web/approval.py"), ("security.context", "security/context.py"),
            ("storage.plans", "storage/plans.py")):
        actual = Path(sys.modules["campuspulse_recovery." + name].__file__).resolve()
        print("CURRENT_REGRESSION_SOURCE_MODULE =", actual)
        assert actual == root / "src/campuspulse_recovery" / relative
    # Independent RFC 7636 Appendix B vector, no external calls.
    assert proof_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pending_browser_approval_exchange_scope_and_hash_only_storage(caplog):
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await start(native)
            assert set(offer) == {"pairing_code", "grant", "expires_at", "approval_path"}
            assert (await exchange(native, offer)).status_code == 403
            # Cannot skip the browser's device review, even with a valid session.
            action = {"code": offer["pairing_code"], "decision": "approve"}
            assert (await browser.post(API_PATH + "/decision", headers=headers, json=action)).status_code == 403
            preview = await browser.post(API_PATH + "/preview", headers=headers, json={"code": offer["pairing_code"]})
            assert preview.json()["device_name"] == "Учебный Windows"
            assert (await browser.post(API_PATH + "/decision", headers=headers, json=action)).status_code == 200
            result = await exchange(native, offer)
            assert result.status_code == 200, result.text
            token = result.json()["credential"]
            access = await h.devices.authenticate(token)
            assert access.scope == "voice:read"
            for tool in ("read_assignments", "read_calendar", "solve_repair"):
                access.require_tool(tool)
            for tool in ("apply_plan", "verify_plan", "arbitrary"):
                with pytest.raises(AccessDenied, match="^voice_access_denied$"):
                    access.require_tool(tool)
            with pytest.raises(AccessDenied):
                with h.auth.request_scope(access):
                    pass
            # A voice bearer cannot become browser consent.
            assert (await native.post("/api/approval", headers={"Authorization": "Bearer " + token}, json={})).status_code == 403
            assert not any(p.startswith("cp_approvals/") for p in h.documents.data)
            assert (await exchange(native, offer)).status_code == 403
            storage = repr(h.documents.data)
            for secret in (offer["grant"], offer["pairing_code"], VERIFIER, token,
                           browser.cookies.get(SESSION_COOKIE), browser.cookies.get(CSRF_COOKIE)):
                assert secret not in storage and secret not in caplog.text
            assert token not in repr(access)
            assert result.headers["cache-control"] == "no-store"
    run(scenario())


@pytest.mark.parametrize("case", ["anonymous", "csrf_missing", "csrf_wrong", "origin_missing", "origin_foreign",
    "host_foreign", "fetch_cross_site", "duplicate_session", "duplicate_csrf", "spoofed_identity"])
def test_browser_guards_are_preserved(case):
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await start(native)
            if case in ("anonymous", "spoofed_identity"):
                browser.cookies.clear()
            if case == "spoofed_identity":
                headers.update({"X-User": "student-1", "X-Session": "session-1", "X-Run": "run-1"})
            if case == "csrf_missing": headers.pop("X-CSRF-Token")
            if case == "csrf_wrong": headers["X-CSRF-Token"] = "wrong"
            if case == "origin_missing": headers.pop("Origin")
            if case == "origin_foreign": headers["Origin"] = "https://evil.test"
            if case == "host_foreign": headers["Host"] = "evil.test"
            if case == "fetch_cross_site": headers["Sec-Fetch-Site"] = "cross-site"
            if case.startswith("duplicate_"):
                name = SESSION_COOKIE if case == "duplicate_session" else CSRF_COOKIE
                headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in browser.cookies.items()] + [f"{name}={browser.cookies.get(name)}"])
            for endpoint, payload in (("preview", {"code": offer["pairing_code"]}),
                    ("decision", {"code": offer["pairing_code"], "decision": "approve"}),
                    ("revoke", {"code": offer["pairing_code"]})):
                response = await browser.post(API_PATH + "/" + endpoint, headers=headers, json=payload)
                assert response.status_code == 403
                assert response.json() == {"error": "voice_access_denied"}
            assert pair_record(h, offer).parent_hash is None
    run(scenario())


def test_distinct_sessions_cannot_share_grants_and_denial_is_terminal():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as a, clients(h)[1] as native, h.client(h.paired_app()) as b:
            ah, bh = await login(a), await login(b)
            offer = await start(native)
            code = {"code": offer["pairing_code"]}
            assert (await a.post(API_PATH + "/preview", headers=ah, json=code)).status_code == 200
            assert (await b.post(API_PATH + "/preview", headers=bh, json=code)).status_code == 403
            for name, headers in ((b, bh),):
                assert (await name.post(API_PATH + "/decision", headers=headers,
                    json={**code, "decision": "approve"})).status_code == 403
                assert (await name.post(API_PATH + "/revoke", headers=headers, json=code)).status_code == 403
            assert (await a.post(API_PATH + "/decision", headers=ah, json={**code, "decision": "deny"})).status_code == 200
            assert (await exchange(native, offer)).status_code == 403
            assert (await a.post(API_PATH + "/decision", headers=ah, json={**code, "decision": "approve"})).status_code == 403
    run(scenario())


@pytest.mark.parametrize("proof", ["w" * 64, "short", "λ" * 64, "v" * 129])
def test_bad_proof_attempts_commit_and_lock_grant(proof):
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await approved(browser, headers, native)
            for attempt in range(MAX_PROOF_ATTEMPTS):
                assert (await exchange(native, offer, proof)).status_code == 403
                assert pair_record(h, offer).attempts == attempt + 1
            assert (await exchange(native, offer)).status_code == 403
            assert pair_record(h, offer).state == "revoked"
    run(scenario())


@pytest.mark.parametrize("kind", ["pending", "approved", "credential", "parent", "config", "parent_binding", "parent_owner"])
def test_expiry_and_current_mapping_checked(kind):
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await start(native) if kind == "pending" else await approved(browser, headers, native)
            if kind in ("pending", "approved"):
                h.clock.value += timedelta(minutes=5)
                assert (await exchange(native, offer)).status_code == 403
                return
            token = (await exchange(native, offer)).json()["credential"]
            if kind == "credential": h.clock.value += timedelta(minutes=20)
            if kind == "parent": h.clock.value += timedelta(hours=1)
            if kind == "config":
                changed = h.config.model_copy(update={"student": h.config.student.model_copy(update={"spreadsheet_id": "sheet-2"})})
                h.auth.policy = ResourcePolicy(changed, environment="local")
                h.devices = DeviceAuth(h.auth)
            if kind.startswith("parent_"):
                path = "cp_web_sessions/" + digest(browser.cookies.get(SESSION_COOKIE))
                from campuspulse_recovery.web.auth import WebSession
                parent = _decode(h.documents.data[path], "web_session", WebSession)
                binding = parent.binding.model_copy(update={"session_id": "foreign"} if kind == "parent_binding" else {"owner_id": "foreign"})
                h.documents.data[path] = _encode("web_session", parent.model_copy(update={"binding": binding}))
            with pytest.raises(AccessDenied, match="^voice_access_denied$"):
                await h.devices.authenticate(token)
    run(scenario())


@pytest.mark.parametrize("action", ["signout", "revoke"])
def test_revocation_and_reload(action):
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await approved(browser, headers, native)
            token = (await exchange(native, offer)).json()["credential"]
            fresh = DeviceAuth(h.auth)
            assert (await fresh.authenticate(token)).device_id
            if action == "signout":
                response = await browser.post("/auth/signout", headers=headers)
            else:
                response = await browser.post(API_PATH + "/revoke", headers=headers, json={"code": offer["pairing_code"]})
            assert response.status_code == 200
            with pytest.raises(AccessDenied): await fresh.authenticate(token)
            assert (await exchange(native, offer)).status_code == 403
    run(scenario())


def test_two_instances_concurrent_redemption_issues_only_one_credential():
    async def scenario():
        h = PairSetup()
        second_auth = WebAuth(h.documents, h.policy, verifier=h.verifier,
            origin=ORIGIN, environment="local", clock=h.clock)
        second = DeviceAuth(second_auth)
        async with clients(h)[0] as browser, clients(h)[1] as native:
            offer = await approved(browser, await login(browser), native)
            result = await asyncio.gather(h.devices.exchange(grant=offer["grant"], verifier=VERIFIER),
                second.exchange(grant=offer["grant"], verifier=VERIFIER), return_exceptions=True)
            assert sum(isinstance(v, AccessDenied) for v in result) == 1
            assert sum(p.startswith("cp_voice_credentials/") for p in h.documents.data) == 1
            issued = next(v for v in result if not isinstance(v, Exception))
            assert (await second.authenticate(issued.credential)).scope == "voice:read"
    run(scenario())


def test_two_browser_sessions_concurrent_review_pins_only_one_parent():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as a, clients(h)[1] as native, h.client(h.paired_app()) as b:
            ah, bh = await login(a), await login(b)
            offer = await start(native)
            result = await asyncio.gather(a.post(API_PATH + "/preview", headers=ah, json={"code": offer["pairing_code"]}),
                b.post(API_PATH + "/preview", headers=bh, json={"code": offer["pairing_code"]}))
            assert sorted(r.status_code for r in result) == [200, 403]
            winner = a if result[0].status_code == 200 else b
            assert pair_record(h, offer).parent_hash == digest(winner.cookies.get(SESSION_COOKIE))
    run(scenario())


@pytest.mark.parametrize("target", ["browser_pairing", "check_voice"])
@pytest.mark.parametrize("phase", ["entry", "retry"])
def test_approval_and_credential_checks_recheck_signout_on_transaction_retry(target, phase):
    async def scenario():
        docs = ScheduledDocuments()
        h = PairSetup(docs)
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await approved(browser, headers, native) if target == "check_voice" else await start(native)
            if target == "check_voice":
                token = (await exchange(native, offer)).json()["credential"]
            else:
                await browser.post(API_PATH + "/preview", headers=headers, json={"code": offer["pairing_code"]})
            docs.arm(target, phase)
            task = asyncio.create_task(h.devices.authenticate(token) if target == "check_voice" else browser.post(
                API_PATH + "/decision", headers=headers, json={"code": offer["pairing_code"], "decision": "approve"}))
            await docs.started.wait()
            assert (await browser.post("/auth/signout", headers=headers)).status_code == 200
            docs.resume.set()
            if target == "check_voice":
                with pytest.raises(AccessDenied): await task
            else:
                assert (await task).status_code == 403
                assert pair_record(h, offer).state == "pending"
    run(scenario())


def test_callback_expiry_is_sampled_after_final_read():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            offer = await approved(browser, await login(browser), native)
            original = h.documents.transact
            from types import SimpleNamespace
            async def transact(callback):
                if callback.__name__ != "redeem_pairing": return await original(callback)
                async def invoke(tx):
                    async def get(path):
                        value = await tx.get(path)
                        if path.startswith("cp_web_sessions/"): h.clock.value = NOW + timedelta(minutes=5)
                        return value
                    return await callback(SimpleNamespace(get=get, create=tx.create, set=tx.set))
                return await original(invoke)
            h.documents.transact = transact
            assert (await exchange(native, offer)).status_code == 403
            assert not any(p.startswith("cp_voice_credentials/") for p in h.documents.data)
    run(scenario())


@pytest.mark.parametrize("phase", ["entry", "retry"])
@pytest.mark.parametrize("invalidator", ["signout", "revoke", "expiry"])
def test_redemption_rechecks_parent_after_wait_or_transaction_retry(phase, invalidator):
    async def scenario():
        docs = ScheduledDocuments()
        h = PairSetup(docs)
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await approved(browser, headers, native)
            docs.arm("redeem_pairing", phase)
            task = asyncio.create_task(h.devices.exchange(grant=offer["grant"], verifier=VERIFIER))
            await docs.started.wait()
            assert not any(p.startswith("cp_voice_credentials/") for p in docs.data)
            if invalidator == "signout": await browser.post("/auth/signout", headers=headers)
            if invalidator == "revoke": await browser.post(API_PATH + "/revoke", headers=headers, json={"code": offer["pairing_code"]})
            if invalidator == "expiry": h.clock.value += timedelta(hours=1)
            docs.resume.set()
            with pytest.raises(AccessDenied): await task
            assert not any(p.startswith("cp_voice_credentials/") for p in docs.data)
    run(scenario())


def test_unknown_commit_reconciles_same_candidate_and_failure_does_not_reissue():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            offer = await approved(browser, await login(browser), native)
            original = h.documents.transact
            async def ambiguous(callback):
                result = await original(callback)
                if callback.__name__ == "redeem_pairing": raise PortError("SECRET provider details")
                return result
            h.documents.transact = ambiguous
            result = await exchange(native, offer)
            assert result.status_code == 200
            token = result.json()["credential"]
            assert (await h.devices.authenticate(token)).device_id
            assert (await exchange(native, offer)).status_code == 503
            assert sum(p.startswith("cp_voice_credentials/") for p in h.documents.data) == 1
    run(scenario())


def test_unknown_uncommitted_outcome_fails_safely():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            offer = await approved(browser, await login(browser), native)
            original = h.documents.transact
            async def failed(callback):
                if callback.__name__ == "redeem_pairing": raise PortError("SECRET provider details")
                return await original(callback)
            h.documents.transact = failed
            response = await exchange(native, offer)
            assert response.status_code == 503 and response.json() == {"error": "voice_unavailable"}
            assert not any(p.startswith("cp_voice_credentials/") for p in h.documents.data)
    run(scenario())


def test_creation_lookup_and_unknown_grant_rate_bounds_survive_instances():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            for _ in range(20): await start(native)
            response = await native.post(API_PATH + "/start", json={"challenge": proof_challenge(VERIFIER), "client_kind": "iphone", "device_name": "iPhone"})
            assert response.status_code == 403
            for _ in range(20):
                assert (await browser.post(API_PATH + "/preview", headers=headers, json={"code": "A" * 12})).status_code == 403
                assert (await browser.post(API_PATH + "/revoke", headers=headers, json={"code": "A" * 12})).status_code == 403
            # A second service shares the durable bounds; no process-local counter.
            second = DeviceAuth(h.auth)
            for _ in range(121):
                with pytest.raises(AccessDenied): await second.exchange(grant="a" * 43, verifier=VERIFIER)
            from campuspulse_recovery.voice.device_auth import Budget
            path = second._budget_path("exchange")
            assert _decode(h.documents.data[path], "voice_budget", Budget).count == 120
            parent_hash = digest(browser.cookies.get(SESSION_COOKIE))
            for kind in ("browser", "revoke"):
                assert _decode(h.documents.data[second._budget_path(kind, parent_hash)], "voice_budget", Budget).count == 20
            h.clock.value += timedelta(minutes=5)
            assert (await start(native))["grant"]
    run(scenario())


@pytest.mark.parametrize("extra", [{"approved": True}, {"owner_id": "foreign"}, {"calendar_id": "primary"},
    {"session_id": "foreign"}, {"config_revision": "foreign"}])
def test_client_cannot_set_identity_approval_resources(extra):
    async def scenario():
        h = PairSetup()
        async with clients(h)[1] as native:
            response = await native.post(API_PATH + "/start", json={"challenge": proof_challenge(VERIFIER),
                "client_kind": "windows", "device_name": "Windows", **extra})
            assert response.status_code == 403
            assert not h.documents.data
    run(scenario())


@pytest.mark.parametrize("case", ["http", "host", "origin", "cookie", "query", "oversize", "invalid_json"])
def test_native_transport_bounds_and_safe_errors(case):
    async def scenario():
        h = PairSetup()
        async with clients(h)[1] as native:
            path = API_PATH + "/start"
            headers = {}
            payload = {"challenge": proof_challenge(VERIFIER), "client_kind": "windows", "device_name": "Windows"}
            if case == "http": path = "http://campus.test" + path
            if case == "host": headers["Host"] = "evil.test"
            if case == "origin": headers["Origin"] = ORIGIN
            if case == "cookie": headers["Cookie"] = "x=y"
            if case == "query": path += "?token=SECRET"
            if case == "oversize": payload["device_name"] = "SECRET" * 1000
            response = (await native.post(path, headers={**headers, "Content-Type": "application/json"}, content="SECRET{{")
                if case == "invalid_json" else await native.post(path, headers=headers, json=payload))
            assert response.status_code == 403
            assert "SECRET" not in response.text
            assert not h.documents.data
    run(scenario())


def test_page_csp_exact_script_and_escaped_untrusted_display():
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            assert (await browser.get(PAGE_PATH)).status_code == 403
            headers = await login(browser)
            page = await browser.get(PAGE_PATH)
            assert page.status_code == 200
            script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
            computed = base64.b64encode(sha256(script.encode()).digest()).decode()
            assert "'sha256-" + computed + "'" in page.headers["content-security-policy"] == PAGE_CSP
            assert "connect-src 'self'" in PAGE_CSP and "form-action 'none'" in PAGE_CSP
            assert "unsafe-inline" not in PAGE_CSP and "onclick=" not in page.text
            assert "innerHTML" not in script and "textContent" in script
            evil = '</script><img src=x onerror="steal()">'
            offer = await start(native, evil)
            preview = await browser.post(API_PATH + "/preview", headers=headers, json={"code": offer["pairing_code"]})
            assert preview.json()["device_name"] == evil
            assert evil not in page.text
            assert (await browser.get("/auth/session")).headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
            assert browser.cookies.get(SESSION_COOKIE) not in page.text
            assert browser.cookies.get(CSRF_COOKIE) not in page.text
    run(scenario())


@pytest.mark.parametrize("choice", ["approve", "deny"])
def test_ui_script_clicks_submit_actual_asgi_approval_with_csrf(choice):
    """Execute shipped JS, replay its exact fetches through actual ASGI routes.

    DOM/fetch are fakes, no browser rendering or CSP engine is claimed. The
    separate CSP test hashes exactly the script bytes delivered by the router.
    """
    node = shutil.which("node")
    assert node, "Node is required for W03 user-action execution evidence"
    harness = r"""
const vm = require('node:vm');
let input = '';
process.stdin.on('data', d => input += d);
process.stdin.on('end', async () => {
 const cfg = JSON.parse(input), elements = {}, requests = [];
 for (const id of ['code','notice','device','approve','deny','revoke','lookup'])
   elements[id] = {value: cfg.code, disabled: false, textContent: '', events: {}, addEventListener(k, fn) {this.events[k] = fn;}};
 const context = {document: {cookie: '__Host-cp-csrf=' + cfg.csrf, getElementById: id => elements[id]},
   fetch: async (path, options) => {requests.push({path, ...options}); return {ok: true, json: async () => ({device_name: cfg.name, client_kind: 'windows'})};}};
 vm.runInNewContext(cfg.script, context);
 await elements.lookup.events.submit({preventDefault() {}});
 if (elements.approve.disabled) throw Error('approval stayed disabled');
 await elements[cfg.choice].events.click();
 process.stdout.write(JSON.stringify({requests, display: elements.device.textContent, notice: elements.notice.textContent}));
});
"""
    async def scenario():
        h = PairSetup()
        async with clients(h)[0] as browser, clients(h)[1] as native:
            headers = await login(browser)
            offer = await start(native)
            result = subprocess.run([node, "-e", harness], input=json.dumps({"code": offer["pairing_code"],
                "csrf": browser.cookies.get(CSRF_COOKIE), "name": "Учебный Windows", "script": PAGE_SCRIPT, "choice": choice}),
                text=True, capture_output=True, timeout=10, encoding="utf-8", check=True)
            ui = json.loads(result.stdout)
            assert len(ui["requests"]) == 2
            assert offer["pairing_code"] in ui["display"]
            for call in ui["requests"]:
                assert call["method"] == "POST" and call["credentials"] == "same-origin"
                assert call["headers"]["X-CSRF-Token"] == headers["X-CSRF-Token"]
                response = await browser.post(call["path"], headers={**call["headers"], "Origin": ORIGIN}, content=call["body"])
                assert response.status_code == 200, response.text
            assert pair_record(h, offer).state == ("approved" if choice == "approve" else "denied")
            assert (await exchange(native, offer)).status_code == (200 if choice == "approve" else 403)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        run(scenario())
