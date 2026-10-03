# SPDX-License-Identifier: GPL-3.0-only
"""Per-user Windows collector. Never generates model requests or redeems resets.

Only sanitized measurements are written to disk. Authentication stays in the
native applications' existing stores and is decrypted in memory when needed.
"""

from __future__ import annotations
import argparse, base64, ctypes, ctypes.wintypes as wt, hashlib, json, math, os
from pathlib import Path
import queue, shutil, subprocess, threading, time, urllib.request, urllib.error
from datetime import datetime

ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONFIG = {}


def load_config():
    """Load optional local paths; credentials never belong in this file."""
    global CONFIG
    try:
        CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf8"))
        if not isinstance(CONFIG, dict):
            raise ValueError("config must be an object")
    except FileNotFoundError:
        CONFIG = {}
    if CONFIG.get("codexHome"):
        # Auth detection and the child app-server must use the same native store.
        os.environ["CODEX_HOME"] = os.path.expandvars(CONFIG["codexHome"])


def configured_path(name):
    value = CONFIG.get(name)
    return Path(os.path.expandvars(value)).expanduser() if value else None


def normalize_claude_grants(raw):
    """Count currently usable grants; an incomplete expiry inventory stays unknown."""
    unknown = {
        "count": None,
        "expiresAt": None,
        "expiryKnown": False,
        "noExpiry": False,
    }
    if not isinstance(raw, dict) or not isinstance(raw.get("grants"), list):
        return unknown
    now = time.time()
    count = 0
    expiries = []
    unknown_expiry = False
    for grant in raw["grants"]:
        if not isinstance(grant, dict) or grant.get("paused"):
            continue
        start = timestamp(grant.get("starts_at"))
        end = timestamp(grant.get("ends_at"))
        if (start and start > now) or (end and end <= now):
            continue
        left = grant.get("resets_left")
        if not isinstance(left, int) or left < 0:
            return unknown
        left = left or (1 if grant.get("claimable") else 0)
        if not left:
            continue
        count += left
        if end:
            expiries.append(end)
        else:
            unknown_expiry = True
    return {
        "count": count,
        "expiresAt": min(expiries) if expiries and not unknown_expiry else None,
        "expiryKnown": not unknown_expiry,
        "noExpiry": False,
    }


