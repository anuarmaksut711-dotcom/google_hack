"""Real web/plan repositories with synthetic Engine and Calendar network I/O."""
from copy import deepcopy
from datetime import timedelta

from cryptography.fernet import Fernet
import pytest

from campuspulse_recovery.cloud.connected_calendar import CloudConnectedCalendar
from campuspulse_recovery.domain.chat_plan import ClassSchedule
from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.integrations.connected_plans import WRITE_SCOPE
from campuspulse_recovery.web.calendar_plans import install_plan_routes
from tests.storage.test_plans import run
from tests.web.test_approval import Setup, login
from tests.web.test_managed_study_chat import composition


class CalendarIO:
    def __init__(self):
        self.events, self.creates = {}, []
        self.before_get = None

    async def get(self, binding, event_id):
        if self.before_get:
            callback, self.before_get = self.before_get, None
            await callback()
        return deepcopy(self.events.get(event_id))

    async def create(self, binding, body):
        self.creates.append(deepcopy(body))
        event = {**deepcopy(body), 'etag': 'created-v1',
                 'htmlLink': 'https://calendar.google.com/calendar/event?eid=synthetic'}
        self.events[event['id']] = event
        return event


def weekly(h, **changes):
    return {'owner_id': h.config.student.trusted_user_id, 'schedule': {
        'valid_from': '2026-10-17', 'valid_until': '2026-10-24', 'classes': [], **changes}}


async def save_weekly(h, record):
    async def save(tx):
        tx.set('cp_class_schedules/' + h.config.student.trusted_user_id, record)
    await h.documents.transact(save)


async def calendar(h):
    io = CalendarIO()
    origin = 'https://study-web.run.app'
    cal = CloudConnectedCalendar(h.documents, key=Fernet.generate_key(),
        mcp_origin='https://study-mcp.run.app', project_id=h.config.project_id,
        origin=origin, clock=h.clock,
        client={'web': {'client_id': 'synthetic.apps.googleusercontent.com',
            'client_secret': 'synthetic-secret', 'project_id': h.config.project_id,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [origin + '/auth/google-calendar/callback']}},
        api=lambda *_: {'items': deepcopy(list(io.events.values()))})
    owner = h.config.student.trusted_user_id
    await cal.save(owner, {'owner': owner, 'subject': 'synthetic-subject',
        'calendar_id': 'synthetic-calendar@example.com', 'timezone': 'Asia/Qyzylorda',
        'tokens': {'refresh_token': 'synthetic-refresh', 'scopes': [WRITE_SCOPE]}})
    cal.plans.io = io
    return cal, io


def blocks(h):
    return [{'title': 'Study task', 'start': (h.clock() + timedelta(hours=1)).isoformat(),
             'end': (h.clock() + timedelta(hours=2)).isoformat()}]


async def proposal(h, cal):
    owner = h.config.student.trusted_user_id
    return await cal.plans.propose(owner, 'request-one', blocks(h), await cal.read(owner),
                                   class_timezone=h.config.student.timezone)


def test_managed_study_blocks_become_draft_then_exact_approval_apply_and_verify():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        app, _, engine, projection = composition(h)
        projection.current = ClassSchedule(status='ready', coverage={
            'start': h.clock(), 'end': h.clock() + timedelta(days=7)}, fetched_at=h.clock())
        engine.reply = {'kind': 'plan', 'message': '', 'blocks': blocks(h)}
        class Tools:
            async def read(self, *, session):
                return await cal.read(h.config.student.trusted_user_id)
            async def call(self, action, *, session, plan_id):
                async def live():
                    await cal.grants.session(h.documents, h.config.student.trusted_user_id, session)
                method = cal.plans.apply if action == 'apply_plan' else cal.plans.verify
                return await method(h.config.student.trusted_user_id, plan_id, session_check=live)
        cal.tools = lambda owner: Tools()
        install_plan_routes(app, auth=app.state.web_auth, calendar=cal)
        async with h.client(app) as client:
            headers = await login(client)
            reply = await client.post('/api/chat', headers=headers,
                json={'request_key': 'managed-plan', 'message': 'Plan in my available hour'})
            assert reply.json()['turns'][0]['status'] == 'completed', reply.text
            assert reply.json()['turns'][0]['response'].count('Предложенное расписание') == 1
            items = (await client.get('/api/google-calendar/plans')).json()['items']
            assert len(items) == 1
            plan, state = items[0]['plan'], items[0]['state']
            assert state['status'] == 'draft' and not io.creates
            selection = {'plan_id': plan['plan_id'], 'payload_hash': plan['hash']}
            assert (await client.post('/api/google-calendar/plans/approve', json=selection)).status_code == 403
            assert (await client.post('/api/google-calendar/plans/approve', headers=headers,
                                      json=selection)).json() == {'status': 'approved'}
            result = await client.post('/api/google-calendar/plans/apply', headers=headers, json=selection)
            assert result.json()['status'] == 'verified', result.text
            assert len(io.creates) == 1
            assert (await client.post('/api/google-calendar/plans/apply', headers=headers,
                                      json=selection)).json()['status'] == 'verified'
            assert len(io.creates) == 1
    run(scenario())


