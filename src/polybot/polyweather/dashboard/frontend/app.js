"use strict";

const REFRESH_MS = 5000;
const charts = {};

function fmtMoney(x) {
  if (x === null || x === undefined) return "—";
  const n = typeof x === "string" ? parseFloat(x) : x;
  if (Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function fmtPct(x) {
  if (x === null || x === undefined) return "—";
  const n = typeof x === "string" ? parseFloat(x) : x;
  if (Number.isNaN(n)) return "—";
  return `${n.toFixed(2)}%`;
}

function fmtNum(x, dp = 4) {
  if (x === null || x === undefined) return "—";
  const n = typeof x === "string" ? parseFloat(x) : x;
  if (Number.isNaN(n)) return "—";
  return n.toFixed(dp);
}

function fmtDuration(seconds) {
  // "23h45m", "45m", "0m" — used for the halt-recovery countdown.
  if (seconds === null || seconds === undefined) return "—";
  const n = typeof seconds === "string" ? parseFloat(seconds) : seconds;
  if (Number.isNaN(n) || n < 0) return "—";
  const total = Math.round(n);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m`;
  return `${total}s`;
}

function fmtStat(x, dp = 3) {
  // Like fmtNum but renders explicit "N/A (need 30+)" when the backend
  // reports null — used for Sharpe/Brier where small samples are meaningless.
  if (x === null || x === undefined) return "N/A (need 30+)";
  const n = typeof x === "string" ? parseFloat(x) : x;
  if (Number.isNaN(n)) return "—";
  return n.toFixed(dp);
}

function setTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("tab-active", t.dataset.tab === name)
  );
  document.querySelectorAll(".tab-panel").forEach((p) =>
    p.classList.toggle("tab-panel-active", p.id === `tab-${name}`)
  );
}

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => setTab(t.dataset.tab));
});

async function safeFetch(url) {
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (err) {
    console.error("fetch fail", url, err);
    return null;
  }
}

function showError(elemId, msg) {
  const el = document.getElementById(elemId);
  if (el) {
    el.textContent = msg;
    el.classList.remove("hidden");
  }
}

function hideError(elemId) {
  const el = document.getElementById(elemId);
  if (el) el.classList.add("hidden");
}

// ─── overview ────────────────────────────────────────────────────────

// Track whether we ever rendered real data. If we did, transient nulls
// from a single failed fetch shouldn't pop the empty-state on top of
// stale-but-still-valid card values.
let _overviewEverHadData = false;

function renderOverview(data) {
  if (!data) {
    if (!_overviewEverHadData) {
      showError("overview-empty", "Data unavailable — see logs.");
    }
    return;
  }
  _overviewEverHadData = true;
  hideError("overview-empty");
  document.getElementById("hero-bankroll").textContent = fmtMoney(data.bankroll_usdc);
  document.getElementById("hero-pnl24").textContent =
    fmtMoney(data.pnl_24h_usdc) + " (" + fmtPct(data.pnl_24h_pct) + ")";
  document.getElementById("hero-pnl7").textContent = fmtMoney(data.pnl_7d_usdc);
  document.getElementById("hero-pnl30").textContent = fmtMoney(data.pnl_30d_usdc);
  document.getElementById("hero-open").textContent = data.open_positions ?? 0;
  document.getElementById("hero-exposure").textContent = fmtPct(data.open_exposure_pct);
  document.getElementById("hero-brier").textContent = fmtStat(data.brier_30d, 3);
  document.getElementById("hero-sharpe").textContent = fmtStat(data.sharpe_30d, 3);

  const modeBanner = document.getElementById("mode-banner");
  modeBanner.textContent = data.mode;
  modeBanner.className = "mode-banner " + (
    data.mode === "LIVE" ? "mode-live"
    : data.mode === "PAPER" ? "mode-paper"
    : data.mode === "HALTED" ? "mode-halted"
    : data.mode === "LIVE-DATA" ? "mode-live-data"
    : "mode-mock"
  );
  const mockWarning = document.getElementById("mock-warning");
  if (mockWarning) {
    if (data.mode === "MOCK") {
      mockWarning.textContent =
        "⚠ MOCK MODE — all data is synthetic, no real Polymarket orders are placed. " +
        "See README.polyweather.md \"Going live\" for how to connect to real markets.";
      mockWarning.classList.remove("hidden");
    } else if (data.mode === "LIVE-DATA") {
      mockWarning.textContent =
        "🛰 LIVE-DATA mode: reading REAL Polymarket markets + forecasts. " +
        "Paper-filling only — no money at risk. Required 14 days + 100 trades " +
        "before live mode can unlock.";
      mockWarning.classList.remove("hidden");
    } else {
      mockWarning.classList.add("hidden");
    }
  }

  const hb = document.getElementById("heartbeat");
  const hbStat = data.heartbeat && data.heartbeat.status;
  hb.textContent = `heartbeat ${hbStat || "—"} (${data.heartbeat ? data.heartbeat.count : 0})`;
  hb.className = "heartbeat heartbeat-" + (hbStat === "green" ? "green" : hbStat === "amber" ? "amber" : "red");

  drawEquity(data.equity_curve);
  drawDrawdown(data.drawdown_curve);
}

function drawEquity(curve) {
  const ctx = document.getElementById("equity-chart");
  if (!ctx || !curve) return;
  const labels = curve.map((p) => new Date(p.ts * 1000).toLocaleString());
  const values = curve.map((p) => parseFloat(p.bankroll));
  if (charts.equity) {
    charts.equity.data.labels = labels;
    charts.equity.data.datasets[0].data = values;
    charts.equity.update("none");
    return;
  }
  charts.equity = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Bankroll USDC",
        data: values,
        borderColor: "#3b82f6",
        backgroundColor: "rgba(59,130,246,0.1)",
        tension: 0.2,
        pointRadius: 0,
      }],
    },
    options: { responsive: true, animation: false, plugins: { legend: { display: false } } },
  });
}

function drawDrawdown(series) {
  const ctx = document.getElementById("dd-chart");
  if (!ctx || !series) return;
  const labels = series.map((p) => new Date(p.ts * 1000).toLocaleString());
  const values = series.map((p) => p.drawdown_pct * 100);
  if (charts.dd) {
    charts.dd.data.labels = labels;
    charts.dd.data.datasets[0].data = values;
    charts.dd.update("none");
    return;
  }
  charts.dd = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Drawdown %",
        data: values,
        borderColor: "#f87171",
        backgroundColor: "rgba(248,113,113,0.18)",
        fill: true,
        tension: 0.2,
        pointRadius: 0,
      }],
    },
    options: { responsive: true, animation: false, plugins: { legend: { display: false } },
              scales: { y: { reverse: true } } },
  });
}

// ─── per-city ────────────────────────────────────────────────────────

let _perCityEverHadData = false;

function renderPerCity(data) {
  const grid = document.getElementById("per-city-grid");
  if (!grid) return;
  if (!data || !data.cells || data.cells.length === 0) {
    if (!_perCityEverHadData) {
      grid.innerHTML = "";
      showError("per-city-empty", "No markets evaluated yet — bot has been running for <1 cycle.");
    }
    return;
  }
  _perCityEverHadData = true;
  hideError("per-city-empty");
  grid.innerHTML = "";
  for (const cell of data.cells) {
    const div = document.createElement("div");
    div.className = "heatmap-cell";
    const edge = Number(cell.edge_bps || 0);
    const intensity = Math.min(1, Math.abs(edge) / 1200);
    const color = edge >= 0
      ? `rgba(52,211,153,${0.15 + intensity * 0.7})`
      : `rgba(248,113,113,${0.15 + intensity * 0.7})`;
    div.style.background = color;
    div.innerHTML = `
      <div class="city">${cell.city}</div>
      <div class="bucket">${fmtNum(cell.bucket_low, 0)}–${fmtNum(cell.bucket_high, 0)}°F</div>
      <div>edge ${fmtNum(edge, 0)}bps</div>
      <div class="muted">p≈${fmtNum(cell.fill_probability, 2)}</div>
    `;
    grid.appendChild(div);
  }
}

// ─── forecasts ───────────────────────────────────────────────────────

let _forecastsEverHadData = false;

function renderForecasts(data) {
  const tbody = document.querySelector("#forecasts-table tbody");
  if (!tbody) return;
  if (!data || !data.markets || data.markets.length === 0) {
    if (!_forecastsEverHadData) {
      tbody.innerHTML = "";
      showError("forecasts-empty", "No forecasts yet.");
    }
    return;
  }
  _forecastsEverHadData = true;
  hideError("forecasts-empty");
  tbody.innerHTML = "";
  for (const m of data.markets) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${m.city || ""}</td>
      <td>${m.market_id || ""}</td>
      <td>${m.strategy || ""}</td>
      <td>${m.decision || ""}</td>
      <td>${fmtNum(m.model_probability, 3)}</td>
      <td>${fmtNum(m.edge_bps, 0)}</td>
      <td>${fmtNum(m.confidence, 2)}</td>
      <td>${fmtNum(m.horizon_hours, 1)}</td>`;
    tbody.appendChild(tr);
  }
}

// ─── risk ────────────────────────────────────────────────────────────

let _riskEverHadData = false;

function renderRisk(data) {
  if (!data) {
    if (!_riskEverHadData) showError("risk-empty", "Risk data unavailable.");
    return;
  }
  _riskEverHadData = true;
  hideError("risk-empty");
  const summary = document.getElementById("risk-summary");
  // Halt line: show a "next attempt in HHmm" countdown for auto-recovering
  // halts (daily / consecutive); call out the ATH kill as a manual reset.
  let haltLine = `<p>Halted: <b class="positive">no</b></p>`;
  if (data.halted) {
    let recovery;
    if (data.halt_recovery_in_seconds === null || data.halt_recovery_in_seconds === undefined) {
      recovery = " — permanent (manual reset required)";
    } else {
      recovery = ` — next attempt in ${fmtDuration(data.halt_recovery_in_seconds)}`;
    }
    haltLine = `<p>Halted: <b class="negative">YES — ${data.halt_reason}</b>${recovery}</p>`;
  }
  summary.innerHTML = `
    <p>Bankroll: <b>${fmtMoney(data.current_bankroll_usdc)}</b> (ATH ${fmtMoney(data.ath_bankroll_usdc)})</p>
    <p>Max drawdown: <b>${fmtPct(data.max_drawdown_pct * 100)}</b></p>
    <p>Open exposure: <b>${fmtMoney(data.open_exposure_usdc)}</b> / cap ${fmtMoney(data.open_exposure_cap_usdc)}</p>
    <p>Consecutive losses: <b>${data.consecutive_losses}</b></p>
    ${haltLine}
  `;
  const ksBody = document.querySelector("#risk-killswitches tbody");
  ksBody.innerHTML = "";
  for (const ks of (data.kill_switches || [])) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${ks.name}</td><td>${ks.current}</td><td>${ks.threshold}</td>
      <td class="${ks.armed ? "negative" : "positive"}">${ks.armed ? "ARMED" : "ok"}</td>`;
    ksBody.appendChild(tr);
  }
  const pnlBody = document.querySelector("#risk-pnl tbody");
  pnlBody.innerHTML = "";
  for (const [k, v] of Object.entries(data.pnl_by_strategy || {})) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${k}</td><td>${fmtMoney(v)}</td>`;
    pnlBody.appendChild(tr);
  }
}

