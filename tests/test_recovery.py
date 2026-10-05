"""Exercise locked-file recovery and the Windows task's lifetime contract."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector
import task_scheduler


class Recovery(unittest.TestCase):
    def test_temporary_sharing_violation_retries_same_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            original = collector.os.replace
            attempts = []

            def locked_once(source, target):
                attempts.append(target)
                if len(attempts) == 1:
                    raise PermissionError("synthetic lock")
                return original(source, target)

            with patch.object(
                collector.os, "replace", side_effect=locked_once
            ), patch.object(collector.time, "sleep"):
                collector.atomic_json(path, {"fresh": True})
            self.assertEqual(json.loads(path.read_text()), {"fresh": True})
            self.assertEqual(len(attempts), 2)

    def test_locked_primary_does_not_prevent_mirror_or_next_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            primary, mirror = (
                Path(directory) / "primary.json",
                Path(directory) / "mirror.json",
            )
            original = collector.atomic_json

            def locked_primary(path, data):
                if path == primary:
                    raise PermissionError("synthetic lock")
                original(path, data)

            with patch.object(
                collector, "atomic_json", side_effect=locked_primary
            ), patch.object(collector, "log_failure"):
                collector.publish_snapshots({"tick": 1}, [primary, mirror])
            self.assertEqual(json.loads(mirror.read_text()), {"tick": 1})
            collector.publish_snapshots({"tick": 2}, [primary, mirror])
            self.assertEqual(json.loads(primary.read_text()), {"tick": 2})

    def test_diagnostics_exclude_exception_message(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            collector, "ROOT", Path(directory)
        ), patch.object(collector, "LAST_ERRORS", {}):
            collector.log_failure("test", ValueError("secret-token-provider-response"))
            text = (Path(directory) / "helper-errors.log").read_text()
            self.assertNotIn("secret-token", text)
            self.assertEqual(json.loads(text)["type"], "ValueError")

    def test_task_restarts_without_killing_healthy_instances_or_using_password(self):
        root = ET.fromstring(
            task_scheduler.task_xml("pythonw.exe", Path("collector.py"), "S-1-test")
        )
        namespaces = {"t": task_scheduler.NAMESPACE}

        def value(path):
            return root.find(path, namespaces).text

        self.assertEqual(value("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(value("t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(value("t:Settings/t:StopIfGoingOnBatteries"), "false")
        self.assertEqual(value("t:Settings/t:DisallowStartIfOnBatteries"), "false")
        self.assertEqual(
            value("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"), "PT1M"
        )
        self.assertEqual(value("t:Triggers/t:LogonTrigger/t:UserId"), "S-1-test")
        self.assertEqual(
            value("t:Principals/t:Principal/t:LogonType"), "InteractiveToken"
        )
        self.assertEqual(value("t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
        self.assertNotIn("Password", ET.tostring(root, encoding="unicode"))

    def test_xml_preserves_paths_with_spaces_and_ampersands(self):
        root = ET.fromstring(
            task_scheduler.task_xml(
                "C:/User & Me/pythonw.exe", Path("C:/My Tools/collector.py"), "S-1-test"
            )
        )
        ns = {"t": task_scheduler.NAMESPACE}
        self.assertEqual(
            root.find("t:Actions/t:Exec/t:Command", ns).text, "C:/User & Me/pythonw.exe"
        )
        self.assertEqual(
            root.find("t:Actions/t:Exec/t:Arguments", ns).text,
            '"' + str(Path("C:/My Tools/collector.py")) + '"',
        )
