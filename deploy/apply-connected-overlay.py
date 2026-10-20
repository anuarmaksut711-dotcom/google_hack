"""Apply exact contextual source edits, preserving existing serving-image features."""
import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path

root = Path(importlib.util.find_spec('campuspulse_recovery').origin).parent
changes = json.loads(Path('/tmp/calendar-release/changes.json').read_text())
changed = {}
for change in changes:
    path = root / change['path']
    if 'expected_sha256' in change:
        actual = hashlib.sha256(path.read_text(encoding='utf-8-sig').encode()).hexdigest() if path.exists() else None
        if actual != change['expected_sha256']:
            raise RuntimeError('serving_source_changed: ' + change['path'])
    if 'new' in change:
        after = change['new']
    else:
        after = path.read_text(encoding='utf-8-sig')
        for chunk in reversed(change['chunks']):
            old, new = chunk['before'], chunk['after']
            if after.count(old) == 1:
                after = after.replace(old, new, 1)
            elif new in after:
                continue
            else:
                raise RuntimeError('source_overlay_conflict: ' + change['path'] + '\n' + old[:120])
    compile(after, str(path), 'exec')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding='utf-8')
    changed['campuspulse_recovery/' + change['path']] = path

dist = importlib.metadata.distribution('campuspulse-recovery')
record = next(dist.locate_file(p) for p in dist.files if str(p).endswith('.dist-info/RECORD'))
with record.open(newline='', encoding='utf-8') as stream:
    rows = list(csv.reader(stream))
rows = [r for r in rows if r[0] not in changed]
for name, path in changed.items():
    data = path.read_bytes()
    rows.append([name, 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(), str(len(data))])
with record.open('w', newline='', encoding='utf-8') as stream:
    csv.writer(stream).writerows(rows)
print(json.dumps({'status': 'calendar_overlay_applied', 'files': len(changed)}))
