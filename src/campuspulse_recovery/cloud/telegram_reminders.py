"""Opt-in Telegram delivery, durable linking and per-deadline delivery claims.

No credentials or coursework are logged. All Telegram HTTP calls occur outside
Firestore transaction callbacks. Ambiguous sends are never blindly retried.
"""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
import re
import secrets
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import httpx

from ..domain.ports import BindingConflict


def key(*parts):
    return hashlib.sha256('\0'.join(map(str, parts)).encode()).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('aware_timestamp_required')
    return result


def owner_path(owner):
    if not re.fullmatch(r'(?:google-|email-)[0-9a-f]{64}', owner):
        raise BindingConflict('telegram_owner_invalid')
    return 'cp_telegram_settings/' + owner


class TelegramFailure(Exception):
    def __init__(self, kind, retry_after=300):
        self.kind, self.retry_after = kind, retry_after
        super().__init__('telegram_delivery_' + kind)


class TelegramAPI:
    def __init__(self, token, *, transport=None):
        if not re.fullmatch(r'[0-9]{5,16}:[A-Za-z0-9_-]{20,128}', token):
            raise ValueError('telegram_token_invalid')
        self._token, self.transport = token, transport

    async def send(self, chat_id, text):
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=10,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.post('https://api.telegram.org/bot' + self._token + '/sendMessage',
                    json={'chat_id': chat_id, 'text': text[:4000],
                          'link_preview_options': {'is_disabled': True}})
            result = response.json()
        except Exception:
            raise TelegramFailure('unknown') from None
        if result.get('ok') is True and type(result.get('result', {}).get('message_id')) is int:
            return result['result']['message_id']
        code = result.get('error_code', response.status_code)
        if code == 429:
            delay = result.get('parameters', {}).get('retry_after', 300)
            raise TelegramFailure('retry', max(1, min(86400, delay if type(delay) is int else 300)))
        if code == 403:
            raise TelegramFailure('blocked')
        raise TelegramFailure('failed' if code == 400 else 'unknown')


@dataclass(frozen=True)
class SourceTasks:
    binding: str
    record: str
    status_record: str
    snapshot_hash: str
    tasks: tuple


