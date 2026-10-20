"""Source bootstrap; prefer a project venv, allow this workspace's test runtime."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
# The development session can install the three optional OAuth packages here
# without modifying shared Python environments. A normal editable installation
# of .[local-calendar] needs no bootstrap paths.
for site in (ROOT / '.validation/calendar-deps',
             *(ROOT / name / 'Lib/site-packages' for name in ('.venv-calendar', '.venv-agent', '.venv-web', '.venv-mcp'))):
    if site.is_dir():
        sys.path.append(str(site))

if __name__ == '__main__':
    from campuspulse_recovery.lms.local_google_calendar import main
    main()
