"""Verify user-visible customer rate units and incomplete-data behavior."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for browser JS regression")
def test_customer_rate_browser_behavior():
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("customer_rates_behavior.cjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