@pytest.mark.parametrize('record', [None, 'expired', 'conflict', 'wrong-owner'])
def test_unknown_expired_conflicting_or_foreign_classes_cannot_materialize_calendar_plan(record):
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        value = weekly(h)
        if record == 'expired':
            value['schedule']['valid_until'] = '2026-10-17'
        if record == 'conflict':
            value['schedule']['classes'] = [{'subject': 'Class', 'weekday': 5, 'start': '22:00', 'end': '23:30'}]
        if record == 'wrong-owner':
            value['owner_id'] = 'somebody-else'
        if record:
            await save_weekly(h, value)
        with pytest.raises(BindingConflict, match='calendar_class_schedule'):
            await proposal(h, cal)
        assert not io.creates
        assert await cal.plans.list(h.config.student.trusted_user_id) == []
    run(scenario())


@pytest.mark.parametrize('boundary', ['approve', 'claim', 'before-create', 'final-verify'])
def test_class_basis_change_blocks_consent_or_further_calendar_writes(boundary):
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        owner = h.config.student.trusted_user_id
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        async def changed():
            await save_weekly(h, weekly(h, classes=[
                {'subject': 'Changed basis', 'weekday': 0, 'start': '08:00', 'end': '09:00'}]))
        async def live():
            pass
        if boundary == 'approve':
            await changed()
            with pytest.raises(BindingConflict, match='calendar_class_schedule_changed'):
                await cal.plans.approve(owner, plan['plan_id'], plan['hash'], 'session-one',
                                        source=await cal.read(owner))
        else:
            await cal.plans.approve(owner, plan['plan_id'], plan['hash'], 'session-one',
                                    source=await cal.read(owner))
            if boundary == 'claim':
                await changed()
                with pytest.raises(BindingConflict, match='calendar_class_schedule_changed'):
                    await cal.plans.claim(owner, plan['plan_id'])
            elif boundary == 'before-create':
                io.before_get = changed
                result = await cal.plans.apply(owner, plan['plan_id'], session_check=live)
                assert result['status'] == 'stale'
            else:
                result = await cal.plans.apply(owner, plan['plan_id'], session_check=live)
                assert result['status'] == 'verified'
                await changed()
                result = await cal.plans.verify(owner, plan['plan_id'], session_check=live)
                assert result['status'] != 'verified'
        assert len(io.creates) == (1 if boundary == 'final-verify' else 0)
    run(scenario())


def test_web_dispatch_rejects_edited_classes_before_requesting_remote_write():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        await cal.plans.approve(h.config.student.trusted_user_id,
                                plan['plan_id'], plan['hash'], 'old-session',
                                source=await cal.read(h.config.student.trusted_user_id))
        await save_weekly(h, weekly(h, valid_until='2026-10-25'))
        class Tools:
            async def call(self, *args, **kwargs):
                pytest.fail('Stale classes must block dispatch before MCP')
        cal.tools = lambda owner: Tools()
        app, _, _, _ = composition(h)
        install_plan_routes(app, auth=app.state.web_auth, calendar=cal)
        async with h.client(app) as client:
            headers = await login(client)
            result = await client.post('/api/google-calendar/plans/apply', headers=headers,
                json={'plan_id': plan['plan_id'], 'payload_hash': plan['hash']})
            assert result.status_code == 409, result.text
            assert not io.creates
    run(scenario())


def test_calendar_change_during_reconciliation_get_blocks_final_insert():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        owner = h.config.student.trusted_user_id
        await cal.plans.approve(owner, plan['plan_id'], plan['hash'], 'session-one',
                                source=await cal.read(owner))
        async def add_busy():
            io.events['new-personal-event'] = {'id': 'new-personal-event', 'etag': 'new-v1',
                'summary': 'New commitment', 'start': {'dateTime': blocks(h)[0]['start']},
                'end': {'dateTime': blocks(h)[0]['end']}}
        async def live():
            pass
        io.before_get = add_busy
        result = await cal.plans.apply(owner, plan['plan_id'], session_check=live)
        assert result['status'] == 'stale'
        assert not io.creates
    run(scenario())


