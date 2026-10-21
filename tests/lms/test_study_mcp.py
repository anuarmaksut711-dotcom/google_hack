"""Actual MCP protocol and owner-isolated real feed parsing; external I/O fake."""
from contextlib import asynccontextmanager
from datetime import timedelta
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
from campuspulse_recovery.cloud.study_grants import StudyGrants
from campuspulse_recovery.cloud.study_mcp import StudySources, create_study_server, create_study_transport
from campuspulse_recovery.demo.adapters import demo_config
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.auth import WebSession, digest
from tests.lms.test_local_google_calendar import NOW
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login, run_async, url

MCP_ORIGIN = 'https://study-private.example.run.app'


async def setup(tmp_path):
    app = create_multiuser_app(origin=ORIGIN, client_id='client', directory=tmp_path,
        test_verifier=Verifier(), clock=lambda: NOW)
    async with client(app) as alice, client(app) as bob:
        await login(alice, 'alice')
        await login(bob, 'bob')
    docs = app.state.documents
    sessions = {}
    for index in await docs.scan('cp_web_session_index'):
        session = _decode(await docs.get(index['path']), 'web_session', WebSession)
        sessions[session.principal_id.split('#')[1]] = session
    return docs, sessions


def feed_factory(requests, *, failure=False, revoke=None):
    async def fetch(request):
        requests.append(request)
        if revoke is not None:
            await revoke()
        uid = request.url.params['userid']
        raw = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:task\r\n'
            f'SUMMARY:Private homework {uid}\r\nCATEGORIES:Algorithms | Teacher private\r\n'
            'DESCRIPTION:Do not expose private description\r\nDTSTART:20261023T120000Z\r\n'
            'END:VEVENT\r\nEND:VCALENDAR\r\n').encode()
        return httpx.Response(503 if failure else 200, content=raw)
    return lambda **kwargs: CloudCalendarFeed(**kwargs, transport=httpx.MockTransport(fetch))


async def save_account(docs, session, user):
    owner = session.binding.owner_id
    async def save(tx):
        tx.set('cp_moodle_accounts/' + owner, {'owner_id': owner, 'url': url(user)})
    await docs.transact(save)


async def parent(grants, session):
    ref = await grants.issue_invocation(session.binding.owner_id, session.binding.session_id,
        'Составь план', [], 'Asia/Qyzylorda')
    return await grants.consume_invocation(ref)


class Identity:
    async def verify(self, request):
        if request.headers.getlist('authorization') != ['Bearer fake-agent-identity']:
            raise AccessDenied('service_identity_required')


@asynccontextmanager
async def channel(server):
    app = create_study_transport(server, Identity())
    async with server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url=MCP_ORIGIN, headers={'Authorization': 'Bearer fake-agent-identity'}) as http:
            async with streamable_http_client(MCP_ORIGIN + '/study/mcp', http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session, app


@run_async
async def test_protocol_exposes_only_read_tools_reads_real_owner_feed_and_schedule(tmp_path):
    docs, sessions = await setup(tmp_path)
    requests = []
    for name, user in [('alice', 42), ('bob', 43)]:
        await save_account(docs, sessions[name], user)
    grants = StudyGrants(docs, clock=lambda: NOW)
    source_factory = feed_factory(requests)
    server = create_study_server(documents=docs, application=demo_config(),
        mcp_origin=MCP_ORIGIN, clock=lambda: NOW, feed_factory=source_factory)
    async with channel(server) as (mcp, app):
        tools = (await mcp.list_tools()).tools
        assert {tool.name for tool in tools} == {'read_assignments', 'read_schedule'}
        assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
        for name, user in [('alice', 42), ('bob', 43)]:
            invocation = await parent(grants, sessions[name])
            child = await grants.issue_tool(invocation, 'read_assignments')
            result = await mcp.call_tool('read_assignments', {'reference': child})
            assert not result.isError
            data = result.structuredContent
            assert data['status'] == 'available'
            assert data['assignments'][0]['title'] == f'Private homework {user}'
            assert data['assignments'][0]['course_title'] == 'Algorithms'
            assert data['assignments'][0]['submission_status'] == 'unknown'
            assert not any(secret in json.dumps(data) for secret in
                ['authtoken', 'private description', 'Teacher private', child, 'refresh_token'])
            replay = await mcp.call_tool('read_assignments', {'reference': child})
            assert replay.isError
            child = await grants.issue_tool(invocation, 'read_schedule')
            result = await mcp.call_tool('read_schedule', {'reference': child})
            assert not result.isError and result.structuredContent['status'] == 'not_connected'
            assert result.structuredContent['class_schedule']['status'] == 'not_connected'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=MCP_ORIGIN) as http:
            assert (await http.post('/study/mcp', json={})).status_code == 403
    assert len(requests) == 2 and all(request.method == 'GET' for request in requests)


