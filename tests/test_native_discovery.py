"""Path precedence keeps custom native installations independent of WSL."""

import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector


class NativeDiscovery(unittest.TestCase):
    def tearDown(self):
        collector.CONFIG = {}

    def test_explicit_executable_overrides_running_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "codex.exe"
            binary.touch()
            collector.CONFIG = {"codexExecutable": str(binary)}
            self.assertEqual(
                collector.codex_executable([(1, 0, "codex.exe", "other.exe")]), binary
            )

    def test_running_binary_can_be_outside_desktop_resources(self):
        self.assertEqual(
            collector.codex_executable([(1, 0, "codex.exe", "C:/Tools/codex.exe")]),
            Path("C:/Tools/codex.exe"),
        )

    def test_path_binary_is_used_when_no_process_runs(self):
        with patch.object(collector.shutil, "which", return_value="C:/Tools/codex.exe"):
            self.assertEqual(collector.codex_executable([]), Path("C:/Tools/codex.exe"))
