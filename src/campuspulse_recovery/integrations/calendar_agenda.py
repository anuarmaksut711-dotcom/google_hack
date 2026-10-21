"""Deterministic local dates for Calendar facts supplied to the chat agent."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def agenda_source(result):
    zone = ZoneInfo(result['timezone'])
    now = datetime.fromisoformat(result['read_at']).astimezone(zone)
    today = now.date()
    events = []
    for event in result['events']:
        start = event['start']
        day = (date.fromisoformat(start['date']) if 'date' in start else
               datetime.fromisoformat(start['dateTime']).astimezone(zone).date())
        label = ('сегодня' if day == today else 'завтра' if day == today + timedelta(days=1)
                 else day.isoformat())
        events.append({**{k: event.get(k) for k in ('summary', 'start', 'end', 'htmlLink', 'transparency')},
                       'local_date': day.isoformat(), 'day_label': label})
    return {'status': 'available', 'read_at': result['read_at'], 'timezone': result['timezone'],
        'local_now': now.isoformat(), 'current_date': today.isoformat(),
        'tomorrow_date': (today + timedelta(days=1)).isoformat(), 'horizon': result['horizon'], 'events': events}
