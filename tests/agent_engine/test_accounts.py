from copy import deepcopy

import pytest
from pydantic import ValidationError

from tests.lms.test_settings import valid_payload, validate


def configured():
    raw = valid_payload()
    raw.pop('moodle')
    raw['calendar_feed_accounts'] = [
        {'principal_id': 'https://accounts.google.com#alice', 'moodle_user_id': 42, 'calendar_id': 'alice@group.calendar.google.com'},
        {'principal_id': 'https://accounts.google.com#bob', 'moodle_user_id': 43, 'calendar_id': 'bob@group.calendar.google.com'}]
    return raw


def test_account_policy_uses_server_bindings_and_cannot_choose_foreign_calendar():
    from campuspulse_recovery.cloud.accounts import account_settings, account_policy
    settings = validate(configured())
    alice = account_settings(settings, 'https://accounts.google.com#alice')
    bob = account_settings(settings, 'https://accounts.google.com#bob')
    assert alice.application.student.calendar_id == 'alice@group.calendar.google.com'
    assert bob.application.student.calendar_id == 'bob@group.calendar.google.com'
    assert alice.application.student.trusted_user_id != bob.application.student.trusted_user_id
    assert account_settings(settings, 'https://accounts.google.com#unknown') is None
    assert account_policy(alice).assignments_resource_id != account_policy(bob).assignments_resource_id


@pytest.mark.parametrize('field', ['principal_id', 'calendar_id', 'moodle_user_id'])
def test_duplicate_account_resources_rejected(field):
    raw = configured()
    raw['calendar_feed_accounts'][1][field] = raw['calendar_feed_accounts'][0][field]
    with pytest.raises(ValidationError): validate(raw)


def test_private_account_selection_authenticates_before_grant_lookup(monkeypatch):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from campuspulse_recovery.cloud import authority
    from campuspulse_recovery.security.context import AccessDenied
    from tests.storage.test_plans import FakeDocuments

    async def scenario():
        from campuspulse_recovery.cloud.accounts import create_account_service
        settings = validate(configured())
        documents = FakeDocuments()
        reads = []
        original = documents.get
        async def observe(path):
            reads.append(path)
            return await original(path)
        documents.get = observe
        async def reject(*args): raise AccessDenied('invalid_service')
        monkeypatch.setattr(authority.ServiceIdentity, 'verify', reject)
        def factory(selected, policy, documents, *, close):
            return FastAPI()
        app = create_account_service(settings, documents, boundary='agent', factory=factory, close=lambda: None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_origin) as browser:
            response = await browser.post('/internal/invoke', headers={'Authorization': 'Bearer invalid',
                'X-CampusPulse-Invocation': 'a' * 43}, json={})
            assert response.status_code == 403
            assert reads == []
    asyncio.run(scenario())


def test_changed_moodle_account_invalidates_old_config_binding():
    from campuspulse_recovery.cloud.accounts import account_settings
    raw = configured()
    previous = account_settings(validate(raw), 'https://accounts.google.com#alice')
    raw['calendar_feed_accounts'][0]['moodle_user_id'] = 142
    changed = account_settings(validate(raw), 'https://accounts.google.com#alice')
    assert previous.application.config_revision != changed.application.config_revision
