"""Prepare a bounded overlay of the current serving images; secrets stay outside build source."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--client', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state = args.state.resolve()
    source = state / 'source'
    source.mkdir(parents=True, exist_ok=True)

    def gcp(*words):
        command = ['gcloud.cmd', *words, '--project='+args.project, '--account='+args.account, '--format=json']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise RuntimeError('cloud_metadata_read_failed: ' + ' '.join(words[:2]))
        return json.loads(result.stdout)

    services = {name: gcp('run', 'services', 'describe', name, '--region='+args.region)
                for name in ('campuspulse', 'campuspulse-mcp')}
    (state / 'rollback.json').write_text(json.dumps(services), encoding='utf-8')
    oauth_client = json.loads(args.client.read_text())
    origin = 'https://campuspulse-403025736340.us-central1.run.app'
    from cryptography.fernet import Fernet
    oauth = {'project_id': args.project, 'database': 'campuspulse-calendar', 'origin': origin,
        'mcp_origin': services['campuspulse-mcp']['status']['url'], 'client': oauth_client,
        'key': Fernet.generate_key().decode(),
        'backend_service_account': services['campuspulse']['spec']['template']['spec']['serviceAccountName']}
    assert oauth_client['web']['project_id'] == args.project
    assert origin + '/auth/google-calendar/callback' in oauth_client['web']['redirect_uris']
    oauth_path = state / 'oauth-secret.json'
    if not oauth_path.exists():
        oauth_path.write_text(json.dumps(oauth), encoding='utf-8')

    files = ['integrations/local_calendar.py', 'lms/multiuser.py', 'web/account.py', 'web/auth.py',
        'web/views/app.py', 'web/views/render.py', 'web/assets/client.py', 'web/chat.py',
        'agent/conversation.py', 'cloud/calendar_app.py', 'web/google_calendar.py',
        'cloud/connected_calendar.py', 'cloud/connected_calendar_mcp.py']
    # Preserve the display brand already published by the user's other changes.
    brand = lambda text: re.sub(r'\bCampusPulse\b|(?i:\bfriday ai\b)', 'Google AI', text)
    changes = []
    for relative in files:
        path = 'src/campuspulse_recovery/' + relative
        after = brand((root / path).read_text(encoding='utf-8-sig')).splitlines(keepends=True)
        previous = subprocess.run(['git', 'show', 'HEAD:'+path], cwd=root, capture_output=True)
        if previous.returncode:
            changes.append({'path': relative, 'new': ''.join(after)})
            continue
        before = brand(previous.stdout.decode('utf-8-sig')).splitlines(keepends=True)
        groups = list(difflib.SequenceMatcher(None, before, after, autojunk=False).get_grouped_opcodes(3))
        chunks = []
        for group in groups:
            i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
            chunks.append({'before': ''.join(before[i1:i2]), 'after': ''.join(after[j1:j2])})
        changes.append({'path': relative, 'chunks': chunks})
    (source / 'changes.json').write_text(json.dumps(changes, ensure_ascii=False), encoding='utf-8')
    shutil.copyfile(root / 'deploy/apply-connected-overlay.py', source / 'apply.py')
    shutil.copytree(state / 'wheels', source / 'wheels', dirs_exist_ok=True)
    hashes = [f'/tmp/calendar-release/wheels/{p.name} --hash=sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}'
              for p in sorted((source / 'wheels').glob('*.whl'))]
    (source / 'oauth.lock').write_text('\n'.join(hashes)+'\n', encoding='utf-8')
    images = {}
    for name, service in services.items():
        active = next(t['revisionName'] for t in service['status']['traffic'] if t.get('percent', 0))
        revision = gcp('run', 'revisions', 'describe', active, '--region='+args.region)
        base = revision['status']['imageDigest']
        images[name] = base
        dockerfile = f'''FROM {base}
USER root
COPY . /tmp/calendar-release/
RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/calendar-release/oauth.lock
RUN python /tmp/calendar-release/apply.py
RUN python -m pip check
USER 10001
'''
        (source / ('Dockerfile.'+name)).write_text(dockerfile, encoding='utf-8')
    (state / 'base-images.json').write_text(json.dumps(images), encoding='utf-8')
    print(json.dumps({'source': str(source), 'files': len(changes), 'secrets_in_build': False}))


if __name__ == '__main__':
    main()
