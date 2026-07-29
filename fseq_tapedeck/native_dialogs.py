"""Native Windows Save/Open file dialogs for this locally-run tool (the server
runs on the user's own machine).

A GUI spawned by a background server process opens *without* foreground focus
and lands behind the browser, so each dialog is parented to an invisible,
top-most owner form that is actually shown and activated -- that reliably pulls
the modal to the front. This is the same technique routes_mixer.py uses for the
export folder picker. All inputs are passed via environment variables, never
interpolated into the script, so there is nothing to inject.
"""

import asyncio
import base64
import os
import sys
from typing import Optional

_SAVE_DIALOG_PS = """
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$script:picked = $null
$owner.Add_Shown({
    $owner.Activate()
    $dialog = New-Object System.Windows.Forms.SaveFileDialog
    if ($env:FSEQ_DIALOG_TITLE) { $dialog.Title = $env:FSEQ_DIALOG_TITLE }
    if ($env:FSEQ_DIALOG_FILTER) { $dialog.Filter = $env:FSEQ_DIALOG_FILTER }
    if ($env:FSEQ_INIT_DIR) { $dialog.InitialDirectory = $env:FSEQ_INIT_DIR }
    if ($env:FSEQ_INIT_NAME) { $dialog.FileName = $env:FSEQ_INIT_NAME }
    $dialog.OverwritePrompt = $true
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $script:picked = $dialog.FileName
    }
    $owner.Close()
})
[System.Windows.Forms.Application]::Run($owner)
if ($script:picked) { [Console]::Out.Write($script:picked) }
"""

_OPEN_DIALOG_PS = """
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$owner = New-Object System.Windows.Forms.Form
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$script:picked = $null
$owner.Add_Shown({
    $owner.Activate()
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    if ($env:FSEQ_DIALOG_TITLE) { $dialog.Title = $env:FSEQ_DIALOG_TITLE }
    if ($env:FSEQ_DIALOG_FILTER) { $dialog.Filter = $env:FSEQ_DIALOG_FILTER }
    if ($env:FSEQ_INIT_DIR) { $dialog.InitialDirectory = $env:FSEQ_INIT_DIR }
    $dialog.CheckFileExists = $true
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $script:picked = $dialog.FileName
    }
    $owner.Close()
})
[System.Windows.Forms.Application]::Run($owner)
if ($script:picked) { [Console]::Out.Write($script:picked) }
"""


def _encoded(script: str) -> str:
    """PowerShell -EncodedCommand expects base64 of UTF-16LE; using it avoids
    all shell/newline quoting concerns for a multi-line script."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


async def _run_dialog(script: str, env_extra: dict) -> Optional[str]:
    if sys.platform != "win32":
        raise RuntimeError("native file dialogs are only supported on Windows")

    env = dict(os.environ)
    env.update({k: v for k, v in env_extra.items() if v is not None})
    proc = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", _encoded(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip() or "file dialog failed"
        raise RuntimeError(detail)
    picked = stdout.decode("utf-8", "replace").strip()
    return picked or None


async def save_file_dialog(
    title: str,
    filter_str: str,
    initial_dir: Optional[str] = None,
    initial_name: Optional[str] = None,
) -> Optional[str]:
    """Pop a native Save As dialog; return the chosen path or None if cancelled."""
    return await _run_dialog(_SAVE_DIALOG_PS, {
        "FSEQ_DIALOG_TITLE": title,
        "FSEQ_DIALOG_FILTER": filter_str,
        "FSEQ_INIT_DIR": initial_dir,
        "FSEQ_INIT_NAME": initial_name,
    })


async def open_file_dialog(
    title: str,
    filter_str: str,
    initial_dir: Optional[str] = None,
) -> Optional[str]:
    """Pop a native Open dialog; return the chosen path or None if cancelled."""
    return await _run_dialog(_OPEN_DIALOG_PS, {
        "FSEQ_DIALOG_TITLE": title,
        "FSEQ_DIALOG_FILTER": filter_str,
        "FSEQ_INIT_DIR": initial_dir,
    })
