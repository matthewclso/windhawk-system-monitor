# Windows 11 Notification Center Styler and System Monitor

Styles Notification Center and adds CPU utilization/effective logical processors,
RAM, C: disk active time, GPU/dedicated VRAM, and remaining signed-in Codex/Claude
usage and reset credits above the calendar. Quota rows include local reset times;
agent rows appear only when a supported Windows sign-in is detected.

**The companion helper is required for measurements.** Install it using the
[repository setup instructions](https://github.com/matthewclso/windhawk-system-monitor#installation).
This mod does not install Python, Node.js, or the helper automatically.

TranslucentShell is the default theme. All original themes and custom styling
remain available. Disable other Notification Center Styler copies before enabling
this mod. Existing fork settings are retained when updating that fork.

Hardware refreshes every second; account usage refreshes about once per minute
while the native agent is running. Closed agents retain their last readings.
Claude reset credits come from its desktop Usage cache: open Settings → Usage to
refresh them. Unsupported or missing data displays as unavailable.

Based on m417z's Windows 11 Notification Center Styler, under GPL v3.
Report monitor/helper issues at the
[project issue tracker](https://github.com/matthewclso/windhawk-system-monitor/issues).
