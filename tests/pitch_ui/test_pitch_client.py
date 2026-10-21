from pathlib import Path
import shutil
import subprocess


def test_lost_response_and_factual_trace():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('client_pitch.cjs')), str(root)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
