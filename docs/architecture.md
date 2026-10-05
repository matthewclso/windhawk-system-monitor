# Architecture and data contract

## Execution and lifecycle

The Windhawk DLL runs in `ShellHost.exe` and/or `ShellExperienceHost.exe`. It keeps the original styling engine and appends four documented hooks: discover `CalendarCenterGrid` after styling; remove the panel on its owning UI thread during teardown; start a background snapshot reader on initialization; stop/join that reader before DLL unload.

The panel inserts an Auto row before the calendar, saves all original child row indices, and restores them on detach or failed attach. It inherits the surrounding card's brush, foreground and corner treatment. Weak host references avoid retaining destroyed flyouts. A UI DispatcherTimer owns XAML updates; the background MTA does only file parsing and publishes immutable snapshots under a mutex.

The Python helper is a native Windows per-user process, guarded by a named mutex. Its one-second main loop calls the x64 `hardware.dll` ABI. A separate account thread performs bounded provider calls so network latency does not delay hardware refresh. Automatic mode is managed by a per-user Windows Task Scheduler task: a logon trigger and repeating one-minute time trigger use `IgnoreNew` to avoid duplicate running tasks. `InteractiveToken` and `LeastPrivilege` use the signed-in user's native auth/DPAPI without a password, elevation or network restrictions from S4U. Execution time is unlimited; battery transitions do not stop the helper, and it does not wake a sleeping PC. Failed tasks have one-minute retries; the repeating trigger also recovers a clean exit. The process belongs to the Windows scheduler rather than its installing terminal or Codex process. These policies use the [Task Scheduler registration API](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskfolder-registertask).

Installation/update disables the owned task to prevent a restart race, signals the helper's named stop event, waits for its bounded child calls to finish, replaces files, and registers/starts the updated task. It never kills an arbitrary PID. The task name includes the Windows user SID; mutations also verify the project marker and collector action so another task is not overwritten. Manual mode on a fresh installation omits the scheduled task.

The helper atomically replaces UTF-8 snapshots through a temporary file. The DLL opens with `FILE_SHARE_DELETE` so replacement works concurrently. Other readers or antivirus may deny replacement temporarily: sharing violations receive two short retries, then that destination is skipped for the current tick. Other copies are still published and the next tick retries normally. Bounded diagnostic logs store exception class, numeric error code and stack locations, excluding exception messages and provider bodies. Fatal collector exceptions exit nonzero for scheduler recovery. Besides the ordinary user `LOCALAPPDATA` snapshot, the helper mirrors sanitized `panel.json` into existing `Microsoft.Windows.ShellExperienceHost_*\AC` directories. That AppContainer gets a package-local `LOCALAPPDATA`; the package's own directory already permits its access, so no ACL expansion is necessary. Directory discovery repeats every minute.

## Hardware definitions

- CPU: English PDH `Processor Information(*)\% Processor Time`, excluding every `_Total` instance. Require one valid reading per active logical processor, across all processor groups. Mean busy time is `Σu/N`; effective participation is `(Σu)²/Σ(u²)`. Uniform utilization yields N even at low overall load; utilization on one processor yields 1. Exactly idle yields 0/“Idle”. This is a participation measure, not the number of fully loaded cores.
- RAM: `GlobalMemoryStatusEx`, `(totalPhys - availPhys) / totalPhys`.
- Disk: discover C: physical disk extents with `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`; report `100 - % Idle Time`, clamped to 0–100, on the busiest matching physical disk. Activity from other partitions on that disk is included. A failed extent lookup yields unavailable.
- GPU: select the first non-software DXGI adapter. Match its LUID to PDH `GPU Engine` counters, sum process counters for each physical engine, then take the busiest engine, clamped to 100. Multiple GPUs are not aggregated.
- VRAM: adapter-wide `GPU Adapter Memory\Dedicated Usage` bytes divided by DXGI `DedicatedVideoMemory`. Shared memory is excluded, and a zero denominator is unavailable.

`HardwareSample` must match Python's `ctypes.Structure` in member order/alignment. It contains six doubles, one unsigned 32-bit logical-processor count and two unsigned 64-bit byte counts. Negative doubles mean unknown. The first PDH sample primes deltas; the helper waits a second before publishing. Sampling is serial; `NcmClose` releases the query.

## Account sources

### Codex