def test_approval_rejects_calendar_changed_since_proposal():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        io.events['new-event'] = {'id': 'new-event', 'etag': 'new-v1', 'summary': 'Busy',
            'start': {'dateTime': blocks(h)[0]['start']}, 'end': {'dateTime': blocks(h)[0]['end']}}
        with pytest.raises(BindingConflict, match='calendar_source_changed'):
            await cal.plans.approve(h.config.student.trusted_user_id,
                                    plan['plan_id'], plan['hash'], 'session-one',
                                    source=await cal.read(h.config.student.trusted_user_id))
        _, state = await cal.plans.get(h.config.student.trusted_user_id, plan['plan_id'])
        assert state['status'] == 'draft' and not io.creates
    run(scenario())


def test_unknown_insert_is_reconciled_before_explicit_continue_without_duplicate():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        owner = h.config.student.trusted_user_id
        await cal.plans.approve(owner, plan['plan_id'], plan['hash'], 'session-one',
                                source=await cal.read(owner))
        create = io.create
        async def lose_response(binding, body):
            await create(binding, body)
            raise TimeoutError('Synthetic lost insert response')
        io.create = lose_response
        async def live():
            pass
        first = await cal.plans.apply(owner, plan['plan_id'], session_check=live)
        assert first['status'] == 'unknown' and len(io.creates) == 1
        io.create = create
        assert (await cal.plans.apply(owner, plan['plan_id'], session_check=live))['status'] == 'verified'
        assert len(io.creates) == 1
    run(scenario())


def test_listing_invalidated_classes_does_not_keep_verified_success_or_links():
    async def scenario():
        h = Setup()
        cal, _ = await calendar(h)
        await save_weekly(h, weekly(h))
        plan = await proposal(h, cal)
        owner = h.config.student.trusted_user_id
        await cal.plans.approve(owner, plan['plan_id'], plan['hash'], 'session-one',
                                source=await cal.read(owner))
        async def live():
            pass
        assert (await cal.plans.apply(owner, plan['plan_id'], session_check=live))['status'] == 'verified'
        await save_weekly(h, weekly(h, valid_until='2026-10-25'))
        item, = await cal.plans.list(owner)
        assert item['state']['status'] == 'stale'
        assert not item['state'].get('verified_at') and not item['state'].get('links')
    run(scenario())


def test_weekly_classes_use_student_timezone_when_calendar_timezone_differs():
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        owner = h.config.student.trusted_user_id
        binding = await cal.plans.binding(owner)
        await cal.save(owner, {**binding, 'timezone': 'UTC'})
        await save_weekly(h, weekly(h, classes=[
            {'subject': 'Evening class', 'weekday': 5, 'start': '22:00', 'end': '23:30'}]))
        with pytest.raises(BindingConflict, match='calendar_class_schedule'):
            await proposal(h, cal)
        assert not io.creates
    run(scenario())


@pytest.mark.parametrize('invalid', ['model-approval', 'overlap', 'past'])
def test_invalid_managed_structured_reply_never_reaches_calendar_proposal(invalid):
    async def scenario():
        h = Setup()
        cal, io = await calendar(h)
        await save_weekly(h, weekly(h))
        app, _, engine, projection = composition(h)
        projection.current = ClassSchedule(status='ready', coverage={
            'start': h.clock(), 'end': h.clock() + timedelta(days=7)}, fetched_at=h.clock())
        engine.reply = {'kind': 'plan', 'message': '', 'blocks': blocks(h)}
        if invalid == 'model-approval':
            engine.reply['approved'] = True
        elif invalid == 'overlap':
            engine.reply['blocks'] *= 2
        else:
            engine.reply['blocks'][0]['start'] = (h.clock() - timedelta(minutes=5)).isoformat()
        async def not_called(*args, **kwargs):
            pytest.fail('Only validated structured blocks may reach a proposal')
        app.state.calendar_plan_proposer = not_called
        async with h.client(app) as client:
            headers = await login(client)
            result = await client.post('/api/chat', headers=headers,
                json={'request_key': 'invalid-plan', 'message': 'Plan my studies'})
            turn, = result.json()['turns']
            assert turn['status'] == 'completed'
            assert 'Study task' not in turn['response']
            assert await cal.plans.list(h.config.student.trusted_user_id) == []
            assert not io.creates
    run(scenario())
