"""Run current sources with the workspace's installed, isolated dependencies."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
for name in ('.venv-agent', '.venv-web', '.venv-mcp'):
    site = ROOT / name / 'Lib/site-packages'
    if site.is_dir():
        sys.path.append(str(site))

if __name__ == '__main__':
    from campuspulse_recovery.lms.local_calendar import main
    main()
