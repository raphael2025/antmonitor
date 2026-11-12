"""Run the actual data-quality helpers and UI renderers with isolated DOM/API adapters."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for JS regression")
def test_data_quality_browser_behavior():
    result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("data_quality_behavior.cjs"))], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
