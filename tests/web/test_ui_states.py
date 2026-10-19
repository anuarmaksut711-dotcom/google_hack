"""LOCAL synthetic identities/consent, fake documents and remote frames only.

These checks do not prove human approval, Firestore or Calendar execution.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from campuspulse_recovery.application.proposals import ProposalService
from campuspulse_recovery.domain.models import CalendarEvent, VerificationResult
from campuspulse_recovery.observability.events import SafeSummary, observation
from campuspulse_recovery.storage.plans import plan_reference
from campuspulse_recovery.storage.runs import FirestoreRunEventStore, JournalLimits
from campuspulse_recovery.web.agent_client import RemoteFrame
from campuspulse_recovery.web.assets.client import SCRIPT
from campuspulse_recovery.web.assets.styles import STYLES
from campuspulse_recovery.web.runs import RecoveryRuns, RunRegistry, RunView
from campuspulse_recovery.web.views.app import SafeSources, create_ui_app
from campuspulse_recovery.web.views.render import render_panel, render_shell, safe_calendar_link
from tests.storage.test_plans import NOW, run, sources
from tests.web.test_approval import Setup, ORIGIN, action, login


class Fixture(Setup):
    """Private fake fixture; never wired by the production factory."""
    def __init__(self, mode="no_change", documents=None):
        super().__init__(documents)
        self.mode, self.invocations = mode, []
        self.journal = FirestoreRunEventStore(self.documents, self.policy, clock=self.clock,
                                             limits=JournalLimits(page_size=1))

    def factory(self, auth):
        async def check(context, reference):
            context.require_active()  # FAKE freshness; no API read claimed.
        async def resolve(context):
            raise RuntimeError("fake_candidate_not_supported")
        async def stream(context, intent):
            self.invocations.append(intent)
            yield RemoteFrame(run_id=intent.run_id, intent_id=intent.intent_id,
                              kind="terminal", status=self.mode)
        self.service = RecoveryRuns(auth=auth,
            registry=RunRegistry(self.documents, self.policy, clock=self.clock),
            remote=SimpleNamespace(stream=stream),
            proposals=ProposalService(self.snapshots, self.plans, self.policy,
                SimpleNamespace(resolve=resolve), clock=self.clock),
            plans=self.plans, approvals=self.approvals,
            sources=SimpleNamespace(check=check), journal=self.journal)
        return self.service

    def app(self, **changes):
        return create_ui_app(**{**dict(policy=self.policy, documents=self.documents,
            verifier=self.verifier, origin=ORIGIN, environment="local", clock=self.clock,
            runs_factory=self.factory), **changes})


def view(status, plan=None, **changes):
    return RunView(**{**dict(run_id="run_fake", intent_id="intent_fake", status=status,
        settled=True, plan=None if plan is None else plan_reference(plan),
        verification_status=None, verification_at=None, verification_run_id=None,
        verified_success=False, observation_available=True), **changes})


def render(status, plan=None, **changes):
    return render_panel(**{**dict(run=view(status, plan), plan=plan, approval=None,
        current=None if plan is None else plan_reference(plan), sources=SafeSources(),
        timezone="Asia/Qyzylorda", now=NOW, runtime_available=True), **changes})


@pytest.mark.parametrize("status,text", [
    ("proposed", "Предложен план"), ("waiting_approval", "Ожидает согласования"),
    ("applying", "Применяется"), ("partial", "Применён частично"),
    ("unknown", "Результат неизвестен"), ("applied_unverified", "проверка не подтверждена"),
    ("verification_failed", "Проверка выявила расхождения"), ("needs_input", "Нужны данные"),
    ("infeasible", "Недостаточно времени в горизонте поиска"),
    ("insufficient_capacity", "Недостаточно времени в горизонте поиска"),
    ("no_change", "Изменения не предложены"), ("stale", "План устарел"),
])
def test_distinct_observable_states(status, text):
    html = render(status)
    assert text in html
    assert 'data-action="apply"' not in html and 'data-action="approve"' not in html
    assert "Расписание проверено" not in html


def test_verification_word_and_flag_without_actual_evidence_never_promote():
    html = render("verified", run=view("verified", verified_success=True,
        verification_status="verified", verification_at=NOW, verification_run_id="run_fake"))
    assert 'data-status="applied_unverified"' in html
    assert "Расписание проверено" not in html and "Проверено:" not in html


def test_exact_stored_diff_escaping_and_approval_render():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        html = render("proposed", plan, sources=SafeSources(assignments=("<script>alert(1)</script>",), calendar=()))
        assert 'data-status="waiting_approval"' in html
        assert plan.payload_hash in html and 'data-version="1"' in html
        assert "Точные сохранённые операции" in html and "Нет блока" in html
        assert all(op.operation_id in html and op.event_id in html for op in plan.payload.operations)
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>" not in html
        assert 'data-action="approve"' in html and 'data-action="apply"' not in html
        assert "В полученной сводке нет записей" in html
        stale = render("proposed", plan, current=None)
        assert "больше не текущая" in stale and 'data-action="approve"' not in stale
    run(scenario())


@pytest.mark.parametrize("status", ["partial", "unknown", "applied_unverified", "verification_failed", "needs_input", "infeasible"])
def test_non_actionable_run_never_offers_apply_even_with_fake_active_consent(status):
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        async with h.client() as c:
            headers = await login(c)
            await c.post("/api/approval", headers=headers, json=action(plan))
            with h.request() as ctx:
                consent = await h.approvals.get(ctx, plan_reference(plan))
            assert 'data-action="apply"' not in render(status, plan, approval=consent)
    run(scenario())


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "https://calendar.google.com.evil.test/calendar/event",
    "https://user:secret@calendar.google.com/calendar/event", "https://calendar.google.com:444/calendar/event",
    "https://calendar.google.com/calendar/\nevent", "https://evil.test/calendar", "https://www.google.com/search?q=x",
    "https://calendar.google.com\\@evil.test/calendar", "https://calendar.google.com/calendar\x7f"])
def test_unsafe_event_links_rejected(bad):
    assert safe_calendar_link(bad) is None


def test_verified_timestamp_links_require_matching_fresh_backend_evidence():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        _, calendar = sources(h.config)
        op = plan.payload.operations[0]
        event = CalendarEvent(event_id=op.event_id, etag="fake-etag", interval=op.target.interval,
                              event_link="https://calendar.google.com/calendar/event?eid=fake")
        evidence = VerificationResult(plan=plan_reference(plan), run_id=calendar.source.reference.run_id,
            status="verified", attempted_at=NOW, verification_at=NOW,
            calendar_source=calendar.source, observed_events=(event,),
            checked_operations=tuple(op.operation_id for op in plan.payload.operations))
        rv = view("verified", plan, run_id=evidence.run_id, verified_success=True,
            verification_status="verified", verification_at=NOW, verification_run_id=evidence.run_id)
        html = render("verified", plan, run=rv, sources=SafeSources(verification=evidence))
        assert "Расписание проверено" in html and "Проверено:" in html
        assert 'rel="noopener noreferrer"' in html and event.event_link in html
        for changed in (rv.model_copy(update={"verification_run_id": "foreign"}),
                        rv.model_copy(update={"verification_at": NOW + timedelta(seconds=1)})):
            assert "Расписание проверено" not in render("verified", plan, run=changed, sources=SafeSources(verification=evidence))
    run(scenario())


def test_original_signin_t24_and_signout_share_durable_session_and_security():
    async def scenario():
        h = Fixture()
        async with h.client() as c:
            assert (await c.get("/ui/state")).status_code == 403
            headers = await login(c)
            assert c.cookies.get("__Host-cp-session")
            for path in ("/auth/session", "/api/runs/missing"):
                response = await c.get(path)
                assert response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
            assert (await c.post("/api/runs", json={"request_key": "same"})).status_code == 403
            assert (await c.post("/api/runs", headers={**headers,"Origin":"https://evil.test"}, json={"request_key":"same"})).status_code == 403
            result = await c.post("/api/runs", headers=headers, json={"request_key": "same"})
            assert result.status_code == 200, result.text
            rid = result.json()["run_id"]
            cookies = dict(c.cookies)
            assert (await c.get("/ui/state", params={"run_id": rid})).status_code == 200
            async with h.client(h.app()) as reload:
                reload.cookies.update(cookies)
                assert (await reload.get("/api/runs/" + rid)).json() == result.json()
                duplicate = await reload.post("/api/runs", headers=headers, json={"request_key":"same"})
                assert duplicate.json() == result.json() and len(h.invocations) == 1
            assert (await c.post("/auth/signout", headers=headers, json={})).status_code == 200
            c.cookies.update(cookies)
            for path in ("/auth/session", "/ui/state", "/api/runs/"+rid, "/api/runs/"+rid+"/trace"):
                assert (await c.get(path)).status_code == 403
    run(scenario())


def test_view_exact_approval_then_separate_apply_no_get_side_effects():
    async def scenario():
        h = Fixture(mode="partial")
        plan = await h.seed_current()
        params = {"plan_id":plan.payload.plan_id,"version":plan.payload.version}
        async with h.client() as c:
            headers = await login(c)
            for _ in range(2):
                page = await c.get("/ui/state", params=params)
                assert 'data-action="approve"' in page.text
            assert not h.invocations and not any(p.startswith("cp_approvals/") for p in h.documents.data)
            wrong = action(plan, payload_hash="0"*64)
            assert (await c.post("/api/approval", headers=headers, json=wrong)).status_code == 409
            accepted = await c.post("/api/approval", headers=headers, json=action(plan))
            assert accepted.status_code == 200
            assert 'data-action="apply"' in (await c.get("/ui/state", params=params)).text
            assert not h.invocations
            payload = {"plan":plan_reference(plan).model_dump(mode="json"),"request_key":"apply-same"}
            results = await asyncio.gather(*(c.post("/api/runs/continue", headers=headers, json=payload) for _ in range(2)))
            assert all(r.status_code == 200 for r in results)
            assert len(h.invocations) == 1
            rid = results[0].json()["run_id"]
            latest = await c.get("/ui/state", params={"run_id":rid})
            assert "Применён частично" in latest.text and 'data-action="apply"' not in latest.text
            assert "Расписание проверено" not in latest.text
            assert (await c.post("/api/runs/continue",headers=headers,json=payload)).json()["run_id"] == rid
            assert len(h.invocations) == 1
    run(scenario())


def test_trace_bounded_pagination_and_unavailable_health():
    async def scenario():
        h = Fixture()
        async with h.client() as c:
            headers = await login(c)
            result = (await c.post("/api/runs",headers=headers,json={"request_key":"trace"})).json()
            rid = result["run_id"]
            first = (await c.get(f"/api/runs/{rid}/trace?after=0")).json()
            second = (await c.get(f"/api/runs/{rid}/trace?after={first['next']}")).json()
            assert len(first["records"]) == len(second["records"]) == 1
            assert second["next"] > first["next"]
            assert (await c.get(f"/api/runs/{rid}/trace?after=9999")).status_code == 400
            async def failed(*a, **k):
                from campuspulse_recovery.domain.ports import PortError
                raise PortError("private-source-body")
            h.journal.records_after = failed
            unavailable = await c.get(f"/api/runs/{rid}/trace")
            assert unavailable.status_code == 503 and "private-source-body" not in unavailable.text
            assert 'Журнал недоступен или неполон' in render("unknown", run=view("unknown",observation_available=False))
    run(scenario())


def test_projection_revocation_after_await_cannot_return_private_text():
    async def scenario():
        h = Fixture()
        class Projection:
            async def project(self, context, **kwargs):
                await c.post("/auth/signout", headers=headers, json={})
                return SafeSources(assignments=("private-after-revocation",))
        async with h.client(h.app(projection=Projection())) as c:
            headers = await login(c)
            response = await c.get("/ui/state")
            assert response.status_code == 403 and "private-after-revocation" not in response.text
    run(scenario())


def test_projection_is_bounded_and_cloud_fake_identity_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SafeSources(assignments=("x"*501,))
    with pytest.raises(ValidationError):
        SafeSources(calendar=("x",)*101)
    with pytest.raises(ValueError):
        Fixture().app(environment="cloud")


@pytest.mark.parametrize("foreign", [False, True])
def test_ui_projection_source_binds_resource_but_preserves_separate_run_lineage(foreign):
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        _, calendar = sources(h.config)
        if foreign:
            ref = calendar.source.reference.model_copy(update={"resource_id":"foreign-calendar"})
            source = calendar.source.model_copy(update={"reference":ref})
        else:
            source = calendar.source
        evidence = VerificationResult(plan=plan_reference(plan), run_id=source.reference.run_id,
            status="verified", attempted_at=NOW, verification_at=NOW, calendar_source=source,
            checked_operations=tuple(op.operation_id for op in plan.payload.operations))
        class Projection:
            async def project(self, context, **kwargs):
                assert context.trusted.run_id != evidence.run_id
                return SafeSources(verification=evidence)
        async with h.client(h.app(projection=Projection())) as c:
            await login(c)
            response = await c.get("/ui/state")
            assert response.status_code == (403 if foreign else 200)
            assert "Расписание проверено" not in response.text
    run(scenario())


def test_ui_query_rejects_changed_reference_and_duplicate_selectors():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        async with h.client() as c:
            await login(c)
            for query in ("plan_id=x&version=1&version=2", "plan_id=x&version=-1", "owner_id=foreign"):
                assert (await c.get("/ui/state?"+query)).status_code == 400
            old = await h.seed_current(2, plan_reference(plan))
            page = await c.get("/ui/state",params={"plan_id":plan.payload.plan_id,"version":1})
            assert "больше не текущая" in page.text and 'data-action="approve"' not in page.text
    run(scenario())


def test_resources_csp_and_missing_runtime_fail_closed():
    async def scenario():
        h = Fixture()
        async with h.client(h.app(runs_factory=None)) as c:
            shell = await c.get("/")
            assert shell.status_code == 200 and 'lang="ru"' in shell.text
            assert 'script-src \'self\'' in shell.headers["content-security-policy"]
            assert 'unsafe-inline' not in shell.headers["content-security-policy"]
            assert 'role="status"' in shell.text and 'aria-busy="true"' in shell.text
            assert "не означает, что задание выполнено" in shell.text
            for path, expected, mime in (("/assets/campuspulse.css",STYLES,"text/css"),("/assets/campuspulse.js",SCRIPT,"text/javascript")):
                response = await c.get(path)
                assert response.text == expected and mime in response.headers["content-type"]
                assert response.headers["x-content-type-options"] == "nosniff"
            await login(c)
            page = await c.get("/ui/state")
            assert 'data-runtime="false"' in page.text and 'Облачный исполнитель не настроен' in page.text
            assert 'data-action="approve"' not in page.text
            assert (await c.post("/api/runs",json={})).status_code == 404
    run(scenario())


def test_client_uses_only_explicit_mutations_and_safe_trace_projection():
    assert "setInterval" not in SCRIPT and "setTimeout" not in SCRIPT
    assert "innerHTML" not in SCRIPT and "textContent" in SCRIPT
    assert "localStorage.setItem" in SCRIPT and "request_key" in SCRIPT
    assert "if (busy) return" in SCRIPT and "pending-start" in SCRIPT
    assert "refresh().catch" in SCRIPT and "act('apply')" not in SCRIPT
    assert "record.observation" in SCRIPT and "event.operation_id" in SCRIPT
    assert "aria-busy" in SCRIPT
    assert "@media (max-width: 640px)" in STYLES and ":focus-visible" in STYLES


@pytest.mark.parametrize("terminal", ["revoked", "expired", "superseded"])
def test_terminal_consent_http_requires_new_version_without_backend_reactivation(terminal):
    """Synthetic UI actions exercise ORIGINAL consent store semantics."""
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        params = {"plan_id":plan.payload.plan_id,"version":1}
        async with h.client() as c:
            headers = await login(c)
            first = await c.post("/api/approval",headers=headers,json=action(plan))
            assert first.status_code == 200
            if terminal == "revoked":
                assert (await c.post("/api/approval/revoke",headers=headers,
                    json=action(plan,"revoke"))).status_code == 200
            elif terminal == "expired":
                h.clock.value += timedelta(minutes=10)
            else:
                await h.seed_current(2,plan_reference(plan))
            stored = (await c.get("/api/plan",params=params)).json()["approval"]
            assert stored["status"] == terminal
            page = await c.get("/ui/state",params=params)
            assert page.status_code == 200
            assert 'data-status="waiting_approval"' not in page.text
            assert 'data-status="approved"' not in page.text
            assert 'data-action="approve"' not in page.text and 'data-action="apply"' not in page.text
            assert "получить свежий план и новую версию" in page.text
            assert "явно согласуйте новую версию" in page.text
            retry = await c.post("/api/approval",headers=headers,json=action(plan,"new-click"))
            assert retry.status_code == 200  # Original endpoint returns the terminal record.
            after = (await c.get("/api/plan",params=params)).json()["approval"]
            assert after == stored  # HTTP200 cannot renew revoked/expired grant.
            assert not h.invocations
    run(scenario())


@pytest.mark.parametrize("terminal,title", [
    ("revoked","Согласование отозвано"),("expired","Срок согласования истёк"),
    ("superseded","Согласование заменено"),("elapsed_active","Срок согласования истёк")])
def test_terminal_consent_renderer_names_block_and_preserves_exact_reference(terminal,title):
    from campuspulse_recovery.domain.models import Approval
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        consent = Approval(owner_id=plan.payload.owner_id,calendar_id=plan.payload.calendar_id,
            plan=plan_reference(plan),approved_by=h.config.student.allowed_signin_principal,
            approved_at=NOW-timedelta(minutes=10) if terminal=="elapsed_active" else NOW,
            expires_at=NOW if terminal=="elapsed_active" else NOW+timedelta(minutes=10),
            status="active" if terminal=="elapsed_active" else terminal,
            invalidated_at=NOW if terminal in ("revoked","superseded") else None)
        html = render("proposed",plan,approval=consent)
        assert "<h2>"+title+"</h2>" in html and plan.payload_hash in html
        assert 'data-status="waiting_approval"' not in html
        assert 'data-action="approve"' not in html and 'data-action="apply"' not in html
    run(scenario())


@pytest.mark.parametrize("approved", [False, True])
def test_non_current_reference_requires_fresh_plan_even_with_active_consent(approved):
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        async with h.client() as c:
            headers = await login(c)
            if approved:
                await c.post("/api/approval",headers=headers,json=action(plan))
            await h.seed_current(2,plan_reference(plan))
            page = await c.get("/ui/state",params={"plan_id":plan.payload.plan_id,"version":1})
            assert 'data-status="stale"' in page.text
            assert 'data-action="approve"' not in page.text and 'data-action="apply"' not in page.text
            assert "получить свежий план и новую версию" in page.text
            assert not h.invocations
    run(scenario())


@pytest.mark.parametrize("outcome", ["partial","unknown","verification_failed","applied_unverified","verified"])
def test_terminal_consent_preserves_observed_business_outcome(outcome):
    from campuspulse_recovery.domain.models import Approval
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        consent = Approval(owner_id=plan.payload.owner_id,calendar_id=plan.payload.calendar_id,
            plan=plan_reference(plan),approved_by=h.config.student.allowed_signin_principal,
            approved_at=NOW,expires_at=NOW+timedelta(minutes=10),status="revoked",invalidated_at=NOW)
        extra = {}
        if outcome == "verified":
            _, calendar = sources(h.config)
            evidence = VerificationResult(plan=plan_reference(plan),run_id=calendar.source.reference.run_id,
                status="verified",attempted_at=NOW,verification_at=NOW,calendar_source=calendar.source,
                checked_operations=tuple(op.operation_id for op in plan.payload.operations))
            extra = dict(run=view("verified",plan,run_id=evidence.run_id,verified_success=True,
                verification_status="verified",verification_at=NOW,verification_run_id=evidence.run_id),
                sources=SafeSources(verification=evidence))
        html = render(outcome,plan,approval=consent,**extra)
        assert f'data-status="{outcome}"' in html
        assert "получить свежий план и новую версию" in html
        assert 'data-action="approve"' not in html and 'data-action="apply"' not in html
    run(scenario())
