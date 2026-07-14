#!/usr/bin/env powershell
# ===========================================================================
#  AuditLens - automatic installer for Windows PowerShell
#
#  Usage:
#      .\scripts\setup.ps1            # full install from scratch
#      .\scripts\setup.ps1 init-db    # apply DB migrations only
#      .\scripts\setup.ps1 check      # check environment readiness
#      .\scripts\setup.ps1 venv       # install Python dependencies only
# ===========================================================================
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
#  Colors
# ---------------------------------------------------------------------------
$esc = [char]27
$Green  = "$esc[32m"
$Yellow = "$esc[33m"
$Red    = "$esc[31m"
$Blue   = "$esc[34m"
$NC     = "$esc[0m"

function info($msg) { Write-Host "${Blue}[*] $msg${NC}" }
function ok($msg)   { Write-Host "${Green}[OK] $msg${NC}" }
function warn($msg) { Write-Host "${Yellow}[!] $msg${NC}" }
function error($msg) { Write-Host "${Red}[ERR] $msg${NC}" }

# ---------------------------------------------------------------------------
#  Project root
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path (Join-Path $ScriptDir '..') | Select-Object -ExpandProperty Path
Set-Location "$RootDir"

# ---------------------------------------------------------------------------
#  Check system dependencies
# ---------------------------------------------------------------------------
function Test-CommandAvailable($Name) {
    return [bool](Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Invoke-ExternalCommand($Command, [string[]]$Arguments = @(), [switch]$IgnoreExitCode, [int]$TailOnError = 0) {
    # Windows PowerShell 5.1 treats native stderr as terminating error when
    # $ErrorActionPreference is 'Stop'. We temporarily silence it and return
    # both stdout and stderr as plain strings.
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $output = @()
    $exitCode = 0
    try {
        $output = & $Command @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEAP
    }
    $result = @()
    foreach ($line in $output) {
        if ($line -is [System.Management.Automation.ErrorRecord]) {
            $result += $line.Exception.Message
        }
        else {
            $result += $line
        }
    }
    if (-not $IgnoreExitCode -and $exitCode -ne 0) {
        $msg = "$Command $Arguments exited with code $exitCode"
        if ($TailOnError -gt 0 -and $result.Count -gt 0) {
            $tail = $result | Select-Object -Last $TailOnError
            $msg += "`n--- last $TailOnError lines of output ---`n$($tail -join "`n")"
        }
        else {
            $msg += "`n$($result -join "`n")"
        }
        throw $msg
    }
    return $result
}

function Get-CommandVersion($Name) {
    try {
        if ($Name -match 'python') {
            $ver = [string](Invoke-ExternalCommand -Command $Name -Arguments @('-c', 'import sys; print(sys.version.split()[0])'))
        }
        else {
            $ver = [string](Invoke-ExternalCommand -Command $Name -Arguments @('--version'))
        }
        return $ver.Trim()
    }
    catch {
        return 'N/A'
    }
}

function check_command($Name, $Advice) {
    if (-not (Test-CommandAvailable $Name)) {
        error "$Name is not installed. $Advice"
        return $false
    }
    ok "$Name found: $(Get-CommandVersion $Name)"
    return $true
}

function check_prereqs() {
    info 'Checking system dependencies...'
    $fail = $false

    $pythonCmd = $null
    if (Test-CommandAvailable 'python') {
        # Verify python is a real interpreter, not a broken launcher/store alias
        try {
            $null = Invoke-ExternalCommand -Command 'python' -Arguments @('-c', 'import sys; print(sys.version_info.major)')
            $pythonCmd = 'python'
        }
        catch {
            warn 'python command found but does not run correctly, trying python3...'
        }
    }
    if (-not $pythonCmd -and (Test-CommandAvailable 'python3')) {
        try {
            $null = Invoke-ExternalCommand -Command 'python3' -Arguments @('-c', 'import sys; print(sys.version_info.major)')
            $pythonCmd = 'python3'
        }
        catch {
            error 'Neither python nor python3 works correctly.'
            $fail = $true
        }
    }
    if (-not $pythonCmd) {
        error 'python is not found. Install Python 3.11+: https://www.python.org/'
        $fail = $true
    }
    else {
        ok "python found: $(Get-CommandVersion $pythonCmd)"
    }

    $dockerCmd = $null
    if (Test-CommandAvailable 'docker') { $dockerCmd = 'docker' }
    else {
        error 'docker is not installed. Install Docker Desktop: https://www.docker.com/products/docker-desktop/'
        $fail = $true
    }

    if ($fail) {
        error 'Install missing dependencies and run again.'
        exit 1
    }

    # Python version
    $pyv = Invoke-ExternalCommand -Command $pythonCmd -Arguments @('-c', "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    $needed = [version]'3.11'
    $actual = [version]$pyv
    if ($actual -lt $needed) {
        error "Python 3.11+ is required. You have: $pyv"
        exit 1
    }
    ok "Python version OK: $pyv"

    # Docker Compose v2
    try {
        $dc = Invoke-ExternalCommand -Command $dockerCmd -Arguments @('compose', 'version') | Select-Object -First 1
        ok "Docker Compose v2 found: $dc"
    }
    catch {
        error 'Docker Compose v2 not found. Update Docker Desktop to the latest version.'
        exit 1
    }
}

# ---------------------------------------------------------------------------
#  .env setup
# ---------------------------------------------------------------------------
function setup_env() {
    if (Test-Path '.env') {
        warn '.env already exists - skipping copy'
    }
    else {
        if (Test-Path '.env.example') {
            Copy-Item '.env.example' '.env'
            ok 'Created .env from template'
            warn 'OPEN .env AND FILL LLM_API_KEY (see docs/API_KEYS.md)'
        }
        else {
            warn '.env.example not found - skipping .env creation'
        }
    }
}

# ---------------------------------------------------------------------------
#  Virtualenv + Python dependencies
# ---------------------------------------------------------------------------
function setup_venv() {
    $pythonCmd = $null
    if (Test-CommandAvailable 'python') {
        try {
            $null = Invoke-ExternalCommand -Command 'python' -Arguments @('-c', 'import sys; print(sys.version_info.major)')
            $pythonCmd = 'python'
        }
        catch {
            warn 'python command found but does not run correctly, trying python3...'
        }
    }
    if (-not $pythonCmd -and (Test-CommandAvailable 'python3')) {
        try {
            $null = Invoke-ExternalCommand -Command 'python3' -Arguments @('-c', 'import sys; print(sys.version_info.major)')
            $pythonCmd = 'python3'
        }
        catch {
            error 'Neither python nor python3 works correctly.'
            exit 1
        }
    }
    if (-not $pythonCmd) {
        error 'python is not found. Install Python 3.11+: https://www.python.org/'
        exit 1
    }

    if (-not (Test-Path '.venv')) {
        info 'Creating virtual environment in .venv/'
        Invoke-ExternalCommand -Command $pythonCmd -Arguments @('-m', 'venv', '.venv')
    }

    # Some Windows Python installs create venv without pip; ensure it exists.
    try {
        Invoke-ExternalCommand -Command '.venv\Scripts\python.exe' -Arguments @('-m', 'pip', '--version') | Out-Null
    }
    catch {
        info 'pip not found in venv, bootstrapping with ensurepip...'
        Invoke-ExternalCommand -Command '.venv\Scripts\python.exe' -Arguments @('-m', 'ensurepip', '--default-pip') | Out-Null
    }

    $activate = Resolve-Path '.venv\Scripts\Activate.ps1' -ErrorAction SilentlyContinue
    if (-not $activate) {
        error 'Could not find .venv\Scripts\Activate.ps1. Virtual environment was not created correctly.'
        exit 1
    }

    info 'Activating virtual environment...'
    & $activate

    info 'Installing Python dependencies (this will take 3-7 minutes, ~2GB for ML models)...'
    Invoke-ExternalCommand -Command 'python' -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel') -TailOnError 20 | Out-Null
    # Local mode by default uses EMBEDDING_MODE=local -> needs torch+sentence-transformers
    Invoke-ExternalCommand -Command 'python' -Arguments @('-m', 'pip', 'install', '-e', '.[local-embeddings]') -TailOnError 50 | Out-Null
    ok 'Dependencies installed'

    info 'Installing Playwright Chromium (for PDF export and complex pages)...'
    try {
        Invoke-ExternalCommand -Command 'playwright' -Arguments @('install', 'chromium') -TailOnError 20 | Out-Null
        ok 'Playwright ready'
    }
    catch {
        warn 'playwright install chromium failed - try manually'
    }
}

# ---------------------------------------------------------------------------
#  Docker (PostgreSQL + SearXNG)
# ---------------------------------------------------------------------------
function setup_docker() {
    info 'Starting PostgreSQL + SearXNG via docker compose...'
    Invoke-ExternalCommand -Command 'docker' -Arguments @('compose', 'up', '-d') -IgnoreExitCode | Select-Object -Last 10

    info 'Waiting for Postgres to become healthy...'
    $ready = $false
    for ($i = 1; $i -le 30; $i++) {
        $pgOutput = Invoke-ExternalCommand -Command 'docker' -Arguments @('compose', 'exec', '-T', 'postgres', 'pg_isready', '-U', 'audit', '-d', 'bank_audit') -IgnoreExitCode
        if ($pgOutput -join ' ' -match 'accepting connections') {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        error 'Postgres did not become ready within 60s. Container logs (last 30 lines):'
        Invoke-ExternalCommand -Command 'docker' -Arguments @('compose', 'logs', '--tail=30', 'postgres') -IgnoreExitCode | ForEach-Object { Write-Host $_ }
        Write-Host
        warn 'Typical reasons:'
        warn '  - Port 5432 is occupied by a local PostgreSQL service - stop it'
        warn '  - Container locale conflict - update the repo: git pull'
        warn '  - Broken volume - recreate: docker compose down -v ; .\scripts\setup.ps1'
        exit 1
    }
    ok 'Postgres ready'
}

# ---------------------------------------------------------------------------
#  Migrations
# ---------------------------------------------------------------------------
function init_db() {
    info 'Applying migrations...'
    $DSN = if ($env:DATABASE_URL) { $env:DATABASE_URL } else { 'postgresql://audit:audit@localhost:5432/bank_audit' }
    # Convert SQLAlchemy DSN (postgresql+psycopg://...) to plain psql DSN
    $PSQL_DSN = $DSN -replace 'postgresql\+psycopg:', 'postgresql:'

    function apply_sql($f) {
        if (Test-Path $f) {
            info "  -> $f"
            $containerPath = '/tmp/' + (Split-Path -Leaf $f)
            Invoke-ExternalCommand -Command 'docker' -Arguments @('compose', 'cp', $f, "postgres:$containerPath") | Out-Null
            Invoke-ExternalCommand -Command 'docker' -Arguments @('compose', 'exec', '-T', 'postgres', 'psql', '-U', 'audit', '-d', 'bank_audit', '-v', 'ON_ERROR_STOP=1', '-f', $containerPath) | Out-Null
        }
    }

    foreach ($migration in (Get-ChildItem 'migrations\*.sql' -ErrorAction SilentlyContinue | Sort-Object Name)) {
        apply_sql $migration.FullName
    }

    if (Test-Path 'src\bank_audit\analytics\views.sql') {
        apply_sql 'src\bank_audit\analytics\views.sql'
    }

    ok 'All migrations applied'
}

# ---------------------------------------------------------------------------
#  Final check
# ---------------------------------------------------------------------------
function final_check() {
    info 'Final check...'
    $activate = Resolve-Path '.venv\Scripts\Activate.ps1' -ErrorAction SilentlyContinue
    if ($activate) { & $activate }

    try {
        python -c "from bank_audit import db; db.session().__enter__().execute(__import__('sqlalchemy').text('SELECT 1'))"
        ok 'DB connection from Python works'
    }
    catch {
        error 'Cannot connect to DB from Python'
        exit 1
    }

    if (Test-Path '.env') {
        $envContent = Get-Content '.env' -Raw
        if ($envContent -match 'fw_REPLACE_WITH_YOUR_KEY') {
            warn '.env still contains placeholder LLM_API_KEY=fw_REPLACE_WITH_YOUR_KEY'
            warn 'Get Fireworks key: https://fireworks.ai/ (free $15 credits)'
            warn 'Detailed instructions: docs/API_KEYS.md'
        }
        else {
            ok 'LLM_API_KEY is filled'
        }
    }
}

# ---------------------------------------------------------------------------
#  Menu
# ---------------------------------------------------------------------------
$cmd = if ($args.Count -gt 0) { $args[0] } else { 'all' }

switch ($cmd) {
    'check' { check_prereqs }
    'init-db' { init_db }
    'docker' { setup_docker }
    'venv' { setup_venv }
    'all' {
        Write-Host '==========================================================='
        Write-Host '  AuditLens - installation from scratch'
        Write-Host '==========================================================='
        Write-Host
        check_prereqs
        setup_env
        setup_docker
        setup_venv
        init_db
        final_check
        Write-Host
        Write-Host '==========================================================='
        ok 'Installation complete!'
        Write-Host '==========================================================='
        Write-Host
        Write-Host 'Start the application:'
        Write-Host '    .venv\Scripts\Activate.ps1'
        Write-Host '    uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000'
        Write-Host
        Write-Host 'Open in browser: http://127.0.0.1:8000'
    }
    default {
        error "Unknown command: $cmd"
        Write-Host 'Usage: .\scripts\setup.ps1 [all|check|init-db|docker|venv]'
        exit 1
    }
}
