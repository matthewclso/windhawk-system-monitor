"""Assemble the single-file Windhawk mod and build native x64 helper code.

Assembly is platform independent. Compilation uses an installed Windhawk's
compiler, headers and engine import library; no proprietary binaries are shipped.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
VERSION = "1.8.0"
MOD_ID = "windows-11-notification-center-styler-fork"
TITLE = "Windows 11 Notification Center Styler and System Monitor"
DESCRIPTION = "Style Notification Center and show CPU, RAM, disk, GPU, and signed-in Codex/Claude usage and resets above the calendar"


def replace_once(text, old, new):
    """Fail on upstream drift instead of silently producing an incomplete mod."""
    if text.count(old) != 1:
        raise RuntimeError("Missing or ambiguous integration: " + old[:100])
    return text.replace(old, new, 1)


def assembled_source():
    text = (ROOT / "vendor/notification-center-styler-1.7.wh.cpp").read_text(
        encoding="utf8"
    )
    metadata = {
        "name": TITLE,
        "description": DESCRIPTION,
        "version": VERSION,
        "author": "Matthew So (system monitor), m417z (original styler)",
        "github": "https://github.com/matthewclso",
        "homepage": "https://github.com/matthewclso/windhawk-system-monitor",
        "architecture": "amd64",
    }
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for key, value in metadata.items():
            if line.startswith("// @" + key + " "):
                lines[index] = f"// @{key:<15} {value}"
    text = "\n".join(lines) + "\n"
    text = replace_once(text, "// For bug reports and feature requests, please open an issue here:\n// https://github.com/ramensoftware/windhawk-mods/issues",
                        "// Monitor/helper issues: https://github.com/matthewclso/windhawk-system-monitor/issues\n// Upstream styler issues: https://github.com/ramensoftware/windhawk-mods/issues")
    text = replace_once(
        text,
        "// ==/WindhawkMod==",
        "// @license         GPL-3.0-only\n// ==/WindhawkMod==",
    )
    overview = (ROOT / "docs/mod-readme.md").read_text(encoding="utf8")
    text = replace_once(
        text,
        "# Windows 11 Notification Center Styler",
        overview + "\n\n## Original styler documentation",
    )
    text = replace_once(text, '- theme: ""', "- theme: TranslucentShell")
    panel = (ROOT / "src/metrics-panel.h").read_text(encoding="utf8")
    text = replace_once(
        text,
        "#include <winrt/Windows.UI.Xaml.h>\n\nstruct ThemeTargetStyles",
        "#include <winrt/Windows.UI.Xaml.h>\n\n// BEGIN generated system monitor panel\n"
        + panel
        + "\n// END generated system monitor panel\n\nstruct ThemeTargetStyles",
    )
    hooks = {
        "ApplyCustomizations(elementId, frameworkElement, element.Type);": "ApplyCustomizations(elementId, frameworkElement, element.Type);\n                    // Attach only to the calendar card, after theme styling.\n                    ncm::ElementAdded(frameworkElement);",
        "void UninitializeForCurrentThread() {": "void UninitializeForCurrentThread() {\n    // XAML objects must be removed on their owning UI thread.\n    ncm::DetachThread();",
        "        StartStatsTimer();\n    }\n\n    return TRUE;": "        StartStatsTimer();\n    }\n\n    ncm::Start();\n    return TRUE;",
        "void Wh_ModUninit() {": "void Wh_ModUninit() {\n    // Join the file reader before unloading this DLL.\n    ncm::Stop();",
    }
    for old, new in hooks.items():
        text = replace_once(text, old, new)
    return text


def compiler_paths(windhawk):
    compiler = windhawk / "Compiler"
    engines = list((windhawk / "Engine").glob("*/64/windhawk.lib"))
    if not engines or not (compiler / "bin/clang++.exe").exists():
        raise RuntimeError(
            "Windhawk compiler/64-bit engine not found; use --windhawk-dir"
        )

    def version(path):
        return tuple(int(part) for part in path.parent.parent.name.split("."))

    return compiler, max(engines, key=version)


def compile_native(windhawk, mod=False):
    compiler, engine = compiler_paths(windhawk)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    clang = str(compiler / "bin/clang++.exe")
    subprocess.run(
        [
            clang,
            "-std=c++23",
            "-O2",
            "-shared",
            "-static-libstdc++",
            "-target",
            "x86_64-w64-mingw32",
            str(ROOT / "src/hardware.cpp"),
            "-lpdh",
            "-ldxgi",
            "-o",
            str(output / "hardware.dll"),
        ],
        cwd=compiler,
        check=True,
    )
    unwind = (
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
        / "Windhawk/Engine/Mods/64/libunwind.whl"
    )
    if not unwind.exists():
        raise RuntimeError(
            "Windhawk runtime libunwind.whl missing; compile a mod in Windhawk first"
        )
    shutil.copyfile(unwind, output / "libunwind.whl")
    if mod:
        subprocess.run(
            [
                clang,
                "-std=c++23",
                "-O2",
                "-shared",
                "-DUNICODE",
                "-D_UNICODE",
                "-DWINVER=0x0A00",
                "-D_WIN32_WINNT=0x0A00",
                "-D_WIN32_IE=0x0A00",
                "-DNTDDI_VERSION=0x0A000008",
                "-D__USE_MINGW_ANSI_STDIO=0",
                "-DWH_MOD",
                f'-DWH_MOD_ID=L"local@{MOD_ID}"',
                f'-DWH_MOD_VERSION=L"{VERSION}"',
                str(engine),
                "-include",
                "windhawk_api.h",
                "-target",
                "x86_64-w64-mingw32",
                "-Wl,--export-all-symbols",
                str(ROOT / "src/mod.wh.cpp"),
                "-lcomctl32",
                "-lole32",
                "-loleaut32",
                "-lruntimeobject",
                "-lshlwapi",
                "-o",
                str(output / f"notification-center-monitor-{VERSION}.dll"),
            ],
            cwd=compiler,
            check=True,
        )
    print("Native build complete:", output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assemble-only", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="verify committed generated source"
    )
    parser.add_argument(
        "--mod", action="store_true", help="also build a mod DLL for development"
    )
    parser.add_argument(
        "--windhawk-dir",
        type=Path,
        default=Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Windhawk",
    )
    args = parser.parse_args()
    source = assembled_source()
    target = ROOT / "src/mod.wh.cpp"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf8") != source:
            raise SystemExit("Generated source is stale; run build.py --assemble-only")
        print("Generated source matches inputs")
    else:
        with target.open("w", encoding="utf8", newline="\n") as stream:
            stream.write(source)
        if not args.assemble_only:
            compile_native(args.windhawk_dir, args.mod)
