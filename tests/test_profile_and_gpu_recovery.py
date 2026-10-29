"""Regressions for migrated Windows Claude profiles and stale graphics queries."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector


class ProfileAndGpuRecovery(unittest.TestCase):
    def test_migrated_profile_precedes_legacy_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "Local"
            roaming = Path(directory) / "Roaming"
            migrated = local / "Claude-Data/claude_example"
            legacy = local / "Packages/Claude_example/LocalCache/Roaming/Claude"
            migrated.mkdir(parents=True)
            legacy.mkdir(parents=True)
            with patch.dict(
                os.environ, {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
            ), patch.object(collector, "CONFIG", {}):
                profiles = collector.claude_profiles()
            self.assertEqual(profiles[0], migrated)
            self.assertIn(legacy, profiles)
            self.assertIn(roaming / "Claude", profiles)

    def test_explicit_profile_override_remains_exclusive(self):
        with patch.object(collector, "CONFIG", {"claudeProfile": "custom-profile"}):
            self.assertEqual(collector.claude_profiles(), [Path("custom-profile")])

    def test_missing_migration_directory_keeps_legacy_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"LOCALAPPDATA": directory, "APPDATA": directory}
            ), patch.object(collector, "CONFIG", {}):
                self.assertEqual(
                    collector.claude_profiles(), [Path(directory) / "Claude"]
                )

    def test_healthy_idle_gpu_is_not_reopened(self):
        sample = collector.HardwareSample(gpu=0, vram=0)
        hardware = Mock()
        with patch.object(collector.time, "monotonic", return_value=100):
            self.assertEqual(collector.recover_gpu_query(hardware, sample, 1), 100)
        hardware.NcmClose.assert_not_called()

    def test_brief_counter_gap_is_not_reopened(self):
        hardware = Mock()
        with patch.object(collector.time, "monotonic", return_value=100):
            self.assertEqual(
                collector.recover_gpu_query(
                    hardware, collector.HardwareSample(gpu=-1, vram=-1), 95
                ),
                95,
            )
        hardware.NcmClose.assert_not_called()

    def test_persistent_gap_reopens_and_primes_without_overwriting_other_readings(self):
        sample = collector.HardwareSample(cpu=42, gpu=-1, vram=-1)

        def prime(pointer):
            pointer._obj.cpu = -1

        hardware = Mock()
        hardware.NcmSample.side_effect = prime
        with patch.object(collector.time, "monotonic", return_value=100):
            retried = collector.recover_gpu_query(hardware, sample, 60)
            collector.recover_gpu_query(hardware, sample, retried)
        self.assertEqual(sample.cpu, 42)
        hardware.NcmClose.assert_called_once()
        hardware.NcmSample.assert_called_once()

    def test_missing_memory_counter_also_triggers_recovery(self):
        hardware = Mock()
        with patch.object(collector.time, "monotonic", return_value=100):
            collector.recover_gpu_query(
                hardware,
                collector.HardwareSample(gpu=5, vram=-1, dedicatedTotal=1024),
                60,
            )
        hardware.NcmClose.assert_called_once()

    def test_zero_dedicated_capacity_does_not_cause_repeated_reopens(self):
        hardware = Mock()
        with patch.object(collector.time, "monotonic", return_value=100):
            collector.recover_gpu_query(
                hardware, collector.HardwareSample(gpu=5, vram=-1, dedicatedTotal=0), 60
            )
        hardware.NcmClose.assert_not_called()
