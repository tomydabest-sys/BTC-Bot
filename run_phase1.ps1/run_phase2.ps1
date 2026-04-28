# BTC-Bot Phase 2 Calibrated Run
# Usage: .\run_phase2.ps1
#
# Run AFTER Phase 1 has produced 30-50 trades.
# This switches to the calibrated config.yaml without force-trade.
# Trades from Phase 1 stay in data/bot.db so analyze.py can compare.

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  BTC-BOT PHASE 2 — CALIBRATED MODE" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

# Verify Phase 1 produced trades
if (Test-Path .\data\bot.db) {
    $count = python -c "import sqlite3; c=sqlite3.connect('data/bot.db'); print(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0])"
    Write-Host "[INFO] Phase 1 left $count orders in data/bot.db" -ForegroundColor Green
} else {
    Write-Host "[WARN] No data/bot.db from Phase 1 — running fresh." -ForegroundColor Yellow
}

# Disable force-trade
Write-Host "`n[1/2] Disabling force-trade mode..." -ForegroundColor Yellow
$env:BOT_FORCE_TRADE = '0'
Write-Host "  [OK] BOT_FORCE_TRADE=0" -ForegroundColor Green

# Launch with calibrated config
Write-Host "`n[2/2] Starting bot with config.yaml..." -ForegroundColor Yellow
Write-Host "`nDashboard: http://127.0.0.1:8501`n" -ForegroundColor Cyan

python -m polybot.dashboard.launcher --config config.yaml --mode paper
