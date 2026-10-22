"""Interaction contracts for the shipped compact assignment renderer, with fake IO."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('scenario', [
    'grouping', 'feedTruth', 'deadlinesAndText', 'restScoresAndSourceLinks',
    'localCompletionAndRestore', 'emptyUnavailableAndDisabled',
    'focusRecovery', 'refreshFocusAndNoSteal',
])
def test_compact_assignment_renderer_preserves_data_and_actions(scenario):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser renderer regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('moodle_compact.cjs')), str(root), scenario],
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