// ─── trades ──────────────────────────────────────────────────────────

let _tradesEverHadData = false;

function renderTrades(data) {
  const tbody = document.querySelector("#trades-table tbody");
  if (!data || !data.trades || data.trades.length === 0) {
    if (!_tradesEverHadData) {
      if (tbody) tbody.innerHTML = "";
      showError("trades-empty", "No trades yet.");
    }
    return;
  }
  _tradesEverHadData = true;
  hideError("trades-empty");
  tbody.innerHTML = "";
  for (const t of data.trades) {
    const tr = document.createElement("tr");
    const ts = new Date(t.timestamp * 1000).toLocaleString();
    const pnl = parseFloat(t.pnl_usdc);
    tr.innerHTML = `
      <td>${ts}</td>
      <td>${t.city || ""}</td>
      <td>${t.strategy || ""}</td>
      <td>${t.side || ""}</td>
      <td>${fmtNum(t.entry_price, 3)}</td>
      <td>${fmtNum(t.exit_price, 3)}</td>
      <td class="${pnl >= 0 ? "positive" : "negative"}">${fmtMoney(pnl)}</td>
      <td>${fmtNum(t.model_probability, 3)}</td>
      <td>${fmtNum(t.fill_latency_seconds, 2)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ─── gate ────────────────────────────────────────────────────────────

function renderGate(data) {
  const banner = document.getElementById("gate-banner");
  const tbody = document.querySelector("#gate-table tbody");
  if (!data) {
    banner.textContent = "Validation gate unavailable.";
    banner.className = "gate-banner fail";
    return;
  }
  banner.textContent = data.ready_for_live ? "READY FOR LIVE: YES" : "READY FOR LIVE: NO";
  banner.className = "gate-banner " + (data.ready_for_live ? "pass" : "fail");
  if (!data.ready_for_live && data.failing && data.failing.length > 0) {
    banner.textContent += " — failing: " + data.failing.join(", ");
  }
  tbody.innerHTML = "";
  for (const [name, c] of Object.entries(data.criteria || {})) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${name}</td>
      <td>${c.value}</td>
      <td>${c.threshold}</td>
      <td class="${c.pass ? "positive" : "negative"}">${c.pass ? "PASS" : "FAIL"}</td>
      <td class="muted">${c.reason}</td>`;
    tbody.appendChild(tr);
  }
}

