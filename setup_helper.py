"""Install the per-user Windows helper without modifying Windhawk's mod registry.

Run with the Python environment that will run the helper. Keep that environment
at its current location, including after a checkout is moved or deleted.
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
INSTALL = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    / "NotificationCenterMetrics"
)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "NotificationCenterMetrics"


def stop_helper():
    """Signal only our event, then wait for bounded account calls to clean up."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.restype = wt.HANDLE
    kernel.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    kernel.SetEvent.argtypes = [wt.HANDLE]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    name = "Local\\NotificationCenterMetricsStop_" + os.environ.get("USERNAME", "user")
    event = kernel.OpenEventW(2, False, name)
    if not event:
        return
    kernel.SetEvent(event)
    kernel.CloseHandle(event)
    for _ in range(90):
        time.sleep(1)
        event = kernel.OpenEventW(2, False, name)
        if not event:
            return
        kernel.CloseHandle(event)
    raise RuntimeError("Helper did not stop within 90 seconds; no files replaced")


def get_startup():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return list(winreg.QueryValueEx(key, RUN_NAME))
    except FileNotFoundError:
        return None


def validate_runtime():
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("Use 64-bit Windows Python on an x64 Windows host")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError(
            "pythonw.exe missing beside this Python; use a standard Windows Python installation"
        )
    return pythonw


def install(args):
    import winreg

    pythonw = validate_runtime()
    for name in ("hardware.dll", "libunwind.whl"):
        if not (ROOT / "outputs" / name).exists():
            raise RuntimeError("Build first: python build.py (missing " + name + ")")
    config_path = INSTALL / "config.json"
    config = (
        json.loads(config_path.read_text(encoding="utf8"))
        if config_path.exists()
        else {}
    )
    for option in ("nodeExecutable", "codexExecutable", "codexHome", "claudeProfile"):
        value = getattr(args, option)
        if value:
            resolved = Path(os.path.expandvars(value)).expanduser().resolve()
            if not resolved.exists():
                raise RuntimeError("Configured path does not exist: " + str(resolved))
            config[option] = str(resolved)
    # Capture the previous startup entry once, before our first mutation.
    before = get_startup()
    if before and str(INSTALL / "collector.py").lower() in before[0].lower():
        # An earlier version of this same helper is an upgrade, not a separate
        # startup entry to resurrect after deleting its installed files.
        before = None
    stop_helper()
    INSTALL.mkdir(parents=True, exist_ok=True)
    record = INSTALL / "installation.json"
    if not record.exists():
        record.write_text(
            json.dumps({"previousStartup": before}, indent=2), encoding="utf8"
        )
    for name in ("collector.py", "claude-reset-cache.cjs"):
        shutil.copyfile(ROOT / "src" / name, INSTALL / name)
    for name in ("hardware.dll", "libunwind.whl"):
        shutil.copyfile(ROOT / "outputs" / name, INSTALL / name)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf8")
    command = subprocess.list2cmdline([str(pythonw), str(INSTALL / "collector.py")])
    if args.startup:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
    subprocess.Popen(
        [str(pythonw), str(INSTALL / "collector.py")],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print("Helper installed and started:", INSTALL)
    print("Keep this Python environment:", pythonw.parent)
    print(
        "Startup enabled"
        if args.startup
        else "Startup setting preserved; use --startup to enable it"
    )


def uninstall():
    import winreg

    stop_helper()
    record = INSTALL / "installation.json"
    previous = (
        json.loads(record.read_text(encoding="utf8")).get("previousStartup")
        if record.exists()
        else None
    )
    current = get_startup()
    # Avoid overwriting a startup entry another installation has taken over.
    if current and str(INSTALL / "collector.py").lower() in current[0].lower():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if previous is None:
                winreg.DeleteValue(key, RUN_NAME)
            else:
                winreg.SetValueEx(key, RUN_NAME, 0, previous[1], previous[0])
    packages = INSTALL.parent / "Packages"
    for path in packages.glob(
        "Microsoft.Windows.ShellExperienceHost_*/AC/NotificationCenterMetrics"
    ):
        shutil.rmtree(path)
    if INSTALL.exists():
        shutil.rmtree(INSTALL)
    print("Helper removed. Disable/remove the mod separately in Windhawk.")


def doctor():
    """Report runtime health without opening credentials or displaying account data."""
    validate_runtime()
    print("Python:", sys.executable)
    print("Helper:", "installed" if (INSTALL / "collector.py").exists() else "missing")
    print("Startup:", "configured" if get_startup() else "absent")
    if (INSTALL / "hardware.dll").exists():
        ctypes.CDLL(str(INSTALL / "hardware.dll"))
        print("Hardware DLL: loadable")
    config_path = INSTALL / "config.json"
    config = (
        json.loads(config_path.read_text(encoding="utf8"))
        if config_path.exists()
        else {}
    )
    node = config.get("nodeExecutable") or shutil.which("node.exe")
    if node:
        result = subprocess.run(
            [
                node,
                "-e",
                "process.exit(typeof require('node:zlib').zstdDecompressSync === 'function' ? 0 : 1)",
            ],
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        print(
            "Claude reset decoder:",
            "ready" if result.returncode == 0 else "Node lacks zstd support",
        )
    else:
        print("Claude reset decoder: Node missing (other metrics still work)")
    snapshots = [INSTALL / "panel.json"] + list(
        (INSTALL.parent / "Packages").glob(
            "Microsoft.Windows.ShellExperienceHost_*/AC/NotificationCenterMetrics/panel.json"
        )
    )
    for path in snapshots:
        try:
            panel = json.loads(path.read_text(encoding="utf8"))
            print(
                "Snapshot:",
                path,
                "age",
                round(time.time() - panel["generatedAt"], 1),
                "seconds",
            )
        except (OSError, ValueError, KeyError):
            print("Snapshot: missing or invalid", path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--uninstall", action="store_true")
    operation.add_argument("--doctor", action="store_true")
    parser.add_argument(
        "--startup", action="store_true", help="start at Windows sign-in"
    )
    for option, flag in (
        ("nodeExecutable", "node"),
        ("codexExecutable", "codex"),
        ("codexHome", "codex-home"),
        ("claudeProfile", "claude-profile"),
    ):
        parser.add_argument("--" + flag, dest=option)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This installer must run in Windows, not WSL")
    if args.uninstall:
        uninstall()
    elif args.doctor:
        doctor()
    else:
        install(args)
