# Windows 11 Notification Center Styler and System Monitor

A Windows 11 Windhawk mod that adds system measurements and signed-in coding-agent account usage **above the calendar**, inside the existing styled calendar card. TranslucentShell is the default; the original styler's themes and custom settings remain available.

| Row | Measurement |
| --- | --- |
| CPU | Busy percentage and effective logical processors, `C_eff = (Σ u_i)² / Σ u_i²` |
| RAM | Used physical RAM as a percentage of usable physical RAM |
| Disk C: | Active time of the physical disk backing C: |
| GPU | Busiest engine percentage and dedicated VRAM used percentage |
| Codex | Remaining weekly and, when provided, 5-hour usage, with local reset date/time |
| Codex resets | Available redeemable resets and earliest known expiration |
| Claude | Remaining Session/5-hour and weekly usage, with local reset date/time |
| Claude resets | Available redeemable resets and earliest known expiration |

Agent rows appear only when a supported **native Windows sign-in** is detected. WSL does not need to be running. Missing information displays as unavailable, rather than a fabricated zero or full quota. The helper never sends model prompts, redeems resets, or purchases credits.

## Requirements

- Windows 11 on **x64**, with [Windhawk](https://windhawk.net/) installed normally. ARM64 and 32-bit Windows/Python are not supported by this helper build.
- A supported **64-bit Windows Python 3.12 or newer**, including `pythonw.exe`, from [python.org](https://www.python.org/downloads/windows/). Run the commands below in Windows PowerShell, not WSL.
- [Node.js 24 LTS](https://nodejs.org/en/download) for Claude reset-cache decoding. Node is optional for hardware, Codex and Claude usage percentages; without it, Claude reset inventory is unavailable.
- `cryptography`, installed below, for Windows Claude Desktop's encrypted token store.
- Optional: native Codex signed in using file-based ChatGPT OAuth (`%USERPROFILE%\.codex\auth.json`); native Claude Desktop signed in on Windows. An installed agent without a sign-in produces no rows.

This is an independent fork, not an official OpenAI or Anthropic integration. Account response formats and Claude's desktop/cache formats can change. See [supported account paths and limitations](docs/architecture.md#account-sources).

## Installation

1. Download this repository using **Code → Download ZIP**, extract it to a permanent directory, or clone it:

   ```powershell
   git clone https://github.com/matthewclso/windhawk-system-monitor.git
   cd windhawk-system-monitor
   ```

2. In Windhawk, disable any other copy of **Windows 11 Notification Center Styler**, including an existing fork. Select **Create a new mod**, replace the editor contents with the **entire** [src/mod.wh.cpp](src/mod.wh.cpp), then **Compile Mod** and enable it. For an existing fork, back up its source and settings, and replace its source in the editor instead. The metadata ID is retained from the original local fork for update compatibility. Select **TranslucentShell** in Settings if it is not already selected.

   Windhawk's [mod creation instructions](https://github.com/ramensoftware/windhawk/wiki/Creating-a-new-mod) explain this workflow. The generated C++ file is self-contained; no custom header installation is required. Until the helper starts, hardware rows show unavailable.

3. Create a Python environment in this permanent checkout and install dependencies:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   Ensure `py -3` selects a supported x64 Python. Keep `.venv` and this directory in place: the helper uses this environment after installation.

4. Compile the hardware helper with Windhawk's installed compiler, then install and start it:

   ```powershell
   .\.venv\Scripts\python.exe build.py
   .\.venv\Scripts\python.exe setup_helper.py --startup
   ```

   These commands do not require an administrator PowerShell. Windhawk may request elevation through its normal installation/editor workflow. If Windhawk is installed elsewhere:

   ```powershell
   .\.venv\Scripts\python.exe build.py --windhawk-dir 'D:\Apps\Windhawk'
   ```

5. Open Notification Center. Open your native Codex/Claude apps once so discovery and quota refresh can complete. Allow up to a minute for account readings. For Claude's reset count/expiration, open **Claude Desktop → Settings → Usage** once to populate its cached reset inventory.

The helper installs to `%LOCALAPPDATA%\NotificationCenterMetrics`. `--startup` adds a per-user Windows sign-in startup entry. Omit it if you want manual startup; run the installed `collector.py` with your environment's `pythonw.exe` when needed.

## Refresh behavior

Hardware refreshes every second. Account usage is checked about once a minute while the corresponding Windows process is running. A signed-in account can receive one initial fetch at helper startup; afterward, a closed agent retains its last reading. Cache state lives in the helper process and is reconstructed after restart. An elapsed quota window displays **Refresh pending** until the provider supplies a fresh reading.

**Claude usage percentages refresh directly through its OAuth usage endpoint. Only its reset count and expiration use the desktop Usage cache.** Open Settings → Usage to refresh that inventory; other Claude actions may also update that cache if the app fetches the same response. This helper does not navigate Claude, read its cookie database, or fetch reset inventory using a browser session. Codex usage and reset inventory both come from its native app-server account API.

All dates use Windows local time. A provider can omit a reset time when its window has not started. Unknown credit expiration stays unknown, especially when the provider returns a truncated list.

## Configuration

Optional paths can be supplied at install/update time. They are persisted in `%LOCALAPPDATA%\NotificationCenterMetrics\config.json`, not in the repository:

```powershell
.\.venv\Scripts\python.exe setup_helper.py --startup `
  --node 'C:\Program Files\nodejs\node.exe' `
  --codex 'C:\path\to\codex.exe' `
  --codex-home 'C:\path\to\native-codex-home' `
  --claude-profile 'C:\path\to\Claude'
```

Supply only the overrides you need. Omitted options retain earlier overrides. To clear one, remove its key from `config.json`, then rerun the installer to restart the helper. No tokens or passwords belong in this file.

| Key | Default discovery |
| --- | --- |
| `nodeExecutable` | `node.exe`/`node` on PATH |
| `codexExecutable` | Running native `codex.exe`, then `codex.exe` on PATH |
| `codexHome` | Inherited `CODEX_HOME`, otherwise `%USERPROFILE%\.codex` |
| `claudeProfile` | `%APPDATA%\Claude` or packaged `Claude_*\LocalCache\Roaming\Claude` |

Codex's desktop resource binary can live in a versioned package directory. If a saved override stops working after an app update, remove it and open Codex, or supply its new path. Custom `CODEX_HOME` must refer to the Windows sign-in you want displayed.

## Troubleshooting

Run diagnostics using the **same Python environment** used to install:

```powershell
.\.venv\Scripts\python.exe setup_helper.py --doctor
```

- **All hardware rows unavailable:** check the helper and DLL diagnostics. Run `python.exe "$env:LOCALAPPDATA\NotificationCenterMetrics\collector.py"` in the foreground to see startup errors. If a helper is already running, the singleton prevents a second one; reinstalling stops and restarts the existing instance.
- **Fresh snapshots but unavailable in the calendar:** the helper mirrors `panel.json` into ShellExperienceHost's package-local `AC\NotificationCenterMetrics` directory. This is essential because the shell's AppContainer has a different `LOCALAPPDATA`. Open Notification Center, wait up to a minute for directory discovery, and check the mirrored snapshot's age with `--doctor`.
- **Agent absent:** confirm the supported Windows app/account is signed in. WSL-only, API-key-only Codex, Codex keyring-only auth, and Claude CLI-only credentials are not supported in this release. A stale or unsupported desktop format may also prevent sign-in detection.
- **Usage unavailable but row visible:** sign-in was detected, but the provider request or executable discovery failed. Open the app and allow the next refresh. Inspect `%LOCALAPPDATA%\NotificationCenterMetrics\agent-status.json` for sanitized error class names; it contains no token or account ID.
- **Claude reset unavailable:** verify Node's zstd support via `--doctor`, then open Claude Settings → Usage. The desktop cache decoder deliberately treats missing/unsupported data as unavailable.
- **VRAM differs from Task Manager:** this is dedicated VRAM only, never shared GPU memory. On integrated GPUs, dedicated capacity can be small. The first hardware DXGI adapter is selected; additional GPUs are not aggregated.
- **Disk differs from C: file activity:** active time is measured on its backing physical disk, including activity on that disk's other partitions. For a multi-disk volume, the busiest backing disk is shown.

## Updating and uninstalling

Before updating, back up your Windhawk source/settings. Download/pull the new code, update Python dependencies, rerun `build.py` and `setup_helper.py --startup`, then replace the Windhawk editor source with the new `src/mod.wh.cpp` and compile. Theme/custom settings remain when updating the existing local mod.

To remove the helper:

```powershell
.\.venv\Scripts\python.exe setup_helper.py --uninstall
```

This stops only this helper, restores its previous startup entry when it still owns that entry, and removes its installed files and shell snapshot mirrors. Separately disable/remove the mod in Windhawk, then re-enable your previous styler if desired. Remove the checkout/venv only after uninstalling.

## Development and license

[src/metrics-panel.h](src/metrics-panel.h) contains the panel, [src/collector.py](src/collector.py) the native account collector, [src/hardware.cpp](src/hardware.cpp) the PDH/DXGI helper, and [src/claude-reset-cache.cjs](src/claude-reset-cache.cjs) the cache decoder. The complete Windhawk source is generated from the preserved upstream snapshot and these documented hooks.

```powershell
.\.venv\Scripts\python.exe build.py --assemble-only
.\.venv\Scripts\python.exe build.py --check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe build.py --mod
```

`--mod` additionally builds a development mod DLL; normal users compile the committed source in Windhawk. Native binaries are built locally and are not redistributed. Automated checks cover quota normalization, cache isolation/decoding and generated-source consistency; they do not substitute for Windhawk UI testing. See [architecture, snapshot contract and privacy](docs/architecture.md), [upstream attribution](NOTICE.md), and [GPL v3 license](LICENSE).
