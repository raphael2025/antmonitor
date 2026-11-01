"""Day/night behavior is exercised against the shipped JS without npm packages."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for browser JS regression")
def test_theme_browser_behavior():
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("theme_behavior.cjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
