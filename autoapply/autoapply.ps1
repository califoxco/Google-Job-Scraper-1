# ============================================================================
#  autoapply.ps1 - fan out job applications through the Simplify extension.
#
#  Opens N job URLs as N tabs in ONE new Chrome window, then walks tab by tab:
#  waits for the page to load, makes sure Simplify's side panel is open, and
#  clicks its "Start Application" button so Simplify begins autofilling.
#  You review and submit each application yourself afterwards.
#
#  Simplify's button lives in Chrome's side panel (a chrome-extension:// page),
#  which NO in-page script or other extension can reach - so this tool drives
#  the real mouse/keyboard at calibrated screen coordinates. Reliability over
#  speed: generous waits, and a safety guard that aborts if Chrome ever stops
#  being the foreground window (so it can't click into the wrong app).
#
#  One-time setup:
#    1. (Recommended) chrome://extensions/shortcuts -> give Simplify's
#       "Activate the extension" a shortcut, e.g. Ctrl+Shift+Y, and pass it
#       during calibration:  autoapply.cmd -Calibrate -Shortcut "^+y"
#       (SendKeys syntax: ^=Ctrl, +=Shift, %=Alt; "^+y" = Ctrl+Shift+Y)
#    2. autoapply.cmd -Calibrate          (captures 3 screen positions)
#    3. autoapply.cmd -DryRun -FromDb 2   (hovers everything, clicks nothing)
#
#  Usage:
#    autoapply.cmd <url> <url> ...        open + apply to these URLs
#    autoapply.cmd -File urls.txt         one URL per line
#    autoapply.cmd -FromDb 5              top 5 new+matched leads from jobs.db
#    autoapply.cmd -ListOnly -FromDb 5    print the URLs, do nothing else
#
#  During a run: DON'T touch the mouse or keyboard until it finishes.
# ============================================================================
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$Urls,
    [string]$File,
    [int]$FromDb = 0,
    [switch]$Calibrate,
    [string]$Shortcut = '',      # with -Calibrate: store this SendKeys combo as the panel-open shortcut
    [switch]$DryRun,
    [switch]$ListOnly,
    [int]$LoadWaitSec = 0,       # per-tab page-load wait (0 = use config)
    [int]$PanelWaitSec = 0       # wait after opening the panel (0 = use config)
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$CfgPath  = Join-Path $PSScriptRoot 'autoapply.config.json'

# --- Win32 helpers ------------------------------------------------------------
if (-not ('AutoApply.Native' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
namespace AutoApply {
    public struct POINT { public int X; public int Y; }
    public static class Native {
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
        [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    }
}
'@
}
Add-Type -AssemblyName System.Windows.Forms

function Test-ChromeForeground {
    $h = [AutoApply.Native]::GetForegroundWindow()
    if ($h -eq [IntPtr]::Zero) { return $false }
    [uint32]$fgPid = 0
    [AutoApply.Native]::GetWindowThreadProcessId($h, [ref]$fgPid) | Out-Null
    if ($fgPid -eq 0) { return $false }
    $p = Get-Process -Id $fgPid -ErrorAction SilentlyContinue
    return ($p -and $p.ProcessName -ieq 'chrome')
}

function Assert-ChromeForeground([string]$doing) {
    if (-not (Test-ChromeForeground)) {
        Write-Host ""
        Write-Host "[ABORT] Chrome is no longer the foreground window (about to: $doing)." -ForegroundColor Red
        Write-Host "        Nothing else will be clicked. Re-run when ready, and don't touch" -ForegroundColor Red
        Write-Host "        the mouse/keyboard while a run is in progress." -ForegroundColor Red
        exit 2
    }
}

function Invoke-ClickAt([int]$x, [int]$y, [string]$what) {
    Assert-ChromeForeground "click $what"
    [AutoApply.Native]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 150
    [AutoApply.Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)   # left down
    [AutoApply.Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)   # left up
}

function Move-CursorTo([int]$x, [int]$y) {
    [AutoApply.Native]::SetCursorPos($x, $y) | Out-Null
}

function Send-Keys([string]$keys, [string]$doing) {
    Assert-ChromeForeground $doing
    [System.Windows.Forms.SendKeys]::SendWait($keys)
}

function Read-CursorAfterCountdown([string]$msg, [int]$secs) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Cyan
    for ($i = $secs; $i -ge 1; $i--) {
        Write-Host ("    capturing cursor position in {0}..." -f $i)
        Start-Sleep -Seconds 1
    }
    $pt = New-Object AutoApply.POINT
    [AutoApply.Native]::GetCursorPos([ref]$pt) | Out-Null
    Write-Host ("    captured ({0}, {1})" -f $pt.X, $pt.Y) -ForegroundColor Green
    return $pt
}

function Find-Chrome {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe' -ErrorAction SilentlyContinue
    if ($reg -and $reg.'(default)') { return $reg.'(default)' }
    throw "chrome.exe not found."
}

function Open-JobsWindow([string[]]$urlList) {
    $chrome = Find-Chrome
    Start-Process -FilePath $chrome -ArgumentList (@('--new-window') + $urlList)
    Write-Host ("  opened a new Chrome window with {0} tab(s); waiting for it..." -f $urlList.Count)
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (Test-ChromeForeground) { break }
        Start-Sleep -Milliseconds 400
    }
    if (-not (Test-ChromeForeground)) {
        Write-Host "[ABORT] the new Chrome window never became foreground." -ForegroundColor Red
        exit 2
    }
    # Maximize so the calibrated coordinates line up every time.
    [AutoApply.Native]::ShowWindow([AutoApply.Native]::GetForegroundWindow(), 3) | Out-Null  # SW_MAXIMIZE
    Start-Sleep -Milliseconds 800
}

