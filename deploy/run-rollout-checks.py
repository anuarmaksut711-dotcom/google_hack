"""Linux release gate. Tests/evaluations use emulator or synthetic external edges."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-sha', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', a.source_sha):
        raise ValueError('explicit_source_sha_required')
    a.output.mkdir(parents=True, exist_ok=False)
    results = []

    def run(name, args):
        with (a.output / (name + '.log')).open('w') as log:
            result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT)
        results.append({'check':name,'exit_code':result.returncode})
        print(json.dumps(results[-1]), flush=True)
        if result.returncode:
            # Docker discards a failed build layer. Keep the bounded failure
            # evidence visible even when /out artifacts cannot be exported.
            print((a.output / (name + '.log')).read_text(errors='replace')[-16000:], flush=True)
            raise RuntimeError('release_check_failed:' + name)

    py = sys.executable
    run('pip-check', [py,'-m','pip','check'])
    run('pytest', ['firebase','emulators:exec','--only','firestore','--project','demo-campuspulse-tests',
        '--config','tests/firestore-emulator.json',
        f'{py} -m pytest -q --junitxml={a.output}/pytest.xml'])
    suites = ET.parse(a.output / 'pytest.xml').getroot()
    counts = {k:sum(int(s.get(k,0)) for s in suites.iter('testsuite'))
              for k in ('tests','failures','errors','skipped')}
    if counts['failures'] or counts['errors'] or counts['skipped']:
        raise RuntimeError('full_pytest_not_complete')
    run('recovery-evaluation', [py,'-I','-S','-B','evals/recovery/run_evaluation.py','--mode','offline',
                              '--output',str(a.output / 'recovery-evaluation')])
    run('voice-evaluation', [py,'-B','evals/voice/run_checks.py','--mode','literal-loopback',
        '--report',str(a.output/'voice-report.json'),'--','evals/voice','-q'])
    run('build', [py,'-m','build','--no-isolation','--outdir',str(a.output/'dist')])
    run('mcp-composition', [py,'deploy/check-recovery-composition.py'])
    artifacts = {f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (a.output/'dist').iterdir()}
    summary = dict(source_sha=a.source_sha, checks=results, pytest=counts, artifacts_sha256=artifacts,
                   external_services='synthetic_or_emulator', live_calendar_verified=False)
    (a.output/'qa-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
