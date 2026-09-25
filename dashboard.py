"""
Dashboard web: valore del portafoglio, posizioni nei pool, ultima
scansione (approvati e scartati con il motivo), storico delle operazioni.

Solo libreria standard, come nei bot fratelli; stile e helper arrivano
da static/dashboard.css e static/dashboard.js, identici nei tre
repository. Il pulsante "Esegui ciclo" e' attivo solo se
DASHBOARD_RUN_TOKEN e' impostato: la dashboard non ha login e un ciclo
puo' firmare transazioni.
"""

import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

import config  # noqa: E402
import db_utils  # noqa: E402

PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("PORT", "3000")))
RUN_TOKEN = os.getenv("DASHBOARD_RUN_TOKEN", "")
# Sotto MIN_ETH_RESERVE il bot non opera; sotto GAS_WARN_ETH la dashboard e
# Telegram chiedono di ricaricare il wallet
GAS_WARN_ETH = float(os.getenv("GAS_WARN_ETH", "0.002"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon-small.svg": ("icon-small.svg", "image/svg+xml"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/static/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}

_run_lock = threading.Lock()

HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Yield Agent</title>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="theme-color" content="#10b981">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dashboard.css?v=3">
<style>:root { --primary: #10b981; --accent: #14b8a6; }</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="/static/dashboard.js?v=3"></script>
</head>
<body>
<header class="header">
  <div class="brand">
    <img src="/static/icon.svg" alt="">
    <div>
      <h1>Yield Agent <span class="badge b-no" id="mode">…</span></h1>
      <p class="tagline">Rendimento passivo su Base • DefiLlama • OpenRouter • SQLite</p>
    </div>
  </div>
  <div class="header-actions">
    <span class="updated" id="updated"></span>
    <button class="btn" id="run">⚡ Esegui ciclo ora</button>
  </div>
</header>

<section class="card paper-panel" id="paper-panel" hidden>
  <div class="card-head"><h2>📝 Paper trading <small>portafoglio virtuale, rendimenti reali</small></h2></div>
  <div class="paper-grid" id="paper-grid"></div>
  <p class="note" id="paper-note"></p>
</section>

<section class="card wallet-bar" id="wallet-panel" hidden>
  <div class="wallet-items" id="wallet-items"></div>
  <p class="note" id="wallet-note" hidden></p>
</section>

<section class="stats">
  <div class="card"><h3>Valore totale</h3><div class="value" id="total">--</div><div class="sub" id="total-sub"></div></div>
  <div class="card"><h3>Posizioni aperte</h3><div class="value" id="npos">--</div><div class="sub" id="npos-sub"></div></div>
  <div class="card"><h3>APY medio ponderato</h3><div class="value" id="apy">--</div><div class="sub" id="yearly"></div></div>
  <div class="card"><h3>Ultima decisione</h3><div class="value" id="last-op">--</div><div class="sub" id="last-op-time">nessuna operazione registrata</div></div>
</section>

<section class="card section">
  <div class="card-head">
    <div class="tabs" data-tabs="chart"><button class="tab active" data-tab="equity">💼 Andamento capitale</button></div>
  </div>
  <div class="chart-box tall"><canvas id="equity"></canvas></div>
</section>

<div class="grid-2">
  <section class="card">
    <div class="card-head"><h2>🏦 Posizioni aperte</h2><small id="pos-note">vault e lending su Base</small></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Pool</th><th>Asset</th><th>Valore</th><th>Depositato</th><th>Rendimento</th><th>APY ora</th><th>APY ingresso</th><th>TVL</th><th>Aperta da</th></tr></thead>
      <tbody id="positions"></tbody>
    </table></div>
  </section>
  <section class="card">
    <div class="card-head"><h2>🧠 Ultima decisione AI</h2></div>
    <div class="decision-box">
      <div class="title" id="ai-action">In attesa del primo ciclo...</div>
      <div class="desc" id="ai-reason">L'agente elaborerà i pool al prossimo intervallo o con "Esegui ciclo ora".</div>
    </div>
    <div class="kv">
      <div><b>Modello:</b> OpenRouter</div>
      <div><b>Input:</b> scansione pool DefiLlama + posizioni + limiti di rischio</div>
    </div>
  </section>
</div>

<h2 class="section-title">🔎 Universo dei pool <small>screening dell'ultima scansione DefiLlama</small></h2>
<section class="card section">
  <div class="card-head">
    <div class="tabs" data-tabs="pools">
      <button class="tab active" data-tab="approved">Approvati</button>
      <button class="tab" data-tab="rejected">Scartati</button>
    </div>
    <small id="scanat"></small>
  </div>
  <div class="table-wrap"><table>
    <thead id="pools-head"></thead>
    <tbody id="pools"></tbody>
  </table></div>
</section>

<section class="card section">
  <div class="card-head"><h2>📜 Storico operazioni</h2></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Data (UTC)</th><th>Operazione</th><th>Pool</th><th>Importo</th><th>Esito</th><th>Motivazione</th></tr></thead>
    <tbody id="ops"></tbody>
  </table></div>
</section>

<section class="card section">
  <div class="card-head"><h2>⚠️ Errori recenti</h2></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Data (UTC)</th><th>Tipo</th><th>Messaggio</th></tr></thead>
    <tbody id="errors"></tbody>
  </table></div>
</section>

<footer class="footer">Yield Agent • Rendimento passivo su Base (DefiLlama) &amp; OpenRouter • CapRover &amp; Docker</footer>

<script>
const { $, esc, usd, signedUsd, pct, big, cls, time, empty, sideBadge, statusBadge } = ITA;
let chart = null, data = null, tab = 'approved';
const poolName = (p) => `${esc(p.project)} <b>${esc(p.symbol)}</b>${p.pool_meta ? ' <small>' + esc(p.pool_meta) + '</small>' : ''}${p.is_loop ? ' <span class="badge b-ok">LOOP ' + (p.leverage ? Number(p.leverage).toFixed(1) + 'x' : '') + (p.health_factor ? ' • HF ' + Number(p.health_factor).toFixed(2) : '') + '</span>' : ''}`;

function renderStatus(s) {
  if (!s) { $('positions').innerHTML = empty(9, 'In attesa del primo ciclo.'); return; }
  $('total').textContent = usd(s.total_value_usd);
  const pnl = s.pnl_since_start_usd;
  $('total-sub').innerHTML = s.mode === 'paper' && pnl != null
    ? `<span class="${cls(pnl)}">${signedUsd(pnl)}</span> dall'inizio (paper)`
    : `investito ${usd(s.invested_usd)} • inattivo ${usd(s.idle_usd)}`;
  const pos = s.positions || [];
  $('npos').textContent = pos.length;
  $('npos-sub').textContent = 'ETH per il gas: ' + Number(s.eth_balance || 0).toFixed(5);
  $('apy').textContent = pct(s.weighted_apy);
  $('yearly').textContent = '~' + usd(s.est_yearly_usd) + ' / anno al tasso attuale';
  $('pos-note').textContent = s.mode === 'paper' ? 'portafoglio virtuale' : 'vault e lending su Base';
  $('positions').innerHTML = pos.map(p => `<tr><td>${poolName(p)}</td>
    <td>${esc(p.asset)}</td><td class="num">${usd(p.value_usd)}</td><td class="num">${usd(p.deposited_usd)}</td>
    <td class="num ${cls(p.pnl_usd)}">${signedUsd(p.pnl_usd, 4)}</td>
    <td class="num">${pct(p.apy_now)}</td><td class="num">${pct(p.entry_apy)}</td>
    <td class="num">${big(p.tvl_now)}</td><td class="num">${Number(p.held_hours || 0).toFixed(0)}h</td></tr>`).join('')
    || empty(9, 'Nessuna posizione aperta: il capitale è in USDC.');
}

function renderPools() {
  const pools = (data?.pools || []).filter(p => tab === 'approved' ? p.status === 'approved' : p.status !== 'approved');
  if (tab === 'approved') {
    $('pools-head').innerHTML = '<tr><th>Pool</th><th>Asset</th><th>APY effettivo</th><th>Base</th><th>Reward</th><th>Media 30g</th><th>TVL</th><th>Esecuzione</th></tr>';
    $('pools').innerHTML = pools.map(p => `<tr><td>${poolName(p)}</td>
      <td>${esc(p.asset)}</td><td class="num"><b>${pct(p.effective_apy)}</b></td><td class="num">${pct(p.apy_base)}</td>
      <td class="num">${pct(p.apy_reward)}</td><td class="num">${pct(p.apy_mean_30d)}</td><td class="num">${big(p.tvl_usd)}</td>
      <td>${p.adapter ? '<span class="badge b-ok">' + esc(p.adapter) + '</span>' : '<span class="badge b-no" title="' + esc(p.reason) + '">monitoraggio</span>'}</td></tr>`).join('')
      || empty(8, "Nessun pool approvato nell'ultima scansione.");
  } else {
    $('pools-head').innerHTML = '<tr><th>Pool</th><th>APY</th><th>TVL</th><th>Motivo dello scarto</th></tr>';
    $('pools').innerHTML = pools.slice(0, 200).map(p => `<tr><td>${esc(p.project)} <b>${esc(p.symbol)}</b></td>
      <td class="num">${pct(p.apy)}</td><td class="num">${big(p.tvl_usd)}</td><td class="reason">${esc(p.reason)}</td></tr>`).join('')
      || empty(4, 'Nessuno scarto.');
  }
}

function renderChart(points) {
  chart = ITA.lineChart(chart, $('equity'), points.map(p => time(p.created_at)), [
    { label: 'Valore totale', data: points.map(p => p.total_value_usd) },
    { label: 'Investito', data: points.map(p => p.invested_usd) },
  ]);
}

function renderOps(ops) {
  const last = ops[0];
  if (last) {
    const label = `${(last.operation || '').toUpperCase()} ${last.symbol || ''}`;
    $('last-op').textContent = (last.operation || '--').toUpperCase();
    $('last-op-time').textContent = time(last.created_at) + ' UTC' + (last.symbol ? ' • ' + last.symbol : '');
    $('ai-action').textContent = label;
    $('ai-reason').textContent = last.llm_reason || last.result_reason || 'Nessuna spiegazione salvata.';
  }
  $('ops').innerHTML = ops.map(o => `<tr><td class="num">${esc(time(o.created_at))}</td>
    <td>${sideBadge(o.operation)}${o.trigger ? ' <small>' + esc(o.trigger) + '</small>' : ''}</td>
    <td>${esc(o.project || '')} <b>${esc(o.symbol || '')}</b></td><td class="num">${usd(o.amount_usd)}</td>
    <td>${statusBadge(o.status)}</td>
    <td class="reason">${esc(o.llm_reason || '')}${o.result_reason ? '<br><span class="neg">' + esc(o.result_reason) + '</span>' : ''}</td></tr>`).join('')
    || empty(6, 'Nessuna operazione registrata.');
}

async function load() {
  try { data = await (await fetch('/api/data')).json(); }
  catch (e) { $('updated').textContent = 'dashboard non raggiungibile'; return; }
  ITA.renderMeta(data.meta);
  renderStatus(data.status);
  renderChart(data.equity || []);
  renderPools();
  renderOps(data.operations || []);
  ITA.renderErrors(data.errors);
  $('scanat').textContent = data.scan_at ? 'scansione ' + time(data.scan_at) + ' UTC' : '';
}

ITA.setupTabs('pools', (t) => { tab = t; renderPools(); });
ITA.setupRun(load);
load();
setInterval(load, 30000);
</script>
</body>
</html>
"""


def build_meta(data):
    """Blocco "meta" nello schema comune alle dashboard dei tre agenti."""
    status = data.get("status") or {}
    mode = status.get("mode")
    if not mode:
        mode = "paper" if config.PAPER_TRADING else ("dry_run" if config.DRY_RUN else None)
    paper = None
    if mode == "paper":
        p = status.get("paper") or {}
        paper = {
            "initial_usd": p.get("initial_usdc"),
            "value_usd": status.get("total_value_usd"),
            "pnl_usd": status.get("pnl_since_start_usd"),
            "operations": p.get("operations"),
            "costs_usd": p.get("gas_spent_usd"),
            "costs_label": "Gas simulato",
            "started_at": p.get("created_at"),
            "extra": [
                ["Rendimento maturato", f"${float(p.get('yield_earned_usd') or 0.0):,.4f}"],
                ["Investito", f"${float(p.get('invested_usd') or 0.0):,.2f}"],
            ],
            "note": "I depositi maturano l'APY reale corrente di ogni pool (DefiLlama). Posizioni tenute "
                    "in USD: per vault su WETH/cbBTC non si simula l'oscillazione di prezzo; gas forfettario.",
        }
    wallet = None
    if status.get("eth_balance") is not None:
        eth = float(status["eth_balance"])
        prices = status.get("prices") or {}
        eth_px = prices.get("WETH") or prices.get("ETH")
        wallet = {
            "address": status.get("wallet"),
            "eth": eth,
            "eth_usd": eth * float(eth_px) if eth_px else None,
            "min_eth": config.MIN_ETH_RESERVE,
            "warn_eth": GAS_WARN_ETH,
            "extra": [["USDC nel wallet", f"${float(status.get('usdc_balance') or 0):,.2f}"]],
        }
    return {"mode": mode, "updated_at": data.get("snapshot_at"),
            "run_enabled": bool(RUN_TOKEN), "paper": paper, "wallet": wallet}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, code: int, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/data", "/api/status"):
            try:
                data = db_utils.fetch_dashboard_data()
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
            data["run_enabled"] = bool(RUN_TOKEN)
            data["meta"] = build_meta(data)
            data["is_paused"] = db_utils.is_bot_paused()
            data["pause_info"] = db_utils.get_pause_info()
            self._json(200, data)
        elif path == "/health":
            self._json(200, {"status": "healthy", "service": "yield-dashboard"})
        elif path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
            try:
                with open(os.path.join(STATIC_DIR, filename), "rb") as fh:
                    payload = fh.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def _is_auth_valid(self) -> bool:
        if not RUN_TOKEN:
            return False
        token = self.headers.get("X-Run-Token", "") or self.headers.get("X-Admin-Token", "")
        if not token and "Authorization" in self.headers:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            else:
                token = auth.strip()
        return bool(token and hmac.compare_digest(token, RUN_TOKEN))

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/pause", "/api/resume", "/api/release_funds"):
            self.send_response(404)
            self.end_headers()
            return

        if not self._is_auth_valid():
            self._json(403, {"message": "Token non valido o DASHBOARD_RUN_TOKEN non configurato."})
            return

        if path == "/api/pause":
            reason = "Pausa richiesta da API"
            try:
                clen = int(self.headers.get("Content-Length", 0))
                if clen > 0:
                    body = json.loads(self.rfile.read(clen).decode("utf-8"))
                    reason = body.get("reason", reason)
            except Exception:
                pass
            db_utils.set_bot_paused(True, reason=reason)
            self._json(200, {"status": "success", "is_paused": True, "message": f"Bot in pausa: {reason}"})
            return

        if path == "/api/resume":
            db_utils.set_bot_paused(False)
            self._json(200, {"status": "success", "is_paused": False, "message": "Bot riattivato con successo."})
            return

        if path == "/api/run":
            if db_utils.is_bot_paused():
                pinfo = db_utils.get_pause_info()
                self._json(200, {
                    "status": "paused",
                    "is_paused": True,
                    "message": f"Bot attualmente in PAUSA ({pinfo.get('reason', 'Pausa attiva')}). Ciclo ignorato."
                })
                return

            if not _run_lock.acquire(blocking=False):
                self._json(409, {"message": "Un ciclo e' gia' in corso."})
                return

            def _run():
                try:
                    subprocess.run([sys.executable, "main.py"], check=False)
                finally:
                    _run_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            self._json(200, {"message": "Ciclo avviato: la dashboard si aggiorna da sola."})

        if path == "/api/release_funds":
            target_amount = 0.0
            try:
                clen = int(self.headers.get("Content-Length", 0))
                if clen > 0:
                    body = json.loads(self.rfile.read(clen).decode("utf-8"))
                    target_amount = float(body.get("amount_usd", 0.0) or 0.0)
            except Exception:
                pass
            try:
                from base_client import BaseClient
                from yield_manager import YieldManager
                client = BaseClient()
                manager = YieldManager(client)
                res = manager.release_funds(target_usdc=target_amount)
                self._json(200, res)
            except Exception as exc:
                self._json(500, {"status": "error", "message": str(exc)})
            return


def run_dashboard(port: int = PORT):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"🌐 Dashboard attiva su http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_dashboard()