def timestamp(value):
    """Accept provider Unix seconds or ISO-8601; unsupported dates stay unknown."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def remaining(used):
    """Convert consumed percent, without treating null/nonfinite readings as zero."""
    if not isinstance(used, (int, float)) or not math.isfinite(used):
        return None
    return min(100.0, max(0.0, 100.0 - used))


def normalize_codex(raw):
    """Convert consumed percentages and duration-based windows, preserving missing data."""
    buckets = raw.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    # A populated multi-bucket response with no Codex bucket is not another agent's quota.
    if not buckets:
        bucket = raw.get("rateLimits")
    windows = []
    for field in ("primary", "secondary"):
        w = (bucket or {}).get(field)
        if not isinstance(w, dict):
            continue
        duration = w.get("windowDurationMins")
        pct = remaining(w.get("usedPercent"))
        if pct is None:
            continue
        if duration == 10080:
            label = "Weekly"
        elif duration == 300:
            label = "5h"
        else:
            label = f"{duration}m" if duration else "Usage"
        windows.append(
            {"label": label, "remaining": pct, "resetsAt": timestamp(w.get("resetsAt"))}
        )
    windows.sort(key=lambda w: w["label"] != "Weekly")
    resets = raw.get("rateLimitResetCredits")
    count = resets.get("availableCount") if isinstance(resets, dict) else None
    credits = resets.get("credits") if isinstance(resets, dict) else None
    expiries = []
    no_expiry = False
    available_rows = 0
    if isinstance(credits, list):
        for credit in credits:
            if credit.get("status") != "available":
                continue
            available_rows += 1
            expiry = timestamp(credit.get("expiresAt"))
            if expiry is not None:
                expiries.append(expiry)
            else:
                no_expiry = True
    # A capped detail list cannot establish the earliest expiration of all available resets.
    complete = (
        isinstance(count, int) and isinstance(credits, list) and available_rows == count
    )
    return {
        "windows": windows,
        "resets": {
            "count": count,
            "expiresAt": min(expiries) if expiries and complete else None,
            "expiryKnown": complete,
            "noExpiry": no_expiry and not expiries,
        },
    }


def normalize_claude(raw):
    """Prefer modern limits[]; legacy five_hour is the Session window, not a second row."""
    windows = []
    limits = raw.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            kind = limit.get("kind")
            if kind not in ("session", "weekly_all", "five_hour"):
                continue
            pct = remaining(limit.get("percent"))
            if pct is not None:
                windows.append(
                    {
                        "label": {
                            "session": "Session",
                            "weekly_all": "Weekly",
                            "five_hour": "5h",
                        }[kind],
                        "remaining": pct,
                        "resetsAt": timestamp(limit.get("resets_at")),
                    }
                )
    # Legacy five_hour is the session window. Do not render it twice when limits[] is present.
    if not windows:
        for field, label in (("five_hour", "Session"), ("seven_day", "Weekly")):
            w = raw.get(field)
            if isinstance(w, dict) and remaining(w.get("utilization")) is not None:
                windows.append(
                    {
                        "label": label,
                        "remaining": remaining(w["utilization"]),
                        "resetsAt": timestamp(w.get("resets_at")),
                    }
                )
    inventory = raw.get("rate_limit_resets") or raw.get("reset_credits")
    return {"windows": windows, "resets": normalize_reset_inventory(inventory)}


def normalize_reset_inventory(raw):
    """Normalize alternative inventory shapes; incomplete expiration lists stay unknown."""
    unknown = {
        "count": None,
        "expiresAt": None,
        "expiryKnown": False,
        "noExpiry": False,
    }
    if raw is None:
        return unknown
    if isinstance(raw, dict):
        count = raw.get("available_count", raw.get("availableCount"))
        entries = raw.get("resets", raw.get("credits"))
        if isinstance(count, int) and not isinstance(entries, list):
            return {**unknown, "count": count}
    elif isinstance(raw, list):
        count = None
        entries = raw
    else:
        return unknown
    if not isinstance(entries, list):
        return unknown
    now = time.time()
    available = [
        e
        for e in entries
        if e.get("status", "available") == "available"
        and not e.get("consumed_at")
        and (timestamp(e.get("expires_at", e.get("expiresAt"))) or float("inf")) > now
    ]
    if count is None:
        count = len(available)
    expiries = [timestamp(e.get("expires_at", e.get("expiresAt"))) for e in available]
    known = len(available) == count
    return {
        "count": count,
        "expiresAt": (
            min((e for e in expiries if e is not None), default=None) if known else None
        ),
        "expiryKnown": known,
        "noExpiry": bool(available) and all(e is None for e in expiries),
    }


def local_time(value):
    try:
        return (
            datetime.fromtimestamp(value).strftime("%b %d, %I:%M %p").replace(" 0", " ")
        )
    except (ValueError, TypeError, OSError, OverflowError):
        return "Unknown"


def quota_text(data):
    """Render local reset times and flag elapsed cached windows for a fresh reading."""
    lines = []
    for window in data.get("windows", []):
        reset = window.get("resetsAt")
        end = f"resets {local_time(reset)}" if reset else "reset not started"
        # A cached window that has elapsed needs a fresh provider reading, not an inferred 100%.
        pct = (
            f"{window['remaining']:.0f}%"
            if not reset or reset > time.time()
            else "Refresh pending"
        )
        lines.append(f"{window['label']} {pct} · {end}")
    return "\n".join(lines) or "Usage unavailable"


def reset_text(data):
    """Separate a credit's expiration from the renewal time of a quota window."""
    resets = data.get("resets", {})
    count = resets.get("count")
    if count is None:
        return "Unavailable"
    if count == 0:
        return "0 available"
    if resets.get("expiresAt"):
        expiry = f"expires {local_time(resets['expiresAt'])}"
    elif resets.get("expiryKnown") and resets.get("noExpiry"):
        expiry = "No expiry"
    else:
        expiry = "Expiry unavailable"
    return f"{count} available · {expiry}"


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def process_paths():
    """Inspect only native agent image paths using limited process-query access."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(ProcessEntry)]
    k.Process32NextW.argtypes = k.Process32FirstW.argtypes
    k.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    ]
    snap = k.CreateToolhelp32Snapshot(2, 0)
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    result = []
    if snap in (None, ctypes.c_void_p(-1).value):
        return result
    try:
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if name in ("codex.exe", "chatgpt.exe", "claude.exe"):
                handle = k.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        buf = ctypes.create_unicode_buffer(32768)
                        size = wt.DWORD(len(buf))
                        if k.QueryFullProcessImageNameW(
                            handle, 0, buf, ctypes.byref(size)
                        ):
                            result.append(
                                (
                                    entry.th32ProcessID,
                                    entry.th32ParentProcessID,
                                    name,
                                    buf.value,
                                )
                            )
                    finally:
                        k.CloseHandle(handle)
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return result


class AppServer:
    """Short-lived stdio JSON-RPC client; only initialize/account read methods are used."""

    def __init__(self, executable):
        self.process = subprocess.Popen(
            [str(executable), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf8",
            creationflags=NO_WINDOW,
            cwd=ROOT,
        )
        self.messages = queue.Queue()
        self.serial = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "notification_center_metrics",
                        "version": "1.0",
                    }
                },
            )
            self.process.stdin.write('{"method":"initialized"}\n')
            self.process.stdin.flush()
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                pass

    def call(self, method, params=None):
        self.serial += 1
        m = {"id": self.serial, "method": method}
        if params is not None:
            m["params"] = params
        self.process.stdin.write(json.dumps(m) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            m = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if m.get("id") == self.serial:
                if "error" in m:
                    raise RuntimeError(f"app-server error {m['error'].get('code')}")
                return m.get("result", {})
        raise TimeoutError("app-server read timed out")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def codex_executable(paths):
    """Prefer an explicit binary, then a running native binary or a PATH installation.

    Windows app packages can have protected/versioned install locations. Running
    Codex once makes its resource binary discoverable without copying credentials.
    An explicit path supports portable and custom installations when closed.
    """
    configured = configured_path("codexExecutable")
    if configured and configured.is_file():
        return configured
    for pid, parent, name, path in paths:
        if name == "codex.exe":
            return Path(path)
    binary = shutil.which("codex.exe")
    if binary:
        return Path(binary)
    return None


def codex_identity():
    """Native file-based OAuth sign-in only; API-key and WSL accounts are excluded."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    try:
        d = json.loads(path.read_text())
        tokens = d.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return None
        identity = (
            tokens.get("account_id") or tokens.get("id_token") or tokens["access_token"]
        )
        return hashlib.sha256(identity.encode()).hexdigest()
    except (OSError, ValueError):
        return None


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def unprotect(data):
    """Use current-user DPAPI and release the Windows-allocated output buffer."""
    c = ctypes.WinDLL("crypt32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    c.CryptUnprotectData.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(Blob),
    ]
    k.LocalFree.argtypes = [ctypes.c_void_p]
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src = Blob(len(data), buf)
    out = Blob()
    if not c.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        k.LocalFree(out.pbData)


