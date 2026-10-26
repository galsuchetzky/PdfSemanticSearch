"""Per-user uninstall helper for the installed frozen executable."""
from __future__ import annotations
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile

PRODUCT = "AcrobatSemanticSearch"
START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Acrobat Semantic Search"
DESKTOP_SHORTCUT = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "Acrobat Semantic Search.lnk"
MARKER = ".acrobat_semantic_search_install"
DATA_MARKER = ".acrobat_semantic_search_data"


def schedule_uninstall() -> int:
    install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    if os.name != "nt" or not (install_dir / MARKER).is_file():
        print("Uninstall is available from the installed Windows application.", file=sys.stderr)
        return 2
    marker = install_dir / MARKER
    if not marker.is_file():
        print("The installation marker is missing; refusing to remove this folder.", file=sys.stderr)
        return 2

    root = str(install_dir).replace("'", "''")
    menu = str(START_MENU).replace("'", "''")
    desktop = str(DESKTOP_SHORTCUT).replace("'", "''")
    registry = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AcrobatSemanticSearch"
    data_dir = Path(os.environ.get("LOCALAPPDATA", "")) / PRODUCT
    settings = install_dir / "app-settings.json"
    if settings.is_file():
        try:
            data_dir = Path(json.loads(settings.read_text(encoding="utf-8"))["data_dir"]).resolve()
        except (OSError, ValueError, KeyError, TypeError):
            print("The data-folder setting is invalid; refusing to remove unverified data.", file=sys.stderr)
            return 2
    data = str(data_dir).replace("'", "''")
    data_marker = str(data_dir / DATA_MARKER).replace("'", "''")
    exe_path = str(install_dir / "AcrobatSemanticSearch.exe").replace("'", "''")
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$root = '{root}'
$exePath = '{exe_path}'
$dataPath = '{data}'
$dataMarker = '{data_marker}'

# Closing the window normally hides the app to its tray, so Settings can start
# uninstall while another app process still has files in the install directory.
# Stop only processes whose executable is this exact installation.
Get-Process -Name 'AcrobatSemanticSearch' | ForEach-Object {{
    try {{ if ($_.Path -eq $exePath) {{ Stop-Process -Id $_.Id -Force }} }} catch {{}}
}}
for ($i = 0; $i -lt 40; $i++) {{
    $running = @(Get-Process -Name 'AcrobatSemanticSearch' | Where-Object {{ try {{ $_.Path -eq $exePath }} catch {{ $false }} }})
    if ($running.Count -eq 0) {{ break }}
    Start-Sleep -Milliseconds 250
}}

Remove-Item -LiteralPath '{menu}' -Recurse -Force
Remove-Item -LiteralPath '{desktop}' -Force
Remove-Item -LiteralPath '{registry}' -Recurse -Force
if (Test-Path -LiteralPath $dataMarker) {{ Remove-Item -LiteralPath $dataPath -Recurse -Force }}
for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath $root); $i++) {{
    Remove-Item -LiteralPath $root -Recurse -Force
    if (Test-Path -LiteralPath $root) {{ Start-Sleep -Milliseconds 500 }}
}}
Remove-Item -LiteralPath $PSCommandPath -Force
"""
    fd, script_path = tempfile.mkstemp(prefix="acrobat-semantic-uninstall-", suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as output:
        output.write(script)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_path],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True,
    )
    return 0

