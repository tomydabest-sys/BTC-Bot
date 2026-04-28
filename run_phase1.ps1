# BTC-Bot Phase 1 Force-Trade Run
# Usage: .\run_phase1.ps1
#
# What this does:
# 1. Cleans previous run state (logs, DB)
# 2. Sets BOT_FORCE_TRADE=1
# 3. Starts the bot with the aggressive config
# 4. Opens dashboard in browser
#
# Run from the repo root.

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  BTC-BOT PHASE 1 — FORCE TRADE MODE" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

# 1. Pre-flight checks
Write-Host "[1/5] Pre-flight checks..." -ForegroundColor Yellow

# Check Binance reachability
$binanceTest = Test-NetConnection stream.binance.com -Port 9443 -WarningAction SilentlyContinue
if ($binanceTest.TcpTestSucceeded) {
    Write-Host "  [OK] Binance WS reachable" -ForegroundColor Green
} else {
    Write-Host "  [WARN] Binance WS unreachable — will use REST fallback or run with --mock-btc-feed" -ForegroundColor Yellow
}

# Check Polymarket reachability
try {
    $polyTest = Invoke-WebRequest -Uri "https://gamma-api.polymarket.com/markets?limit=1" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    if ($polyTest.StatusCode -eq 200) {
        Write-Host "  [OK] Polymarket Gamma API reachable" -ForegroundColor Green
    }
} catch {
    Write-Host "  [FAIL] Polymarket Gamma API unreachable: $_" -ForegroundColor Red
    exit 1
}

# 2. Clean slate
Write-Host "`n[2/5] Cleaning previous state..." -ForegroundColor Yellow
Remove-Item .\logs\decisions.jsonl -ErrorAction SilentlyContinue
Remove-Item .\logs\polybot.log -ErrorAction SilentlyContinue
Remove-Item .\data\bot.db -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path .\logs -Force | Out-Null
New-Item -ItemType Directory -Path .\data -Force | Out-Null
Write-Host "  [OK] Cleared logs/ and data/bot.db" -ForegroundColor Green

# 3. Set environment
Write-Host "`n[3/5] Setting BOT_FORCE_TRADE=1..." -ForegroundColor Yellow
$env:BOT_FORCE_TRADE = '1'
Write-Host "  [OK] Force-trade mode ENABLED" -ForegroundColor Green

# 4. Open dashboard in browser (after 5s delay)
Write-Host "`n[4/5] Scheduling dashboard auto-open in 8s..." -ForegroundColor Yellow
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 8
    Start-Process "http://127.0.0.1:8501"
} | Out-Null
Write-Host "  [OK] Browser will open http://127.0.0.1:8501" -ForegroundColor Green

# 5. Launch bot
Write-Host "`n[5/5] Starting bot...`n" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  In a SECOND PowerShell window, run:" -ForegroundColor Cyan
Write-Host "    while(`$true) { Clear-Host; python analyze.py --since 5m; Start-Sleep -Seconds 10 }" -ForegroundColor White
Write-Host "========================================`n" -ForegroundColor Cyan

python -m polybot.dashboard.launcher --config config.aggressive.yaml --mode paper --skip-validation