The helper detects native file-based OAuth sign-in in `CODEX_HOME/auth.json`, defaulting to `%USERPROFILE%\.codex`. A hash of the account identity is kept only in memory to discard readings when the account changes. API-key and keyring-only authentication are not supported by this file-based detector. It uses the configured, running or PATH native `codex.exe` to launch a short-lived `app-server` stdio JSON-RPC client, calls `initialize`, sends `initialized`, then calls only `account/read` and `account/rateLimits/read`. The child inherits the same `CODEX_HOME`; the executable itself interprets its native credential store.

Quota data prefers `rateLimitsByLimitId.codex`. A populated map without that bucket is not assumed to be Codex quota. `windowDurationMins` identifies weekly (10080) and five-hour (300) windows; missing windows are omitted. Displayed percent is `100 - usedPercent`, clamped to 0–100. Reset credits use `availableCount`; earliest expiration is shown only when the available detail list is complete.

### Claude

This release uses **Claude Desktop's Windows sign-in**, not WSL or CLI-only `.claude` credentials. It locates unpackaged/packaged profile stores, requires `lastKnownAccountUuid`, and reads only matching active-account entries from the encrypted v2 OAuth cache. Windows DPAPI unwraps the Local State AES key, then AES-GCM decrypts Chromium v10 records. The selected token must have the profile scope; Claude Code session scope is preferred. Identifiers/tokens remain in process memory. Unsupported encryption/profile versions are unavailable rather than guessed.

The usage request is `GET https://api.anthropic.com/api/oauth/usage` with the existing OAuth token. Modern `limits[]` identifies Session, Weekly and a distinct five-hour window when supplied. Legacy `five_hour` is labeled Session and is not duplicated when modern limits exist. Model-specific weekly windows are not presented as the overall weekly quota.

Claude's OAuth usage surface did not expose desktop reset grants in the tested response. The separate Node cache reader locates only the active organization's cached `https://claude.ai/api/organizations/<organization>/usage?cedar_ember=1` response. Chromium blockfile decoding, gzip/brotli/zstd decompression and committed-entry checks are best effort. It does not read cookies or create browser-authenticated requests. HTTP Date identifies the newest cached response. Only sanitized grant fields leave the decoder. Paused, expired or not-yet-active grants are ignored; claimable grants are counted. Missing inventory means unknown, not zero. Open desktop Settings → Usage to refresh that cache. Usage percentages refresh directly regardless of this cache.

These private response/cache formats are observed integration contracts, not guaranteed public APIs. Node 24 supplies `zlib.zstdDecompressSync`; older Node may decode uncompressed/gzip entries but cannot decode zstd. Provider changes may require collector updates.

## Snapshot schema v1

`panel.json` contains `schemaVersion: 1`, `generatedAt` (Unix seconds), sanitized `hardware`, and **eight ordered rows**: CPU, RAM, disk, GPU, Codex usage, Codex resets, Claude usage, Claude resets. Each row has `value` (text), `visible` (boolean), and `tooltip` (text). Agent visibility depends on supported sign-in detection. `agent-status.json` separately contains normalized windows, credit inventory, running/sign-in flags, sample times and error class names; no account identifiers.

The DLL caps input at 64 KiB, requires eight rows and rejects incomplete parse results. If a snapshot is over ten seconds old, hardware becomes unavailable and visible account values are marked cached. Missing/invalid files produce four unavailable hardware rows and hidden agent rows. Dates render in local Windows time. Elapsed cached quota windows display Refresh pending until fetched again; no inference of renewed quota is made. The helper does one initial signed-in fetch after startup and refreshes thereafter only while the corresponding process runs. Cached account readings are not restored from disk after restart.

## Privacy and recovery

Network operations are usage/account reads only. There are no model prompts, credit redemption, purchases, browser-cookie reads, ACL changes or WSL dependencies. Credentials remain in their original applications' stores and are decrypted only in memory as needed. Serialized diagnostics intentionally exclude provider response bodies, emails, account/organization IDs and tokens. Quota percentages and reset dates are still personal information: snapshots stay in per-user directories and should not be uploaded with bug reports unless deliberately shared.

The installer modifies only its per-user helper directory, its owned scheduled task, its legacy startup value and its own package-local snapshot mirrors. It preserves configuration overrides on update and records the previous startup entry once. Migrating to the scheduler removes only the old startup value that references this helper. Uninstall disables/removes the owned task and restores the earlier startup value when its name has not been taken over. It does not modify the Windhawk registry; mod install/update/removal uses Windhawk's editor, preserving its normal source/settings workflow.
