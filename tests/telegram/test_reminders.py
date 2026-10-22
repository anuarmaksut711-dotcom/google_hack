import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest

from campuspulse_recovery.cloud.telegram_reminders import (
    SourceTasks, TelegramAPI, TelegramFailure, TelegramReminders, key, owner_path,
)
from campuspulse_recovery.domain.ports import BindingConflict
from tests.storage.test_plans import FakeDocuments

NOW = datetime(2026, 10, 22, 9, tzinfo=UTC)
ALICE, BOB = 'google-' + 'a' * 64, 'google-' + 'b' * 64


class Bot:
    def __init__(self):
        self.calls, self.failures = [], []

    async def send(self, chat_id, text):
        self.calls.append((chat_id, text))
        if self.failures:
            raise self.failures.pop(0)
        return len(self.calls)


def fixture():
    docs, bot, clock = FakeDocuments(), Bot(), [NOW]
    service = TelegramReminders(docs, bot=bot, username='CampusPulseTestBot', webhook_secret='s' * 43,
                                clock=lambda: clock[0])
    service.origin = 'https://campus.test'
    url, snapshot = 'synthetic-private-feed', b'snapshot'
    source = SourceTasks(key(url), 'cp_calendar_feeds/test', 'cp_calendar_statuses/test', key(snapshot.hex()), ())
    docs.data.update({owner_path(ALICE): {'owner': ALICE, 'enabled': True, 'chat_id': 42,
        'enabled_since': (NOW - timedelta(days=2)).isoformat(), 'generation': 'g', 'label': '@alice'},
        'cp_telegram_chats/' + key(42): {'owner': ALICE},
        'cp_moodle_accounts/' + ALICE: {'owner_id': ALICE, 'url': url},
        source.record: {'snapshot': snapshot, 'error': None}})
    task = SimpleNamespace(id='task', title='<Homework>', course_title='Course',
                           due_at=NOW + timedelta(hours=72), local_status='active')
    return docs, bot, clock, service, source, task


@pytest.mark.parametrize('supplied,expected', [('s' * 43, True), ('wrong', False), ('é' * 43, False), (None, False), (123, False)])
def test_webhook_header_rejects_invalid_values_without_bypassing_auth(supplied, expected):
    _, _, _, service, _, _ = fixture()
    assert service.verify_webhook(supplied) is expected


def run(function):
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    from functools import wraps
    return wraps(function)(wrapped)


@run
async def test_72_hours_duplicate_concurrent_and_restart_only_one_send():
    docs, bot, clock, service, source, task = fixture()
    clock[0] -= timedelta(seconds=1)
    assert await service.deliver(ALICE, source, task) == 'skipped'
    clock[0] = NOW
    results = await asyncio.gather(*(service.deliver(ALICE, source, task) for _ in range(8)))
    assert results.count('sent') == 1 and len(bot.calls) == 1
    restarted = TelegramReminders(docs, bot=bot, username=service.username, webhook_secret='s' * 43, clock=lambda: NOW)
    restarted.origin = service.origin
    assert await restarted.deliver(ALICE, source, task) == 'skipped'
    assert len(bot.calls) == 1
    assert '25.10.2026 14:00' in bot.calls[0][1]
    assert '<Homework>' in bot.calls[0][1]  # Plain text; no Telegram parse_mode.


