"""Offline Agent Engine package preparation; no execute or upload operation."""
import argparse
import json
from pathlib import Path
import sys


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes input values; keep private paths out.
        raise ValueError('invalid_cli_arguments')


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = SafeParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--requirements', type=Path, default=root / 'requirements/cloud-linux.lock')
    parser.add_argument('--output', type=Path, required=True)
    try:
        options = parser.parse_args(argv)
        with options.settings.open('rb') as source:
            data = source.read(64_001)
        if len(data) > 64_000:
            raise ValueError('settings_too_large')
        raw = json.loads(data.decode('utf-8-sig'))
        sys.path.insert(0, str(root / 'src'))
        from campuspulse_recovery.agent_engine.prepare import prepare_bundle
        manifest = prepare_bundle(raw, wheel=options.wheel, requirements=options.requirements,
                                  output=options.output)
    except Exception:
        print(json.dumps({'status': 'blocked', 'reason': 'preparation_failed', 'cloud_calls': 0}))
        return 2
    print(json.dumps({'status': manifest['status'], 'artifact_count': len(manifest['files']), 'cloud_calls': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