@run_async
async def test_done_tasks_excluded_and_failed_freshness_never_empty_success(tmp_path):
    docs, sessions = await setup(tmp_path)
    session = sessions['alice']
    await save_account(docs, session, 42)
    now = NOW
    requests = []
    source = StudySources(docs, demo_config(), clock=lambda: now, feed_factory=feed_factory(requests))
    value = {'owner': session.binding.owner_id, 'session': session.binding.session_id, 'timezone': 'Asia/Qyzylorda'}
    assert (await source.read_assignments(value))['total'] == 1
    feed = source.feed_factory(documents=docs, url=url(42), owner_id=value['owner'], user_id=42, clock=lambda: now)
    await feed.load()
    await feed.set_local_status('task', True)
    result = await source.read_assignments(value)
    assert result['status'] == 'available' and result['assignments'] == []
    now += timedelta(minutes=31)
    source.feed_factory = feed_factory(requests, failure=True)
    result = await source.read_assignments(value)
    assert result['status'] == 'unavailable' and result['assignments'] == []


@run_async
async def test_changed_owner_binding_rejected_and_live_session_checked_after_fetch(tmp_path):
    docs, sessions = await setup(tmp_path)
    session = sessions['alice']
    await save_account(docs, session, 42)
    async def revoke():
        index = await docs.get('cp_web_session_index/' + digest(session.binding.session_id))
        async def save(tx):
            await tx.get(index['path'])
            tx.set(index['path'], _encode('web_session', session.model_copy(update={'revoked': True})))
        await docs.transact(save)
    grants = StudyGrants(docs, clock=lambda: NOW)
    invocation = await parent(grants, session)
    server = create_study_server(documents=docs, application=demo_config(), mcp_origin=MCP_ORIGIN,
        clock=lambda: NOW, feed_factory=feed_factory([], revoke=revoke))
    async with channel(server) as (mcp, _):
        child = await grants.issue_tool(invocation, 'read_assignments')
        result = await mcp.call_tool('read_assignments', {'reference': child})
        assert result.isError
        assert 'Private homework' not in str(result.content)


@run_async
async def test_saved_class_schedule_is_bound_and_marks_busy_time(tmp_path):
    docs, sessions = await setup(tmp_path)
    session = sessions['alice']
    value = {'owner': session.binding.owner_id, 'session': session.binding.session_id, 'timezone': 'Asia/Qyzylorda'}
    source = StudySources(docs, demo_config(), clock=lambda: NOW)
    record = {'owner_id': value['owner'], 'schedule': {'valid_from': '2026-10-19',
        'valid_until': '2026-10-25', 'classes': [{'subject': 'Algorithms', 'weekday': 1,
            'start': '10:00', 'end': '11:00'}]}}
    async def save(tx):
        tx.set('cp_class_schedules/' + value['owner'], record)
    await docs.transact(save)
    result = await source.read_schedule(value)
    assert result['status'] == 'available' and len(result['class_schedule']['busy']) == 1
    record['owner_id'] = 'foreign'
    await docs.transact(save)
    with pytest.raises(AccessDenied):
        await source.read_schedule(value)


@run_async
async def test_connected_google_calendar_is_read_for_schedule_without_manual_week(tmp_path):
    docs, sessions = await setup(tmp_path)
    session = sessions['alice']
    calls = []
    class Calendar:
        async def status(self, owner):
            assert owner == session.binding.owner_id
            return {'connected': True}
        async def read(self, owner):
            calls.append(owner)
            return {'timezone': 'Asia/Qyzylorda', 'read_at': NOW.isoformat(),
                'horizon': {'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=72)).isoformat()},
                'events': [{'id': 'event', 'summary': 'Operating System Practice',
                    'start': {'dateTime': (NOW + timedelta(hours=1)).isoformat()},
                    'end': {'dateTime': (NOW + timedelta(hours=2)).isoformat()},
                    'description': 'PRIVATE_DESCRIPTION', 'attendees': ['PRIVATE_EMAIL']}]}
    source = StudySources(docs, demo_config(), clock=lambda: NOW, calendar=Calendar())
    result = await source.read_schedule({'owner': session.binding.owner_id,
        'session': session.binding.session_id, 'timezone': 'Asia/Qyzylorda'})
    assert calls == [session.binding.owner_id]
    assert result['status'] == 'available'
    assert result['calendar_event_count'] == 1
    assert any('Operating System Practice' in line for line in result['context'])
    assert any((NOW + timedelta(hours=1)).isoformat() in line for line in result['context'])
    assert 'PRIVATE_' not in json.dumps(result)


@run_async
async def test_connected_calendar_failure_does_not_become_empty_availability(tmp_path):
    docs, sessions = await setup(tmp_path)
    session = sessions['alice']
    class Calendar:
        async def status(self, owner): return {'connected': True}
        async def read(self, owner): raise RuntimeError('private refresh token')
    source = StudySources(docs, demo_config(), clock=lambda: NOW, calendar=Calendar())
    result = await source.read_schedule({'owner': session.binding.owner_id,
        'session': session.binding.session_id, 'timezone': 'Asia/Qyzylorda'})
    assert result['status'] == 'unavailable'
    assert 'private refresh token' not in json.dumps(result)
