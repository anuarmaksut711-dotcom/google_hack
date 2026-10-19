"""Use the same isolated dependency bootstrap as the calendar server."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('run_moodle_calendar.py')))

if __name__ == '__main__':
    from campuspulse_recovery.lms.multiuser import main
    main()
