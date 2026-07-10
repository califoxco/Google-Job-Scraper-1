# Job Scrape launcher: kills old instances, starts Flask + sqlite-web + ngrok,
# prints (and copies) the public VA link. Invoked by start_job_scrape.bat.
param([switch]$NoPause)  # -NoPause: skip browser + keypress (automated runs)

$Root = $PSScriptRoot
$Py   = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }  # fall back to PATH python

# App config (gitignored) drives the ngrok + LLM sections below.
$appCfg = $null
try { $appCfg = Get-Content "$Root\config.json" -Raw | ConvertFrom-Json } catch {}

Write-Host ""
Write-Host "  Job Scrape Launcher" -ForegroundColor Cyan
Write-Host "  -------------------"

# --- stop anything from a previous run ---------------------------------------
Write-Host "  Stopping old services (ports 5050, 8081, ngrok)..."
foreach ($port in 5050, 8081) {
    Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {} }
}
try { Stop-Process -Name ngrok -Force -ErrorAction Stop } catch {}
Start-Sleep 1

# --- start services -----------------------------------------------------------
Write-Host "  Starting Flask UI      ->  http://127.0.0.1:5050"
Start-Process $Py -ArgumentList '-m','jobscrape.app' -WorkingDirectory $Root -WindowStyle Hidden