def claude_credential():
    """Decrypt only the active Windows desktop account using the current user's DPAPI.

    Chromium v10 token-cache encryption uses the DPAPI-protected AES key from
    Local State. Account and organization identifiers stay in process memory.
    This private desktop format may change; unsupported profiles remain hidden.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    profiles = [Path.home() / "AppData/Roaming/Claude"]
    profiles += list(
        (Path.home() / "AppData/Local/Packages").glob(
            "Claude_*/LocalCache/Roaming/Claude"
        )
    )
    if configured_path("claudeProfile"):
        profiles = [configured_path("claudeProfile")]
    for profile in profiles:
        try:
            config = json.loads((profile / "config.json").read_text(encoding="utf8"))
            active = config.get("lastKnownAccountUuid")
            if not active:
                continue
            state = json.loads((profile / "Local State").read_text(encoding="utf8"))
            key = unprotect(base64.b64decode(state["os_crypt"]["encrypted_key"])[5:])
            encoded = config.get("oauth:tokenCacheV2")
            if not encoded:
                continue
            raw = base64.b64decode(encoded)
            cache = json.loads(
                AESGCM(key).decrypt(raw[3:15], raw[15:], None)
                if raw[:3] == b"v10"
                else unprotect(raw)
            )
            matches = [
                (scope, e)
                for scope, e in cache.items()
                if isinstance(e, dict)
                and e.get("token")
                and (not active or scope.startswith("acct:" + active + "|"))
            ]
            matches.sort(
                key=lambda item: (
                    "user:sessions:claude_code" not in item[0],
                    -(item[1].get("expiresAt") or 0),
                )
            )
            for scope, entry in matches:
                if "user:profile" not in scope:
                    continue
                # Electron's v2 key is acct:<account>|<client>:<organization>:<scopes>.
                parts = scope.split("|", 1)[1].split(":", 2) if "|" in scope else []
                organization = parts[1] if len(parts) == 3 else None
                return {
                    "identity": hashlib.sha256(
                        (
                            (active or scope.split("|")[0]) + "|" + (organization or "")
                        ).encode()
                    ).hexdigest(),
                    "token": entry["token"],
                    "expiresAt": entry.get("expiresAt"),
                    "profile": profile,
                    "organization": organization,
                }
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            continue
    return None


def claude_usage(credential):
    """Read quotas using the existing OAuth token; never emit provider bodies in logs."""
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": "Bearer " + credential["token"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "notification-center-metrics/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def claude_resets(credential):
    """Decode cached desktop grants in Node, passing only profile and active org in memory."""
    # The OAuth quota endpoint excludes desktop reset grants. Read only the
    # active organization's cached settings response; never redeem a grant.
    if not credential.get("organization"):
        return None
    node = (
        configured_path("nodeExecutable")
        or shutil.which("node.exe")
        or shutil.which("node")
    )
    if not node:
        return None
    request = {
        "profile": str(credential["profile"]),
        "organization": credential["organization"],
    }
    process = subprocess.run(
        [str(node), str(ROOT / "claude-reset-cache.cjs")],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf8",
        timeout=10,
        creationflags=NO_WINDOW,
    )
    if process.returncode:
        return None
    return json.loads(process.stdout)


class HardwareSample(ctypes.Structure):
    """x64 ABI shared with hardware.cpp; negative doubles mean unknown, not zero."""

    _fields_ = [
        (name, ctypes.c_double)
        for name in ("cpu", "effective", "ram", "disk", "gpu", "vram")
    ] + [
        ("logicalProcessors", ctypes.c_uint),
        ("dedicatedUsed", ctypes.c_ulonglong),
        ("dedicatedTotal", ctypes.c_ulonglong),
    ]


def percent(value):
    return (
        f"{value:.0f}%"
        if isinstance(value, (int, float)) and value >= 0 and math.isfinite(value)
        else "Unavailable"
    )


def render(hardware, agents):
    """Build exactly eight ordered rows shared with metrics-panel.h, including hidden agents."""
    h = hardware
    cpu = percent(h.get("cpu", -1))
    effective = h.get("effective", -1)
    effective = (
        f'{effective:.1f} / {h.get("logicalProcessors",0)}'
        if effective > 0
        else ("Idle" if effective == 0 else "Unavailable")
    )
    rows = [
        {
            "value": f"{cpu} · Eff. {effective}",
            "visible": True,
            "tooltip": "CPU busy time; effective logical-processor participation from the same sample.",
        },
        {
            "value": percent(h.get("ram", -1)),
            "visible": True,
            "tooltip": "Physical RAM in use / usable physical RAM.",
        },
        {
            "value": percent(h.get("disk", -1)),
            "visible": True,
            "tooltip": "Active time of the physical disk backing C:.",
        },
        {
            "value": f'{percent(h.get("gpu",-1))} · VRAM {percent(h.get("vram",-1))}',
            "visible": True,
            "tooltip": f'Busiest GPU engine. Dedicated memory: {h.get("dedicatedUsed",0)/2**20:.0f} / {h.get("dedicatedTotal",0)/2**20:.0f} MiB. Shared memory excluded.',
        },
    ]
    for name in ("codex", "claude"):
        a = agents.get(name, {})
        visible = bool(a.get("signedIn"))
        data = a.get("data") or {}
        tip = "Remaining account usage. " + (
            "Updated " + local_time(a["updatedAt"]) + ". " if a.get("updatedAt") else ""
        )
        if not a.get("running"):
            tip += "Agent closed; showing cached values. "
        if a.get("error"):
            tip += "Last refresh unavailable. "
        if name == "claude":
            tip += "Read from Claude Desktop on Windows."
        rows.append({"value": quota_text(data), "visible": visible, "tooltip": tip})
        reset_tip = (
            "Available redeemable resets; dates are expiration dates, distinct from quota-window renewal. "
            + tip
        )
        if name == "claude" and data.get("resetsUpdatedAt"):
            reset_tip += (
                "Reset inventory cached by Claude Desktop: "
                + local_time(data["resetsUpdatedAt"])
                + ". Open Claude Settings → Usage to refresh this inventory."
            )
        rows.append(
            {"value": reset_text(data), "visible": visible, "tooltip": reset_tip}
        )
    return {"schemaVersion": 1, "generatedAt": time.time(), "hardware": h, "rows": rows}


def atomic_json(path, data):
    """Replace a sanitized snapshot atomically; the reader allows delete sharing."""
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf8"
    )
    os.replace(temp, path)


def shell_snapshot_paths():
    # ShellExperienceHost is an AppContainer and receives a package-local
    # LOCALAPPDATA. Its own AC folder already grants its package access; use
    # that directory rather than changing permissions on the user's data.
    packages = (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Packages"
    )
    paths = []
    for profile in packages.glob("Microsoft.Windows.ShellExperienceHost_*/AC"):
        target = profile / "NotificationCenterMetrics"
        try:
            target.mkdir(exist_ok=True)
            paths.append(target / "panel.json")
        except OSError:
            pass
    return paths


def run(once=False):
    """One hardware loop plus one bounded account worker; stop via a named event.

    Identity changes discard prior readings. Signed-in accounts are fetched once
    at startup, then only while their native process runs. Cached values are
    process-local; raw credentials and account identifiers are never serialized.
    """
    load_config()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = wt.HANDLE
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    mutex = kernel.CreateMutexW(
        None,
        False,
        "Local\\NotificationCenterMetrics_" + os.environ.get("USERNAME", "user"),
    )
    if ctypes.get_last_error() == 183:
        return
    kernel.CreateEventW.restype = wt.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    stop_event = kernel.CreateEventW(
        None,
        True,
        False,
        "Local\\NotificationCenterMetricsStop_" + os.environ.get("USERNAME", "user"),
    )
    hardware = ctypes.CDLL(str(ROOT / "hardware.dll"))
    hardware.NcmSample.argtypes = [ctypes.POINTER(HardwareSample)]
    sample = HardwareSample()
    stop = threading.Event()
    lock = threading.Lock()
    agents = {}
    shell_paths = shell_snapshot_paths()
    next_discovery = 0

    def accounts():
        identities = {}
        last_fetch = {}
        while not stop.is_set():
            try:
                paths = process_paths()
                executable = codex_executable(paths)
                for name in ("codex", "claude"):
                    if stop.is_set():
                        break
                    credential = claude_credential() if name == "claude" else None
                    identity = (
                        credential["identity"]
                        if credential
                        else (codex_identity() if name == "codex" else None)
                    )
                    running = (
                        any(n == "claude.exe" for _, _, n, _ in paths)
                        if name == "claude"
                        else any(
                            n in ("codex.exe", "chatgpt.exe") for _, _, n, _ in paths
                        )
                    )
                    with lock:
                        previous = agents.get(name, {})
                        if identities.get(name) != identity:
                            previous = {}
                            last_fetch[name] = 0
                        identities[name] = identity
                        agents[name] = {
                            **previous,
                            "signedIn": bool(identity),
                            "running": running,
                        }
                    if not identity:
                        continue
                    due = time.time() - last_fetch.get(name, 0) >= 60
                    if not due or (not running and previous.get("data")):
                        continue
                    last_fetch[name] = time.time()
                    try:
                        if name == "claude":
                            data = normalize_claude(claude_usage(credential))
                            try:
                                inventory = claude_resets(credential)
                                data["resets"] = normalize_claude_grants(inventory)
                                if inventory:
                                    data["resetsUpdatedAt"] = inventory.get("updatedAt")
                            except Exception:
                                pass
                        else:
                            if not executable:
                                raise RuntimeError(
                                    "Native Codex executable unavailable"
                                )
                            client = AppServer(executable)
                            try:
                                account = client.call(
                                    "account/read", {"refreshToken": False}
                                ).get("account")
                                if not account:
                                    with lock:
                                        agents[name] = {
                                            "signedIn": False,
                                            "running": running,
                                        }
                                    continue
                                data = normalize_codex(
                                    client.call("account/rateLimits/read")
                                )
                            finally:
                                client.close()
                        with lock:
                            agents[name].update(
                                data=data, updatedAt=time.time(), error=None
                            )
                    except Exception as error:
                        # Do not log network response bodies, tokens, account IDs, or emails.
                        with lock:
                            agents[name]["error"] = type(error).__name__
                atomic_json(
                    ROOT / "agent-status.json",
                    {
                        name: {k: v for k, v in value.items() if k != "identity"}
                        for name, value in agents.items()
                    },
                )
            except Exception:
                pass
            if stop.wait(10):
                break

    worker = threading.Thread(target=accounts, daemon=True)
    worker.start()
    try:
        hardware.NcmSample(ctypes.byref(sample))
        time.sleep(1)
        while True:
            hardware.NcmSample(ctypes.byref(sample))
            h = {name: getattr(sample, name) for name, _ in sample._fields_}
            with lock:
                a = json.loads(json.dumps(agents))
            panel = render(h, a)
            atomic_json(ROOT / "panel.json", panel)
            if time.monotonic() >= next_discovery:
                # A package's AC directory may be created after helper startup.
                shell_paths = shell_snapshot_paths()
                next_discovery = time.monotonic() + 60
            for destination in shell_paths:
                try:
                    atomic_json(destination, panel)
                except OSError:
                    pass
            if once or kernel.WaitForSingleObject(stop_event, 1000) == 0:
                break
    finally:
        stop.set()
        worker.join(timeout=85)
        hardware.NcmClose()
        kernel.CloseHandle(stop_event)
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(args.once)
