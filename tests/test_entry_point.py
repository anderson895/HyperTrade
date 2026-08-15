"""Launching has to work however the user reaches for it.

`src/main.py` uses relative imports, so running it as a plain script normally dies
with "attempted relative import with no known parent package". It re-runs itself as a
module instead; these pin that down.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def launch(args, cwd):
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_module_form_from_the_project_root():
    result = launch(["-m", "src.main", "--help"], ROOT)
    assert result.returncode == 0, result.stderr
    assert "hypertrade" in result.stdout


def test_running_the_file_directly_from_the_project_root():
    result = launch(["src/main.py", "--help"], ROOT)
    assert result.returncode == 0, result.stderr
    assert "hypertrade" in result.stdout


def test_running_it_from_inside_the_src_folder():
    """The trap that caught a real user: cd src, then `python main.py`."""
    result = launch(["main.py", "--help"], ROOT / "src")
    assert result.returncode == 0, result.stderr
    assert "hypertrade" in result.stdout


def test_the_console_runner_still_reports_success():
    """--help exits 0; a broken re-exec would surface as a non-zero code here."""
    assert launch(["-m", "src.main", "--help"], ROOT).returncode == 0
