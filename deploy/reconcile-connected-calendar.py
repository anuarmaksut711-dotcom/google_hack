"""Three-way merge the Calendar change with source exported from exact serving images.

Known deployed features (themes, voice, structured advice and manual classes)
are retained explicitly. Unknown conflicts stop the release.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def resolve(name, index, current, ours):
    if name == 'lms/multiuser.py':
        if index == 0:
            return ours
        if index == 1:
            return current + ours[ours.index('            if local_google_calendar'):]
        if index == 2:
            return ("                from ..web.google_calendar import ACCOUNT_GOOGLE_CALENDAR\n"
                    "                return render_moodle_settings(connected=connected, classes_html='',\n"
                    "                    google_calendar_html=ACCOUNT_GOOGLE_CALENDAR if local_google_calendar is not None else '')\n")
    if name == 'web/account.py':
        if index == 0:
            return current.replace('classes_html):', "classes_html, google_calendar_html=''):")
        if index == 1:
            return current.replace("+ classes_html + '</main>", "+ google_calendar_html + classes_html + '</main>")
    if name == 'web/assets/client.py' and index == 0:
        return current
    if name == 'web/chat.py':
        if index == 0:
            return current.replace('structured_advice=False):', 'structured_advice=False, additional_sources=None):')
        if index == 1:
            return current + "                    if additional_sources is not None:\n                        sources['google_calendar'] = await additional_sources(context)\n"
    if name == 'web/views/app.py' and index == 0:
        return current.replace("structured_advice=chat_mode == 'calendar_advice'))",
            "structured_advice=chat_mode == 'calendar_advice', additional_sources=chat_sources))")
    if name == 'web/views/render.py':
        if index == 0:
            return current + '{calendar_assets}\n'
        if index == 1:
            return current.replace('Ваше учебное пространство', 'Ваши задачи и планы').replace(
                '<main id="workspace">{moodle}', '<main id="workspace">{calendar_section}{moodle}')
        if index == 2:
            return current.replace('Задайте вопрос об учебном плане…', 'Задайте вопрос о вашем плане…')
    raise ValueError('unreviewed_serving_source_conflict: '+name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state = args.state.resolve()
    merge = state / 'merge'
    merge.mkdir(exist_ok=True)
    changes = json.loads((state / 'source/changes.json').read_text())
    brand = lambda s: re.sub(r'\bCampusPulse\b|(?i:\bfriday ai\b)', 'Google AI', s)
    for target in ('web', 'mcp'):
        source = json.loads((state / ('base-'+target+'.json')).read_text())
        output = []
        for change in changes:
            name = change['path']
            if target == 'mcp' and name not in ('integrations/local_calendar.py', 'cloud/connected_calendar.py',
                                               'cloud/connected_calendar_mcp.py'):
                continue
            path = 'src/campuspulse_recovery/'+name
            ours = brand((root / path).read_text(encoding='utf-8-sig'))
            cloud = source[name]
            old = subprocess.run(['git', 'show', 'HEAD:'+path], cwd=root, capture_output=True)
            result = ours
            if cloud is not None and not old.returncode:
                base = brand(old.stdout.decode('utf-8-sig'))
                for label, content in [('current', cloud), ('base', base), ('ours', ours)]:
                    (merge / label).write_text(content, encoding='utf-8')
                merged = subprocess.run(['git', 'merge-file', '-p', str(merge/'current'),
                    str(merge/'base'), str(merge/'ours')], capture_output=True)
                result = merged.stdout.decode('utf-8').replace('\r\n', '\n')
                if merged.returncode:
                    index = 0
                    pattern = re.compile(r'<<<<<<<[^\n]*\n(.*?)=======\n(.*?)>>>>>>>[^\n]*\n', re.S)
                    def choose(match):
                        nonlocal index
                        value = resolve(name, index, match[1], match[2])
                        index += 1
                        return value
                    result = pattern.sub(choose, result)
            if '<<<<<<<' in result:
                raise ValueError('unresolved_source_conflict')
            compile(result, name, 'exec')
            output.append({'path': name, 'new': result, 'expected_sha256': None if cloud is None else
                hashlib.sha256(cloud.encode()).hexdigest()})
        (state / ('source/changes-'+target+'.json')).write_text(json.dumps(output, ensure_ascii=False), encoding='utf-8')
        print(target, len(output), 'source modules reconciled')


if __name__ == '__main__':
    main()