function fmtTs(ts) {
  if (!ts) return "—";
  const n = typeof ts === "string" ? parseFloat(ts) : ts;
  if (!n || Number.isNaN(n)) return "—";
  return new Date(n * 1000).toLocaleString();
}

function renderWalletWatch(data) {
  if (!data) return;
  const banner = document.getElementById("wallets-banner");
  const sigBody = document.querySelector("#wallets-signals-table tbody");
  const walBody = document.querySelector("#wallets-table tbody");
  const empty = document.getElementById("wallets-empty");
  if (!banner || !sigBody || !walBody) return;

  const wallets = data.wallets || [];
  const signals = data.market_signals || [];
  if (!data.enabled) {
    banner.textContent = "Disabled — no wallets in config/polyweather/wallets.yaml.";
    empty.classList.remove("hidden");
  } else if (!data.polled_at) {
    banner.textContent = "Configured — not polled yet (live-data mode only).";
    empty.classList.add("hidden");
  } else {
    banner.textContent = `${wallets.length} wallet(s) tracked · polled ${fmtTs(data.polled_at)}`;
    empty.classList.add("hidden");
  }

  sigBody.innerHTML = "";
  for (const s of signals) {
    const tr = document.createElement("tr");
    const cls = s.direction === "bullish" ? "positive" : (s.direction === "bearish" ? "negative" : "muted");
    tr.innerHTML = `
      <td>${s.title || s.condition_id}</td>
      <td>${s.wallets}</td>
      <td class="${cls}">${s.direction}</td>
      <td>${fmtMoney(s.net_usdc)}</td>
      <td class="muted">${fmtTs(s.last_trade_ts)}</td>`;
    sigBody.appendChild(tr);
  }

  walBody.innerHTML = "";
  for (const w of wallets) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${w.label || ""}</td>
      <td class="muted">${w.address}</td>
      <td>${w.error ? "—" : w.weather_trades}</td>
      <td>${w.error ? "—" : fmtMoney(w.net_usdc)}</td>
      <td class="muted">${w.error ? ("err: " + w.error) : fmtTs(w.last_trade_ts)}</td>`;
    walBody.appendChild(tr);
  }
}

// ─── refresh loop ────────────────────────────────────────────────────

async function refresh() {
  const [ov, pc, fc, rk, tr, gt, ww] = await Promise.all([
    safeFetch("/api/weather/overview"),
    safeFetch("/api/weather/per-city-edge"),
    safeFetch("/api/weather/forecasts"),
    safeFetch("/api/weather/risk"),
    safeFetch("/api/weather/trades?limit=50"),
    safeFetch("/api/weather/validation-gate"),
    safeFetch("/api/weather/wallet-watch"),
  ]);
  renderOverview(ov);
  renderPerCity(pc);
  renderForecasts(fc);
  renderRisk(rk);
  renderTrades(tr);
  renderGate(gt);
  renderWalletWatch(ww);
}

refresh().catch((e) => console.error(e));
setInterval(refresh, REFRESH_MS);