# --- collect URLs ---------------------------------------------------------------
$urlList = @()
if ($Urls)  { $urlList += @($Urls | Where-Object { $_ -match '^https?://' }) }
if ($File)  { $urlList += @(Get-Content $File | Where-Object { $_ -match '^https?://' }) }
if ($FromDb -gt 0) {
    $py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
    $urlList += @(& $py (Join-Path $PSScriptRoot 'pick_urls.py') $FromDb | Where-Object { $_ -match '^https?://' })
}
$urlList = @($urlList | Select-Object -Unique)

if ($ListOnly) {
    if ($urlList.Count -eq 0) { Write-Host "no URLs." } else { $urlList | ForEach-Object { Write-Host $_ } }
    exit 0
}

# --- calibration -----------------------------------------------------------------
if ($Calibrate) {
    $sample = $null
    if ($urlList.Count -gt 0) { $sample = $urlList[0] }
    if (-not $sample) {
        $py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
        if (Test-Path $py) {
            $sample = @(& $py (Join-Path $PSScriptRoot 'pick_urls.py') 1) | Select-Object -First 1
        }
    }
    if (-not $sample) { Write-Host "Pass a sample job URL:  autoapply.cmd -Calibrate <url>" -ForegroundColor Yellow; exit 1 }

    Write-Host ""
    Write-Host "  CALIBRATION - one maximized Chrome window will open with a sample job." -ForegroundColor Cyan
    Write-Host "  Keep the window maximized; runs always maximize to match these positions."
    Open-JobsWindow @($sample)
    Start-Sleep -Seconds 5

    $icon = Read-CursorAfterCountdown "  1/3  Hover over the SIMPLIFY ICON in Chrome's toolbar (don't click yet)." 6
    Write-Host ""
    Write-Host "  Now CLICK the Simplify icon so its side panel opens, and wait for the" -ForegroundColor Cyan
    Write-Host "  'Start Application' button to appear." -ForegroundColor Cyan
    $btn  = Read-CursorAfterCountdown "  2/3  Hover over the START APPLICATION button (don't click)." 10
    $neut = Read-CursorAfterCountdown "  3/3  Hover over an EMPTY spot on the page itself (no links/buttons) - used to give focus back to the page between tabs." 6

    $cfg = [ordered]@{
        IconX          = $icon.X;  IconY   = $icon.Y
        ButtonX        = $btn.X;   ButtonY  = $btn.Y
        NeutralX       = $neut.X;  NeutralY = $neut.Y
        IconShortcut   = $Shortcut          # SendKeys combo; '' = click the icon instead
        PanelStaysOpen = $true              # side panel persists across tabs; set false if yours closes
        LoadWaitSec    = 12
        PanelWaitSec   = 5
    }
    $cfg | ConvertTo-Json | Set-Content -Path $CfgPath -Encoding utf8
    Write-Host ""
    Write-Host "  Saved $CfgPath" -ForegroundColor Green
    Write-Host "  Next: autoapply.cmd -DryRun -FromDb 2   (verifies positions without clicking)"
    exit 0
}

