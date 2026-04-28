# BTC-Bot v3 — fix zero-size bug, deploy missed websocket.py, restart cleanly
#
# This script:
#   1. Verifies the bot is actually stopped
#   2. Copies missed v2 websocket.py from the v2 fix folder
#   3. Copies new v3 risk/manager.py (sizing floor)
#   4. Verifies all 4 v2/v3 patches are in place
#   5. Sets BOT_FORCE_TRADE=1 in current session
#   6. Starts the bot
#
# Run from repo root:
#   .\fix_and_restart.ps1 -V2Folder C:\path\to\btc-bot-fixes-v2 -V3Folder C:\path\to\btc-bot-fixes-v3
#
# Or pass paths inline via env vars BTC_V2_DIR / BTC_V3_DIR

param(
    [string]$V2Folder = $env:BTC_V2_DIR,
    [string]$V3Folder = $env:BTC_V3_DIR,
    [switch]$SkipBackup = $false
)

if (-not $V2Folder) { $V2Folder = ".\btc-bot-fixes-v2" }
if (-not $V3Folder) { $V3Folder = ".\btc-bot-fixes-v3" }

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  BTC-Bot v3 fix-and-restart"          -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  V2 source: $V2Folder"
Write-Host "  V3 source: $V3Folder"
Write-Host "  CWD:       $(Get-Location)"

# ─────────────────────────────────────────────────────────────────
# 1. Confirm bot is stopped
# ─────────────────────────────────────────────────────────────────
Write-Host "`n[1/5] Checking for running python processes..." -ForegroundColor Yellow
$running = Get-Process python -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "  Found these python processes:" -ForegroundColor Yellow
    $running | Format-Table Id, ProcessName, StartTime, CPU
    $kill = Read-Host "Kill them? (y/N)"
    if ($kill -eq 'y') {
        $running | Stop-Process -Force
        Start-Sleep -Seconds 2
        Write-Host "  [OK] Killed" -ForegroundColor Green
    } else {
        Write-Host "  [!] Aborting — bot must be stopped first" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  [OK] No python processes running" -ForegroundColor Green
}

# ─────────────────────────────────────────────────────────────────
# 2. Backup current files
# ─────────────────────────────────────────────────────────────────
if (-not $SkipBackup) {
    $bk = ".\.backup_pre_v3_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Write-Host "`n[2/5] Backing up current files to $bk..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $bk -Force | Out-Null
    Copy-Item .\src\polybot\data\websocket.py "$bk\websocket.py" -ErrorAction SilentlyContinue
    Copy-Item .\src\polybot\risk\manager.py "$bk\manager.py" -ErrorAction SilentlyContinue
    Write-Host "  [OK] Backups in $bk" -ForegroundColor Green
}

# ─────────────────────────────────────────────────────────────────
# 3. Copy missed websocket.py from v2 + new manager.py from v3
# ─────────────────────────────────────────────────────────────────
Write-Host "`n[3/5] Deploying patches..." -ForegroundColor Yellow

$ws_src = Join-Path $V2Folder "data\websocket.py"
if (-not (Test-Path $ws_src)) {
    Write-Host "  [!] $ws_src not found" -ForegroundColor Red
    Write-Host "      Did you save the v2 patch folder?" -ForegroundColor Red
    exit 1
}
Copy-Item $ws_src .\src\polybot\data\websocket.py -Force
Write-Host "  [OK] websocket.py (v2 batched subscribes)" -ForegroundColor Green

$mgr_src = Join-Path $V3Folder "risk\manager.py"
if (-not (Test-Path $mgr_src)) {
    Write-Host "  [!] $mgr_src not found" -ForegroundColor Red
    exit 1
}
Copy-Item $mgr_src .\src\polybot\risk\manager.py -Force
Write-Host "  [OK] manager.py (v3 sizing floor)" -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────
# 4. Verify ALL patches present
# ─────────────────────────────────────────────────────────────────
Write-Host "`n[4/5] Verifying patches..." -ForegroundColor Yellow

$checks = @(
    @{ File = "src\polybot\strategies\maker_edge.py"; Marker = "max_position_notional_usd" },
    @{ File = "src\polybot\strategies\maker_edge.py"; Marker = "min_quote_interval_s" },
    @{ File = "src\polybot\risk\manager.py";          Marker = "v3_floor_recovery" },
    @{ File = "src\polybot\risk\manager.py";          Marker = "_is_exit_order" },
    @{ File = "src\polybot\data\websocket.py";        Marker = "MAX_TOKENS_PER_SUBSCRIBE" },
    @{ File = "src\polybot\data\websocket.py";        Marker = "_send_subscribe_batch" },
    @{ File = "src\polybot\main.py";                  Marker = "_force_closed_markets" },
    @{ File = "src\polybot\main.py";                  Marker = "market_expired_force_close" }
)

$all_ok = $true
foreach ($c in $checks) {
    if (-not (Test-Path $c.File)) {
        Write-Host "  [MISSING]  $($c.File)" -ForegroundColor Red
        $all_ok = $false
        continue
    }
    $found = Select-String -Path $c.File -Pattern $c.Marker -SimpleMatch -Quiet
    if ($found) {
        Write-Host "  [OK]   $($c.File)  ::  '$($c.Marker)'" -ForegroundColor Green
    } else {
        Write-Host "  [BAD]  $($c.File)  ::  '$($c.Marker)' NOT FOUND" -ForegroundColor Red
        $all_ok = $false
    }
}

if (-not $all_ok) {
    Write-Host "`n  [!] Some patches missing — aborting restart" -ForegroundColor Red
    exit 1
}

# ─────────────────────────────────────────────────────────────────
# 5. Set environment and start
# ─────────────────────────────────────────────────────────────────
Write-Host "`n[5/5] Setting environment and starting bot..." -ForegroundColor Yellow
$env:BOT_FORCE_TRADE = '1'
Write-Host "  [OK] BOT_FORCE_TRADE = 1" -ForegroundColor Green

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  STARTING BOT (v3 patches loaded)"     -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "`nWatch for these in the FIRST 30 SECONDS:" -ForegroundColor Yellow
Write-Host "  - 'ws_subscribed_batch count=...' (NOT 142x 'ws_subscribed')" -ForegroundColor White
Write-Host "  - 'paper_fill ... sz=X.X' where X.X is NOT 0.0" -ForegroundColor White
Write-Host "  - 'sizing_floor_recovery' if floor is being used" -ForegroundColor White
Write-Host ""

python -m polybot.dashboard.launcher --config config.aggressive.yaml --mode paper --skip-validation