@pytest.mark.parametrize('change', ['done', 'disabled', 'late_enable', 'expired', 'stale', 'feed_changed', 'done_during_scan', 'wrong_chat'])
@run
async def test_no_send_for_ineligible_or_changed_sources(change):
    docs, bot, clock, service, source, task = fixture()
    if change == 'done': task.local_status = 'done'
    if change == 'disabled': docs.data[owner_path(ALICE)]['enabled'] = False
    if change == 'late_enable': docs.data[owner_path(ALICE)]['enabled_since'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'expired': clock[0] = task.due_at
    if change == 'stale': docs.data[source.record]['snapshot'] = b'new-version'
    if change == 'feed_changed': docs.data['cp_moodle_accounts/' + ALICE]['url'] = 'changed'
    if change == 'done_during_scan': docs.data[source.status_record] = {'tasks': {'task': 'done'}}
    if change == 'wrong_chat': docs.data['cp_telegram_chats/' + key(42)]['owner'] = BOB
    assert await service.deliver(ALICE, source, task) == 'skipped'
    assert not bot.calls


@run
async def test_changed_deadline_gets_distinct_reminder_after_reaching_72_hours():
    docs, bot, clock, service, source, task = fixture()
    assert await service.deliver(ALICE, source, task) == 'sent'
    task.due_at += timedelta(days=1)
    assert await service.deliver(ALICE, source, task) == 'skipped'
    clock[0] += timedelta(days=1)
    assert await service.deliver(ALICE, source, task) == 'sent'
    assert len(bot.calls) == 2


@run
async def test_timeout_after_possible_success_is_not_retried():
    docs, bot, clock, service, source, task = fixture()
    bot.failures = [TelegramFailure('unknown')]
    assert await service.deliver(ALICE, source, task) == 'unknown'
    clock[0] += timedelta(minutes=10)
    assert await service.deliver(ALICE, source, task) == 'skipped'
    assert len(bot.calls) == 1


@run
async def test_lost_claim_lease_is_unknown_and_never_resent():
    docs, bot, clock, service, source, task = fixture()
    claimed = await service.claim(ALICE, source, task)
    clock[0] += timedelta(minutes=6)
    assert await service.deliver(ALICE, source, task) == 'skipped'
    assert docs.data[claimed[0]]['status'] == 'unknown'
    assert not bot.calls


@run
async def test_rate_limit_has_durable_backoff_and_one_later_delivery():
    docs, bot, clock, service, source, task = fixture()
    bot.failures = [TelegramFailure('retry', 600)]
    assert await service.deliver(ALICE, source, task) == 'retry'
    assert await service.deliver(ALICE, source, task) == 'skipped'
    clock[0] += timedelta(minutes=10)
    assert await service.deliver(ALICE, source, task) == 'sent'
    assert await service.deliver(ALICE, source, task) == 'skipped'
    assert len(bot.calls) == 2


@run
async def test_blocked_bot_disables_further_notifications():
    docs, bot, clock, service, source, task = fixture()
    bot.failures = [TelegramFailure('blocked')]
    assert await service.deliver(ALICE, source, task) == 'blocked'
    assert docs.data[owner_path(ALICE)]['enabled'] is False


@run
async def test_disable_after_claim_before_http_prevents_send():
    docs, bot, clock, service, source, task = fixture()
    original = service.claim
    async def disable(*args):
        result = await original(*args)
        await service.set_enabled(ALICE, False)
        return result
    service.claim = disable
    assert await service.deliver(ALICE, source, task) == 'cancelled'
    assert not bot.calls


@run
async def test_storage_failure_after_send_does_not_duplicate():
    docs, bot, clock, service, source, task = fixture()
    original = docs.transact
    async def failure(callback):
        if callback.__name__ == 'finish': raise RuntimeError('storage unavailable')
        return await original(callback)
    docs.transact = failure
    with pytest.raises(RuntimeError): await service.deliver(ALICE, source, task)
    docs.transact = original
    clock[0] += timedelta(minutes=6)
    assert await service.deliver(ALICE, source, task) == 'skipped'
    assert len(bot.calls) == 1


def update(nonce, chat_id=123, **extra):
    return {'update_id': 1, 'message': {'chat': {'type': 'private', 'id': chat_id},
            'from': {'id': chat_id, 'username': 'student_123'}, 'text': '/start ' + nonce, **extra}}


@run
async def test_pairing_requires_start_and_authenticated_confirmation_and_is_single_use():
    docs, bot, clock, service, source, task = fixture()
    docs.data.pop(owner_path(ALICE))
    connected = await service.connect(ALICE)
    nonce = parse_qs(urlsplit(connected['url']).query)['start'][0]
    assert 'chat_id' not in connected
    with pytest.raises(BindingConflict): await service.confirm(ALICE)
    await service.webhook(update(nonce))
    status = await service.status(ALICE)
    assert status['confirmation_required'] and status['enabled'] is False
    with pytest.raises(BindingConflict): await service.confirm(BOB)
    await service.confirm(ALICE)
    assert (await service.status(ALICE))['enabled'] is True
    with pytest.raises(BindingConflict): await service.confirm(ALICE)
    await service.webhook(update(nonce, 999))
    assert docs.data[owner_path(ALICE)]['chat_id'] == 123
    assert not bot.calls
    await service.set_enabled(ALICE, False)
    assert (await service.status(ALICE))['enabled'] is False


@pytest.mark.parametrize('reason', ['expired', 'superseded', 'group', 'mismatched_sender'])
@run
async def test_pairing_rejects_expired_replaced_and_nonprivate_updates(reason):
    docs, bot, clock, service, source, task = fixture()
    link = await service.connect(BOB)
    nonce = parse_qs(urlsplit(link['url']).query)['start'][0]
    value = update(nonce)
    if reason == 'expired': clock[0] += timedelta(minutes=11)
    if reason == 'superseded': await service.connect(BOB)
    if reason == 'group': value['message']['chat']['type'] = 'group'
    if reason == 'mismatched_sender': value['message']['from']['id'] = 321
    await service.webhook(value)
    with pytest.raises(BindingConflict): await service.confirm(BOB)


@run
async def test_one_telegram_chat_cannot_be_taken_over_by_another_owner():
    docs, bot, clock, service, source, task = fixture()
    link = await service.connect(BOB)
    nonce = parse_qs(urlsplit(link['url']).query)['start'][0]
    await service.webhook(update(nonce, 42))
    with pytest.raises(BindingConflict): await service.confirm(BOB)
    assert docs.data['cp_telegram_chats/' + key(42)]['owner'] == ALICE


@run
async def test_stop_command_disables_only_its_bound_owner():
    docs, bot, clock, service, source, task = fixture()
    await service.webhook(update('unused', 42, text='/stop'))
    assert docs.data[owner_path(ALICE)]['enabled'] is False
    await service.webhook(update('unused', 999, text='/stop'))
    assert len([p for p in docs.data if p.startswith('cp_telegram_settings/')]) == 1


@run
async def test_no_configuration_no_background_reads_or_messages():
    docs = FakeDocuments()
    service = TelegramReminders(docs)
    result = await service.run_due(origin='https://campus.test')
    assert result['configured'] is False and result['sent'] == 0 and docs.calls == 0
    assert not service.verify_webhook('s' * 43)


@run
async def test_failed_source_does_not_send_and_is_reported():
    docs, bot, clock, service, source, task = fixture()
    async def unavailable(owner): raise RuntimeError('unavailable')
    service.source = unavailable
    result = await service.run_due(origin='https://campus.test')
    assert result['source_errors'] == 1 and result['sent'] == 0 and not bot.calls


@pytest.mark.parametrize('kind', ['success', 'timeout', '429', '403', '500'])
@run
async def test_telegram_http_uses_plain_text_and_classifies_unknown_outcomes(kind):
    token = '123456789:' + 'a' * 35
    def transport(request):
        import json
        data = json.loads(request.content)
        assert data['chat_id'] == 42 and 'parse_mode' not in data
        if kind == 'timeout': raise httpx.ReadTimeout('simulated')
        if kind == 'success': return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})
        return httpx.Response(int(kind), json={'ok': False, 'error_code': int(kind), 'parameters': {'retry_after': 30}})
    api = TelegramAPI(token, transport=httpx.MockTransport(transport))
    if kind == 'success': assert await api.send(42, '<untrusted>') == 7
    else:
        with pytest.raises(TelegramFailure) as caught: await api.send(42, '<untrusted>')
        assert caught.value.kind == {'timeout': 'unknown', '429': 'retry', '403': 'blocked', '500': 'unknown'}[kind]
        assert token not in str(caught.value)
