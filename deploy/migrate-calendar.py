"""Copy the current user's calendar and completion marks into their cloud account.

The owner comes from a verified Google CLI identity, never an email or a manually
supplied subject. Secret feed URLs and token contents are never printed.
"""
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
import zlib

from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_oauth2_token
from google.cloud.firestore import AsyncClient

from campuspulse_recovery.cloud.calendar_app import CalendarSettings
from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
from campuspulse_recovery.lms.calendar_feed import CalendarFeed
from campuspulse_recovery.lms.multiuser import account_config
from campuspulse_recovery.storage.plans import FirestoreDocuments


async def main():
    root = Path(__file__).resolve().parents[1]
    settings = CalendarSettings.model_validate_json((root / '.campuspulse-local/calendar-cloud-settings.json').read_text())
    cli = shutil.which('gcloud.cmd') or shutil.which('gcloud')
    token = subprocess.run([cli, 'auth', 'print-identity-token', '--quiet'], capture_output=True, text=True, check=True).stdout.strip()
    claims = verify_oauth2_token(token, Request(), clock_skew_in_seconds=30)
    if not claims.get('sub') or claims.get('email_verified') is not True:
        raise RuntimeError('verified_google_identity_required')
    principal = 'https://accounts.google.com#' + claims['sub']
    owner = account_config(principal, settings.application).student.trusted_user_id
    url = (root / '.campuspulse-local/moodle-calendar-url.txt').read_text().strip()
    user = int(parse_qs(urlsplit(url).query)['userid'][0])
    client = AsyncClient(project=settings.application.project_id, database=settings.application.firestore_database)
    documents = FirestoreDocuments(client, project_id=settings.application.project_id,
                                   database_id=settings.application.firestore_database)
    try:
        local = CalendarFeed(url=url, user_id=user, owner_id=owner,
                             cache_file=root / '.campuspulse-local/moodle-calendar-cache.json')
        feed = CloudCalendarFeed(documents=documents, url=url, user_id=user, owner_id=owner)
        statuses = local._local_statuses()
        async def save(tx):
            previous = await tx.get('cp_moodle_accounts/' + owner)
            existing_statuses = await tx.get(feed.status_record)
            cached = await tx.get(feed.record)
            if previous is not None and previous != {'owner_id': owner, 'url': url}:
                raise ValueError('existing_cloud_connection_differs')
            tx.set('cp_moodle_accounts/' + owner, {'owner_id': owner, 'url': url})
            if existing_statuses is None:
                tx.set(feed.status_record, statuses.model_dump(mode='json'))
            if cached is None and local.snapshot is not None:
                tx.set(feed.record, {'owner_id': owner, 'binding': feed._binding,
                    'last_attempt': None, 'error': None,
                    'snapshot': zlib.compress(local.snapshot.model_dump_json().encode())})
        await documents.transact(save)
        await feed.refresh()
        state = feed.state()
        if state.snapshot is None or state.error:
            raise RuntimeError('cloud_calendar_refresh_failed')
        print(json.dumps({'status': 'migrated', 'tasks': len(state.snapshot.tasks),
            'completed': sum(t.local_status == 'done' for t in state.snapshot.tasks),
            'google_account': claims.get('email'), 'secret_values_logged': False}))
    finally:
        client.close()


if __name__ == '__main__':
    asyncio.run(main())
