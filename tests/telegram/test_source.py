import asyncio
from datetime import timedelta
import zlib

import httpx

from campuspulse_recovery.cloud.telegram_reminders import key
from tests.lms.test_cloud_calendar import URL
from .test_reminders import ALICE, fixture


def test_source_freezes_version_and_tasks_together_during_another_refresh(monkeypatch):
    async def scenario():
        import campuspulse_recovery.cloud.calendar_feed as module
        docs, bot, clock, service, _, _ = fixture()
        docs.data['cp_moodle_accounts/' + ALICE] = {'owner_id': ALICE, 'url': URL}
        original = module.CloudCalendarFeed
        due = (clock[0] + timedelta(hours=72)).strftime('%Y%m%dT%H%M%SZ')
        ics = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:task\r\nSUMMARY:Original\r\nDTSTART:'
               + due + '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()
        monkeypatch.setattr(module, 'CloudCalendarFeed', lambda **kwargs:
            original(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=ics))))
        get = docs.get
        reads = 0
        async def concurrent_refresh(path):
            nonlocal reads
            if path.startswith('cp_calendar_feeds/') and docs.data.get(path, {}).get('snapshot'):
                reads += 1
                if reads == 2:
                    raw = zlib.decompress(docs.data[path]['snapshot']).replace(b'Original', b'Revised')
                    docs.data[path]['snapshot'] = zlib.compress(raw)
            return await get(path)
        docs.get = concurrent_refresh
        source = await service.load_source(ALICE)
        assert source.tasks[0].title == 'Revised'
        assert source.snapshot_hash == key(docs.data[source.record]['snapshot'].hex())
    asyncio.run(scenario())
