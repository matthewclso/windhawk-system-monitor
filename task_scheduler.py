"""Manage only this user's system-monitor task through Windows Task Scheduler.

The scheduler owns the long-running collector so its lifetime is independent of
the installing terminal/Codex process. A minute trigger also recovers clean exits,
while IgnoreNew prevents starting duplicates of an already running task.
"""

import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

MARKER = "Windhawk System Monitor: per-user collector with automatic recovery"
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ROOT = Path(__file__).resolve().parent


def task_xml(pythonw, collector, sid, start=None):
    """Build a least-privilege task with no password or three-day/battery timeout."""
    ET.register_namespace("", NAMESPACE)

    def child(parent, name, value=None, **attributes):
        item = ET.SubElement(parent, "{" + NAMESPACE + "}" + name, attributes)
        if value is not None:
            item.text = str(value)
        return item

    task = ET.Element("{" + NAMESPACE + "}Task", version="1.2")
    info = child(task, "RegistrationInfo")
    child(info, "Description", MARKER)
    triggers = child(task, "Triggers")
    recurring = child(triggers, "TimeTrigger")
    repetition = child(recurring, "Repetition")
    child(repetition, "Interval", "PT1M")
    child(repetition, "StopAtDurationEnd", "false")
    child(
        recurring,
        "StartBoundary",
        start or (datetime.now() + timedelta(seconds=10)).isoformat(timespec="seconds"),
    )
    child(recurring, "Enabled", "true")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "UserId", sid)
    principal = child(child(task, "Principals"), "Principal", id="Author")
    child(principal, "UserId", sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for name, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "false",
        "AllowStartOnDemand": "true",
        "Enabled": "true",
        "Hidden": "false",
        "RunOnlyIfIdle": "false",
        "WakeToRun": "false",
        "ExecutionTimeLimit": "PT0S",
        "Priority": "7",
    }.items():
        child(settings, name, value)
    idle = child(settings, "IdleSettings")
    child(idle, "StopOnIdleEnd", "false")
    child(idle, "RestartOnIdle", "false")
    restart = child(settings, "RestartOnFailure")
    child(restart, "Interval", "PT1M")
    child(restart, "Count", "3")
    action = child(child(task, "Actions", Context="Author"), "Exec")
    child(action, "Command", pythonw)
    child(action, "Arguments", subprocess.list2cmdline([str(collector)]))
    child(action, "WorkingDirectory", Path(collector).parent)
    return ET.tostring(task, encoding="unicode")


def manage(operation, installation, xml=None):
    """Pass all variable data as stdin JSON, never executable PowerShell source."""
    powershell = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tools/manage-task.ps1"),
        ],
        input=json.dumps(
            {
                "operation": operation,
                "marker": MARKER,
                "collector": str(Path(installation) / "collector.py"),
                "xml": xml,
            }
        ),
        text=True,
        encoding="utf8",
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Task Scheduler operation failed: " + result.stderr.strip())
    return json.loads(result.stdout.lstrip("\ufeff"))
