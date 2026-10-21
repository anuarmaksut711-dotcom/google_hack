"""User-consented read-only Calendar. Encrypted Firestore state; private MCP reads."""
import asyncio
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from ..integrations.local_calendar import ConnectedCalendar, CalendarConnectionError
from ..storage.plans import _decode
from ..web.auth import WebSession, digest
from ..security.context import AccessDenied
from ..agent.mcp_auth import PlatformIdTokenIssuer, cloud_run_audience
from ..integrations.local_calendar import SCOPES
from ..integrations.connected_plans import ConnectedPlans, WRITE_SCOPE


class EncryptedCalendarDocuments:
    """Only OAuth records use this port. Keys are mounted from Secret Manager."""
    def __init__(self, documents, key):
        self.documents, self.cipher = documents, Fernet(key)

    @staticmethod
    def path(path):
        if not re.fullmatch(r'cp_(?:local_(?:google_calendars|calendar_pending)|calendar_(?:plans|plan_state|plan_index|plan_requests|plan_locks|owned_events))/[A-Za-z0-9_-]{1,160}', path):
            raise ValueError('calendar_document_path_required')
        return path

    def encode(self, path, value):
        # Include the document path in the ciphertext to prevent record substitution.
        return {'ciphertext': self.cipher.encrypt(json.dumps({'path': path, 'value': value}).encode()).decode()}

    def decode(self, path, value):
        if value is None:
            return None
        decoded = json.loads(self.cipher.decrypt(value['ciphertext'].encode()))
        if decoded['path'] != path:
            raise ValueError('calendar_ciphertext_binding_mismatch')
        return decoded['value']

    async def get(self, path):
        return self.decode(self.path(path), await self.documents.get(path))

    async def transact(self, callback):
        store = self
        async def transaction(tx):
            class Transaction:
                async def get(self, path):
                    return store.decode(store.path(path), await tx.get(path))
                def set(self, path, value):
                    tx.set(store.path(path), store.encode(path, value))
                def create(self, path, value):
                    tx.create(store.path(path), store.encode(path, value))
            return await callback(Transaction())
        return await self.documents.transact(transaction)


class CalendarReadGrants:
    def __init__(self, documents, clock=lambda: datetime.now(UTC)):
        self.documents, self.clock = documents, clock

    async def session(self, reader, owner, session_id):
        index = await reader.get('cp_web_session_index/' + digest(session_id))
        if not isinstance(index, dict) or set(index) != {'path'} or not re.fullmatch(
                r'cp_web_sessions/[0-9a-f]{64}', index['path']):
            raise AccessDenied('calendar_session_required')
        value = _decode(await reader.get(index['path']), 'web_session', WebSession)
        if (value.revoked or value.binding.owner_id != owner or value.binding.session_id != session_id
                or not value.authenticated_at <= self.clock() < value.expires_at):
            raise AccessDenied('calendar_session_rejected')
        return value

    async def issue(self, owner, session_id, *, action='read_calendar', plan_id=None):
        if action not in ('read_calendar', 'apply_plan', 'verify_plan') or (action != 'read_calendar' and not plan_id):
            raise AccessDenied('calendar_grant_action')
        reference = secrets.token_urlsafe(32)
        async def save(tx):
            session = await self.session(tx, owner, session_id)
            path = 'cp_calendar_read_grants/' + digest(reference)
            await tx.get(path)
            tx.create(path, {'owner': owner, 'session': session_id, 'consumed': False,
                'action': action, 'plan_id': plan_id,
                'expires': min(session.expires_at, self.clock() + timedelta(seconds=60)).isoformat()})
        await self.documents.transact(save)
        return reference

    async def consume(self, reference, *, action='read_calendar'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', reference):
            raise AccessDenied('calendar_grant_rejected')
        async def claim(tx):
            path = 'cp_calendar_read_grants/' + digest(reference)
            value = await tx.get(path)
            if not value or value.get('action', 'read_calendar') != action or value['consumed'] or self.clock() >= datetime.fromisoformat(value['expires']):
                raise AccessDenied('calendar_grant_rejected')
            await self.session(tx, value['owner'], value['session'])
            tx.set(path, {**value, 'consumed': True})
            return value
        return await self.documents.transact(claim)


class CloudConnectedCalendar(ConnectedCalendar):
    cloud_ready = True
    scopes = (*SCOPES, WRITE_SCOPE)
    # A connected Calendar account is distinct from the website login account.
    # Signed Google subject, nonce, live owner session and PKCE still bind consent.
    # The prelogin flow separately verifies the exact subject before signing in.
    require_matching_login = False

    def window(self, now, timezone):
        local = now.astimezone(ZoneInfo(timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        return start, now.astimezone(UTC) + timedelta(hours=72)

    def __init__(self, documents, *, key, mcp_origin, **kwargs):
        cloud_run_audience(kwargs['origin'])
        self.raw_documents, self.mcp_origin = documents, cloud_run_audience(mcp_origin)
        self.grants = CalendarReadGrants(documents, kwargs.get('clock', lambda: datetime.now(UTC)))
        super().__init__(EncryptedCalendarDocuments(documents, key), **kwargs)
        self.plans = ConnectedPlans(self)

    async def status(self, owner):
        value = await self.documents.get('cp_local_google_calendars/' + owner)
        return {'connected': bool(value and value.get('connected')),
                'can_write': bool(value and value.get('connected') and WRITE_SCOPE in value.get('tokens', {}).get('scopes', []))}

    def tools(self, owner):
        return RemoteCalendarTools(self, owner)


class RemoteCalendarTools:
    def __init__(self, calendar, owner):
        self.calendar, self.owner = calendar, owner

    async def read(self, *, session):
        return await self.call('read_calendar', session=session)

    async def call(self, action, *, session, plan_id=None):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        started = time.monotonic()
        trace = {'tool': action, 'run_id': 'run_' + uuid4().hex, 'call_id': 'call_' + uuid4().hex}
        try:
            async with asyncio.timeout(45):
                reference = await self.calendar.grants.issue(self.owner, session, action=action, plan_id=plan_id)
                issued = await PlatformIdTokenIssuer()(self.calendar.mcp_origin, 5)
                headers = {'Authorization': 'Bearer ' + issued.value}
                async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=40,
                                             follow_redirects=False) as client:
                    async with streamable_http_client(self.calendar.mcp_origin + '/calendar/mcp',
                            http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as channel:
                            await channel.initialize()
                            result = await channel.call_tool(action, {'reference': reference})
                if result.isError:
                    raise CalendarConnectionError('calendar_read_failed')
                data = result.structuredContent
                if data is None and len(result.content) == 1 and result.content[0].type == 'text':
                    data = json.loads(result.content[0].text)
                if not isinstance(data, dict) or len(json.dumps(data).encode()) > 800_000:
                    raise CalendarConnectionError('calendar_read_failed')
                await self.calendar.grants.session(self.calendar.raw_documents, self.owner, session)
                trace.update(status='completed', duration_ms=round((time.monotonic()-started)*1000),
                             result={'event_count': len(data.get('events', [])), 'status': data.get('status')})
                return {**data, 'trace': trace}
        except Exception:
            trace.update(status='failed', duration_ms=round((time.monotonic()-started)*1000))
            return {'error': 'calendar_read_failed' if action == 'read_calendar' else 'calendar_operation_unknown', 'trace': trace}
