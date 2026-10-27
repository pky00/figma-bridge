"""Runs the real plugin main thread (plugin/code.js) in Node against a mock `figma` (tests/plugin_harness.mjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).with_name("plugin_harness.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_plugin_main_thread():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