# --- run ----------------------------------------------------------------------
if ($urlList.Count -eq 0) {
    Write-Host "No URLs. Usage:  autoapply.cmd <url> ...  |  -File urls.txt  |  -FromDb 5" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $CfgPath)) {
    Write-Host "Not calibrated yet - run:  autoapply.cmd -Calibrate" -ForegroundColor Yellow
    exit 1
}
$cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
if ($LoadWaitSec  -le 0) { $LoadWaitSec  = [int]$cfg.LoadWaitSec }
if ($PanelWaitSec -le 0) { $PanelWaitSec = [int]$cfg.PanelWaitSec }

if ($urlList.Count -gt 10) {
    Write-Host ("  note: {0} tabs at once is a lot; Simplify + forms are heavy. Consider batches of ~8." -f $urlList.Count) -ForegroundColor Yellow
}

Write-Host ""
if ($DryRun) { Write-Host "  DRY RUN - cursor will move, nothing will be clicked." -ForegroundColor Cyan }
Write-Host ("  {0} job(s). HANDS OFF the mouse/keyboard until done." -f $urlList.Count) -ForegroundColor Cyan
Start-Sleep -Seconds 2

Open-JobsWindow $urlList
Start-Sleep -Seconds 3

function Open-Panel([object]$cfg, [bool]$dry) {
    if ($dry) { Move-CursorTo $cfg.IconX $cfg.IconY; Start-Sleep -Seconds 1; return }
    if ($cfg.IconShortcut) {
        Send-Keys $cfg.IconShortcut "open Simplify panel (shortcut)"
    } else {
        Invoke-ClickAt $cfg.IconX $cfg.IconY "the Simplify toolbar icon"
    }
}

$done = 0
for ($i = 0; $i -lt $urlList.Count; $i++) {
    $label = "[{0}/{1}]" -f ($i + 1), $urlList.Count

    # Select the tab: Ctrl+1 for the first, Ctrl+Tab to advance after that.
    if ($i -eq 0) { Send-Keys '^1' "select tab 1" } else { Send-Keys '^{TAB}' "next tab" }

    Write-Host ("  {0} loading ({1}s): {2}" -f $label, $LoadWaitSec, $urlList[$i])
    Start-Sleep -Seconds $LoadWaitSec

    # Make sure the Simplify panel is open (first tab always; later tabs only
    # if it doesn't persist). Opening is a toggle, so don't re-send when open.
    if (($i -eq 0) -or (-not [bool]$cfg.PanelStaysOpen)) {
        Open-Panel $cfg $DryRun
        Start-Sleep -Seconds $PanelWaitSec
    } else {
        Start-Sleep -Seconds 2   # give the panel a beat to re-detect the new tab's job
    }

    if ($DryRun) {
        Move-CursorTo $cfg.ButtonX $cfg.ButtonY
        Write-Host ("  {0} (dry) hovered Start Application" -f $label)
        Start-Sleep -Seconds 2
    } else {
        Invoke-ClickAt $cfg.ButtonX $cfg.ButtonY "Start Application"
        Write-Host ("  {0} clicked Start Application" -f $label) -ForegroundColor Green
        Start-Sleep -Seconds 2
    }

    # Hand focus back to the page so Ctrl+Tab is handled by the tab strip.
    if ($DryRun) { Move-CursorTo $cfg.NeutralX $cfg.NeutralY } else { Invoke-ClickAt $cfg.NeutralX $cfg.NeutralY "neutral page spot" }
    Start-Sleep -Milliseconds 600
    $done++
}

Write-Host ""
Write-Host ("  Done: {0}/{1} tabs processed." -f $done, $urlList.Count) -ForegroundColor Green
Write-Host "  Tabs are left open - go through each one, let Simplify finish filling,"
Write-Host "  answer anything it couldn't, and SUBMIT each application yourself."