class TelegramReminders:
    def __init__(self, documents, *, bot=None, username=None, webhook_secret=None,
                 timezone='Asia/Qyzylorda', clock=lambda: datetime.now(UTC), source=None):
        if any((bot, username, webhook_secret)) and not all((bot, username, webhook_secret)):
            raise ValueError('telegram_configuration_incomplete')
        if username and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
            raise ValueError('telegram_username_invalid')
        if webhook_secret and not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', webhook_secret):
            raise ValueError('telegram_webhook_secret_invalid')
        self.documents, self.bot, self.username = documents, bot, username
        self._webhook_secret, self.clock = webhook_secret, clock
        self.zone, self.source = ZoneInfo(timezone), source

    @property
    def configured(self):
        return self.bot is not None

    @classmethod
    def from_environment(cls, documents, *, timezone, clock):
        token = os.environ.get('CAMPUSPULSE_TELEGRAM_BOT_TOKEN', '').strip()
        username = os.environ.get('CAMPUSPULSE_TELEGRAM_BOT_USERNAME', '').strip()
        secret = os.environ.get('CAMPUSPULSE_TELEGRAM_WEBHOOK_SECRET', '').strip()
        if not any((token, username, secret)):
            return cls(documents, timezone=timezone, clock=clock)
        return cls(documents, bot=TelegramAPI(token), username=username, webhook_secret=secret,
                   timezone=timezone, clock=clock)

    def verify_webhook(self, supplied):
        return (self.configured and isinstance(supplied, str) and supplied.isascii()
                and secrets.compare_digest(supplied, self._webhook_secret))

    async def status(self, owner):
        saved = await self.documents.get(owner_path(owner)) or {}
        pending = None
        if saved.get('pending'):
            pending = await self.documents.get('cp_telegram_pairings/' + saved['pending'])
        valid = bool(pending and pending.get('owner') == owner
                     and instant(pending['expires_at']) > self.clock()
                     and pending.get('status') in ('pending', 'received'))
        return {'configured': self.configured, 'connected': bool(saved.get('chat_id')),
                'enabled': saved.get('enabled') is True, 'label': saved.get('label', ''),
                'pending': valid, 'confirmation_required': bool(valid and pending.get('status') == 'received'),
                'pending_label': pending.get('label', '') if valid else ''}

    async def connect(self, owner):
        if not self.configured:
            raise BindingConflict('telegram_not_configured')
        path, nonce, now = owner_path(owner), secrets.token_urlsafe(32), self.clock()
        hashed = key(nonce)
        async def issue(tx):
            saved = await tx.get(path) or {'owner': owner, 'enabled': False}
            tx.create('cp_telegram_pairings/' + hashed,
                {'owner': owner, 'created_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat(),
                 'status': 'pending'})
            tx.set(path, {**saved, 'pending': hashed})
        await self.documents.transact(issue)
        return {'url': 'https://t.me/' + self.username + '?start=' + nonce, 'expires_in': 600}

    async def webhook(self, update):
        message = update.get('message', {})
        chat, sender = message.get('chat', {}), message.get('from', {})
        chat_id = chat.get('id')
        if (chat.get('type') != 'private' or type(chat_id) is not int or not 0 < chat_id < 2**53
                or sender.get('id') != chat_id or sender.get('is_bot') is True):
            return
        text = message.get('text', '')
        if text == '/stop':
            async def stop(tx):
                reverse = await tx.get('cp_telegram_chats/' + key(chat_id))
                saved = await tx.get(owner_path(reverse['owner'])) if reverse and reverse.get('owner') else None
                if saved and saved.get('chat_id') == chat_id:
                    tx.set(owner_path(reverse['owner']), {**saved, 'enabled': False})
            await self.documents.transact(stop)
            return
        match = re.fullmatch(r'/start ([A-Za-z0-9_-]{43})', text) if isinstance(text, str) else None
        if not match:
            return
        hashed, now = key(match[1]), self.clock()
        username = sender.get('username', '')
        label = '@' + username if re.fullmatch(r'[A-Za-z0-9_]{5,32}', str(username)) else str(sender.get('first_name', 'Telegram'))[:80]
        async def receive(tx):
            pairing = await tx.get('cp_telegram_pairings/' + hashed)
            saved = await tx.get(owner_path(pairing['owner'])) if pairing else None
            if (not pairing or not saved or saved.get('pending') != hashed
                    or instant(pairing['expires_at']) <= now or pairing.get('status') != 'pending'):
                return
            tx.set('cp_telegram_pairings/' + hashed,
                   {**pairing, 'status': 'received', 'chat_id': chat_id, 'label': label})
        await self.documents.transact(receive)

    async def confirm(self, owner):
        path, now = owner_path(owner), self.clock()
        async def confirm(tx):
            saved = await tx.get(path)
            pairing = await tx.get('cp_telegram_pairings/' + saved['pending']) if saved and saved.get('pending') else None
            reverse = await tx.get('cp_telegram_chats/' + key(pairing['chat_id'])) if pairing and pairing.get('chat_id') else None
            if (not pairing or pairing.get('owner') != owner or pairing.get('status') != 'received'
                    or instant(pairing['expires_at']) <= now):
                raise BindingConflict('telegram_pairing_expired')
            if reverse and reverse.get('owner') not in (None, owner):
                raise BindingConflict('telegram_chat_already_linked')
            previous_chat = saved.get('chat_id')
            old_reverse = await tx.get('cp_telegram_chats/' + key(previous_chat)) if previous_chat and previous_chat != pairing['chat_id'] else None
            tx.set(path, {'owner': owner, 'enabled': True, 'chat_id': pairing['chat_id'],
                          'label': pairing['label'], 'enabled_since': now.isoformat(),
                          'generation': secrets.token_hex(16), 'pending': None})
            tx.set('cp_telegram_chats/' + key(pairing['chat_id']), {'owner': owner})
            if old_reverse and old_reverse.get('owner') == owner:
                tx.set('cp_telegram_chats/' + key(previous_chat), {'owner': None})
            tx.set('cp_telegram_pairings/' + saved['pending'], {**pairing, 'status': 'consumed'})
        await self.documents.transact(confirm)

    async def set_enabled(self, owner, enabled):
        path, now = owner_path(owner), self.clock()
        async def change(tx):
            saved = await tx.get(path)
            if not self.configured or not saved or not saved.get('chat_id'):
                raise BindingConflict('telegram_not_connected')
            if saved.get('enabled') is enabled:
                return
            tx.set(path, {**saved, 'enabled': enabled, 'enabled_since': now.isoformat(),
                          'generation': secrets.token_hex(16)})
        await self.documents.transact(change)

    async def load_source(self, owner):
        if self.source is not None:
            return await self.source(owner)
        from .calendar_feed import CloudCalendarFeed
        account = await self.documents.get('cp_moodle_accounts/' + owner)
        if not account or account.get('owner_id') != owner:
            return None
        user = int(parse_qs(urlsplit(account['url']).query)['userid'][0])
        feed = CloudCalendarFeed(documents=self.documents, url=account['url'], owner_id=owner, user_id=user, clock=self.clock)
        await feed.refresh()
        # Bind tasks and claim validation to the same immutable read, even if
        # another replica refreshes this feed while we are preparing messages.
        saved = await self.documents.get(feed.record)
        statuses = await self.documents.get(feed.status_record)
        async def frozen(path):
            return saved if path == feed.record else statuses
        await feed.load(reader=SimpleNamespace(get=frozen))
        state = feed.state()
        if (state.error or state.stale or state.snapshot is None or
                (state.last_attempt and state.last_success and state.last_attempt > state.last_success)):
            raise ValueError('telegram_source_unavailable')
        return SourceTasks(feed._binding, feed.record, feed.status_record, key(saved['snapshot'].hex()), state.snapshot.tasks)

    async def claim(self, owner, source, task):
        now, path = self.clock(), owner_path(owner)
        reminder_at = task.due_at - timedelta(hours=72)
        if task.local_status == 'done' or not reminder_at <= now < task.due_at:
            return None
        delivery_path = 'cp_telegram_deliveries/' + key(owner, source.binding, task.id, task.due_at.isoformat())
        async def claim(tx):
            saved = await tx.get(path)
            previous = await tx.get(delivery_path)
            account = await tx.get('cp_moodle_accounts/' + owner)
            feed = await tx.get(source.record)
            statuses = await tx.get(source.status_record)
            reverse = await tx.get('cp_telegram_chats/' + key(saved['chat_id'])) if saved and saved.get('chat_id') else None
            if (not saved or saved.get('enabled') is not True or not reverse or reverse.get('owner') != owner
                    or instant(saved['enabled_since']) > reminder_at or not account
                    or account.get('owner_id') != owner or key(account['url']) != source.binding
                    or not feed or feed.get('error') or not feed.get('snapshot')
                    or key(feed['snapshot'].hex()) != source.snapshot_hash
                    or (statuses and task.id in statuses.get('tasks', {}))):
                return None
            if previous:
                if previous['status'] == 'sending' and instant(previous['lease_until']) <= now:
                    tx.set(delivery_path, {**previous, 'status': 'unknown'})
                    return None
                if previous['status'] != 'retry' or instant(previous['next_attempt_at']) > now:
                    return None
            claim_id = secrets.token_hex(16)
            value = {'owner': owner, 'task_id': task.id, 'due_at': task.due_at.isoformat(),
                     'status': 'sending', 'claim_id': claim_id, 'generation': saved['generation'],
                     'lease_until': (now + timedelta(minutes=5)).isoformat(),
                     'attempts': (previous or {}).get('attempts', 0) + 1}
            tx.set(delivery_path, value)
            return delivery_path, value, saved['chat_id']
        return await self.documents.transact(claim)

    async def deliver(self, owner, source, task):
        claimed = await self.claim(owner, source, task)
        if not claimed:
            return 'skipped'
        path, value, chat_id = claimed
        current = await self.documents.get(owner_path(owner))
        if not current or current.get('enabled') is not True or current.get('generation') != value['generation']:
            outcome = {'status': 'cancelled'}
        else:
            due = task.due_at.astimezone(self.zone)
            text = ('Напоминание о дедлайне\n\n' + task.title[:500] + '\n'
                    + task.course_title[:300] + '\nСрок: ' + due.strftime('%d.%m.%Y %H:%M')
                    + ' (' + str(self.zone) + ')\n\nОткрыть задания: ' + self.origin + '/#moodle')
            try:
                message_id = await self.bot.send(chat_id, text)
                outcome = {'status': 'sent', 'message_id': message_id, 'sent_at': self.clock().isoformat()}
            except TelegramFailure as error:
                outcome = {'status': error.kind}
                if error.kind == 'retry':
                    outcome['next_attempt_at'] = (self.clock() + timedelta(seconds=error.retry_after)).isoformat()
        async def finish(tx):
            current = await tx.get(path)
            setting = await tx.get(owner_path(owner))
            if not current or current.get('claim_id') != value['claim_id']:
                return
            tx.set(path, {**current, **outcome})
            if outcome['status'] == 'blocked' and setting and setting.get('generation') == value['generation']:
                tx.set(owner_path(owner), {**setting, 'enabled': False})
        await self.documents.transact(finish)
        return outcome['status']

    async def run_due(self, *, origin):
        self.origin = origin
        counts = {name: 0 for name in ('sent', 'skipped', 'retry', 'failed', 'unknown', 'blocked', 'cancelled', 'source_errors')}
        if not self.configured:
            return {'configured': False, **counts}
        records = await self.documents.scan('cp_telegram_settings')
        semaphore = asyncio.Semaphore(3)
        async def account(saved):
            if saved.get('enabled') is not True:
                return
            async with semaphore:
                try:
                    owner = saved['owner']
                    owner_path(owner)
                    source = await self.load_source(owner)
                    if source is None:
                        return
                    for task in source.tasks:
                        status = await self.deliver(owner, source, task)
                        counts[status] += 1
                except Exception:
                    counts['source_errors'] += 1
        await asyncio.gather(*(account(saved) for saved in records))
        return {'configured': True, **counts}