Write-Host "  Starting sqlite-web    ->  http://127.0.0.1:8081"
Start-Process $Py -ArgumentList '-m','sqlite_web','--host','127.0.0.1','--port','8081','results/jobs.db' `
    -WorkingDirectory $Root -WindowStyle Hidden

# --- local LLM (Ollama) used by the title matcher ------------------------------
$llmEnabled  = (-not $appCfg) -or ($appCfg.llm_match_enabled -ne $false)
$ollamaUrl   = if ($appCfg -and $appCfg.ollama_url)   { $appCfg.ollama_url }   else { "http://127.0.0.1:11434" }
$ollamaModel = if ($appCfg -and $appCfg.ollama_model) { $appCfg.ollama_model } else { "qwen2.5:14b" }

function Get-OllamaModels {
    try {
        (Invoke-RestMethod "$($ollamaUrl.TrimEnd('/'))/api/tags" -TimeoutSec 3).models.name
    } catch { $null }
}

$llmStatus = "disabled"
if ($llmEnabled) {
    Write-Host "  Checking local LLM     ->  $ollamaUrl"
    $models = Get-OllamaModels
    if ($null -eq $models) {
        # Ollama not answering — try to start it if it's installed.
        $ollamaExe = $null
        $cmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($cmd) { $ollamaExe = $cmd.Source }
        elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") {
            $ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
        }
        if ($ollamaExe) {
            Write-Host "  Ollama not running     ->  starting it..."
            Start-Process $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
            foreach ($i in 1..10) {
                Start-Sleep -Milliseconds 900
                $models = Get-OllamaModels
                if ($null -ne $models) { break }
            }
        }
    }
    if ($null -eq $models) { $llmStatus = "down" }
    elseif (($models -contains $ollamaModel) -or
            ($models | Where-Object { $_ -like "$ollamaModel*" })) { $llmStatus = "ready" }
    else { $llmStatus = "nomodel" }
}

# --- ngrok tunnel (public link for the VA) -------------------------------------
$ngrokExe = $null
$cmd = Get-Command ngrok -ErrorAction SilentlyContinue
if ($cmd) { $ngrokExe = $cmd.Source }
else {
    # winget install location (PATH only updates in new shells)
    $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter ngrok.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($pkg) { $ngrokExe = $pkg.FullName }
}

# Token + reserved domain live in config.json (gitignored, next to the other keys)
$ngrokToken  = if ($appCfg) { $appCfg.ngrok_authtoken } else { $null }
$ngrokDomain = if ($appCfg) { $appCfg.ngrok_domain }   else { $null }

function Start-Tunnel([string[]]$tunnelArgs) {
    Start-Process $ngrokExe -ArgumentList $tunnelArgs -WindowStyle Hidden
    foreach ($i in 1..15) {
        Start-Sleep -Milliseconds 800
        try {
            $t = Invoke-RestMethod "http://127.0.0.1:4040/api/tunnels" -ErrorAction Stop
            if ($t.tunnels.Count -gt 0) { return $t.tunnels[0].public_url }
        } catch {}
    }
    return $null
}

$vaUrl = $null
$vaUrlIsRandom = $false
if ($ngrokExe) {
    # Make sure the authtoken is registered (ngrok persists it in ngrok.yml,
    # so this only actually runs if that file was wiped or never set up).
    $ngrokYml = "$env:LOCALAPPDATA\ngrok\ngrok.yml"
    if ($ngrokToken -and (-not (Test-Path $ngrokYml) -or
        -not (Select-String -Path $ngrokYml -Pattern 'authtoken' -Quiet))) {
        Write-Host "  Registering ngrok authtoken..."
        & $ngrokExe config add-authtoken $ngrokToken | Out-Null
    }

    # Tunnel points at Flask (5050). With a reserved domain the VA link is stable.
    # NOTE: some home networks TLS-block *.ngrok-free.dev locally, so don't
    # judge tunnel health by opening the link from this machine - registration
    # via the local ngrok API is the reliable signal, and external reachability
    # is what matters for the person you send the link to.
    if ($ngrokDomain) {
        Write-Host "  Starting ngrok tunnel  ->  https://$ngrokDomain"
        # --domain works on every ngrok v3; --url only exists on 3.13+
        $vaUrl = Start-Tunnel @('http', "--domain=$ngrokDomain", '5050')
        if (-not $vaUrl) {
            Write-Host "  Reserved domain failed to register - falling back to a random URL..." -ForegroundColor Yellow
            try { Stop-Process -Name ngrok -Force -ErrorAction Stop } catch {}
            Start-Sleep 1
            $vaUrl = Start-Tunnel @('http', '5050')
            $vaUrlIsRandom = $true
        }
    } else {
        Write-Host "  Starting ngrok tunnel  ->  public VA link"
        $vaUrl = Start-Tunnel @('http', '5050')
        $vaUrlIsRandom = $true
    }
} else {
    Write-Host "  ngrok not found - skipping the public VA link." -ForegroundColor Yellow
}

# --- wait for Flask to answer ---------------------------------------------------
foreach ($i in 1..20) {
    try {
        Invoke-WebRequest "http://127.0.0.1:5050/admin" -UseBasicParsing -ErrorAction Stop | Out-Null
        break
    } catch { Start-Sleep -Milliseconds 500 }
}

# --- summary --------------------------------------------------------------------
Write-Host ""
Write-Host "  Everything is up:" -ForegroundColor Green
Write-Host "    Admin UI     http://127.0.0.1:5050/admin"
Write-Host "    DB viewer    http://127.0.0.1:8081"
switch ($llmStatus) {
    "ready"    { Write-Host "    LLM matcher  $ollamaModel ready ($ollamaUrl)" -ForegroundColor Green }
    "nomodel"  { Write-Host "    LLM matcher  Ollama is up but '$ollamaModel' is missing - run: ollama pull $ollamaModel" -ForegroundColor Yellow }
    "down"     { Write-Host "    LLM matcher  Ollama NOT reachable - matching falls back to string rules (staff/director/manager)" -ForegroundColor Yellow }
    "disabled" { Write-Host "    LLM matcher  disabled in config.json" -ForegroundColor DarkGray }
}
if ($vaUrl) {
    $vaPass = if ($appCfg -and $appCfg.va_password) { $appCfg.va_password } else { "jobapply" }
    Write-Host "    VA link      $vaUrl/assistant" -ForegroundColor Cyan
    Write-Host "                 (password: $vaPass - link copied to clipboard)"
    if ($vaUrlIsRandom) {
        Write-Host "                 NOTE: random URL - changes on every launch, re-send it to your VA." -ForegroundColor Yellow
    } else {
        Write-Host "                 Permanent link. If it won't open from this network, your LAN" -ForegroundColor DarkGray
        Write-Host "                 may block *.ngrok-free.dev - it still works from outside." -ForegroundColor DarkGray
    }
    try { Set-Clipboard -Value "$vaUrl/assistant" } catch {}
} elseif ($ngrokExe) {
    Write-Host "    VA link      ngrok started but no tunnel came up." -ForegroundColor Yellow
    Write-Host "                 One-time setup: sign up at https://dashboard.ngrok.com," -ForegroundColor Yellow
    Write-Host "                 then run:  ngrok config add-authtoken <your-token>" -ForegroundColor Yellow
}
Write-Host ""

if (-not $NoPause) {
    Start-Process "http://127.0.0.1:5050/admin"
    Write-Host "  Press any key to close this window (services keep running)..."
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
}
exit 0  # don't leak the last probe's exit code
