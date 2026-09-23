"""
Dashboard web: valore del portafoglio, posizioni nei pool, ultima
scansione (approvati e scartati con il motivo), storico delle operazioni.

Solo libreria standard, come nei bot fratelli. Il pulsante "Esegui ciclo"
e' attivo solo se DASHBOARD_RUN_TOKEN e' impostato: la dashboard non ha
login e un ciclo puo' firmare transazioni.
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

import db_utils  # noqa: E402

PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("PORT", "3000")))
RUN_TOKEN = os.getenv("DASHBOARD_RUN_TOKEN", "")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon-small.svg": ("icon-small.svg", "image/svg+xml"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/static/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
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
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0b0f19; --surface: #131b2e; --border: #1e2942;
    --primary: #10b981; --accent: #14b8a6; --danger: #ef4444; --warning: #f59e0b;
    --text: #f3f4f6; --muted: #9ca3af;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }
  .header { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
            margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
  .header h1 { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
  .header img { width: 32px; height: 32px; }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 9999px; font-weight: 600; border: 1px solid; }
  .b-live { color: var(--danger); background: rgba(239,68,68,.12); }
  .b-paper { color: var(--accent); background: rgba(20,184,166,.12); }
  .b-dry { color: var(--warning); background: rgba(245,158,11,.12); }
  .b-ok { color: var(--primary); background: rgba(16,185,129,.12); }
  .b-no { color: var(--muted); background: rgba(156,163,175,.1); }
  .btn { background: linear-gradient(135deg, var(--primary), var(--accent)); color: #fff; border: 0;
         padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
  .card h3 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }
  .card .value { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .card .sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .section { margin-bottom: 24px; }
  .section h2 { font-size: 16px; font-weight: 600; margin-bottom: 12px; display: flex; gap: 8px; align-items: baseline; }
  .section h2 small { color: var(--muted); font-weight: 400; font-size: 12px; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
       padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  td.num { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }
  td.reason { color: var(--muted); max-width: 420px; }
  .pos { color: var(--primary); } .neg { color: var(--danger); }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tab { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 6px 12px;
         border-radius: 6px; cursor: pointer; font-size: 12px; }
  .tab.active { color: var(--text); border-color: var(--primary); }
  .empty { color: var(--muted); padding: 16px 10px; font-size: 13px; }
  .chart-box { height: 260px; }
  .footer { color: var(--muted); font-size: 12px; margin-top: 12px; }
  @media (max-width: 600px) { body { padding: 16px; } .card .value { font-size: 22px; } }
</style>
</head>
<body>
<div class="header">
  <h1><img src="/static/icon.svg" alt="">Yield Agent <span class="badge b-no" id="mode">…</span></h1>
  <div style="display:flex;gap:10px;align-items:center">
    <span class="footer" id="updated"></span>
    <button class="btn" id="run" title="Richiede DASHBOARD_RUN_TOKEN">Esegui ciclo ora</button>
  </div>
</div>

<div class="stats">
  <div class="card"><h3>Valore totale</h3><div class="value" id="total">--</div><div class="sub" id="pnl"></div></div>
  <div class="card"><h3>Investito</h3><div class="value" id="invested">--</div><div class="sub" id="idle"></div></div>
  <div class="card"><h3>APY medio ponderato</h3><div class="value" id="apy">--</div><div class="sub" id="yearly"></div></div>
  <div class="card"><h3>Posizioni</h3><div class="value" id="npos">--</div><div class="sub" id="eth"></div></div>
</div>

<div class="section card">
  <h2>Andamento <small>valore totale e capitale investito</small></h2>
  <div class="chart-box"><canvas id="equity"></canvas></div>
</div>

<div class="section card">
  <h2>Posizioni aperte</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Pool</th><th>Asset</th><th>Valore</th><th>Depositato</th><th>Rendimento</th>
      <th>APY ora</th><th>APY ingresso</th><th>TVL</th><th>Aperta da</th></tr></thead>
    <tbody id="positions"></tbody>
  </table></div>
</div>

<div class="section card">
  <h2>Ultima scansione <small id="scanat"></small></h2>
  <div class="tabs">
    <button class="tab active" data-tab="approved">Approvati</button>
    <button class="tab" data-tab="rejected">Scartati</button>
  </div>
  <div class="table-wrap"><table>
    <thead id="pools-head"></thead>
    <tbody id="pools"></tbody>
  </table></div>
</div>

<div class="section card">
  <h2>Operazioni</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Data</th><th>Operazione</th><th>Pool</th><th>Importo</th><th>Esito</th><th>Motivazione</th></tr></thead>
    <tbody id="ops"></tbody>
  </table></div>
</div>

<div class="section card">
  <h2>Errori recenti</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Data</th><th>Tipo</th><th>Messaggio</th></tr></thead>
    <tbody id="errors"></tbody>
  </table></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd = (v, d = 2) => v == null ? '--' : '$' + Number(v).toLocaleString('it-IT', {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (v) => v == null ? '--' : Number(v).toFixed(2) + '%';
const big = (v) => v == null ? '--' : v >= 1e6 ? '$' + (v / 1e6).toFixed(1) + 'M' : usd(v, 0);
const empty = (cols, text) => `<tr><td colspan="${cols}" class="empty">${text}</td></tr>`;
let chart = null, data = null, tab = 'approved';

function renderStatus(s) {
  const modes = {live: ['LIVE', 'b-live'], paper: ['PAPER', 'b-paper'], dry_run: ['DRY-RUN', 'b-dry']};
  const [label, cls] = modes[s?.mode] || ['in attesa del primo ciclo', 'b-no'];
  $('mode').textContent = label; $('mode').className = 'badge ' + cls;
  if (!s) return;
  $('total').textContent = usd(s.total_value_usd);
  $('invested').textContent = usd(s.invested_usd);
  $('idle').textContent = 'inattivo ' + usd(s.idle_usd);
  $('apy').textContent = pct(s.weighted_apy);
  $('yearly').textContent = '~' + usd(s.est_yearly_usd) + ' / anno al tasso attuale';
  $('npos').textContent = (s.positions || []).length;
  $('eth').textContent = 'ETH per il gas: ' + Number(s.eth_balance || 0).toFixed(5);
  if (s.mode === 'paper' && s.pnl_since_start_usd != null) {
    const p = s.pnl_since_start_usd;
    $('pnl').innerHTML = `<span class="${p >= 0 ? 'pos' : 'neg'}">${p >= 0 ? '+' : ''}${usd(p)}</span> dall'inizio (paper)`;
  }
  const rows = (s.positions || []).map(p => {
    const gain = p.pnl_usd;
    return `<tr><td>${esc(p.project)} <b>${esc(p.symbol)}</b>${p.pool_meta ? ' <small>' + esc(p.pool_meta) + '</small>' : ''}</td>
      <td>${esc(p.asset)}</td><td class="num">${usd(p.value_usd)}</td><td class="num">${usd(p.deposited_usd)}</td>
      <td class="num ${gain >= 0 ? 'pos' : 'neg'}">${gain == null ? '--' : (gain >= 0 ? '+' : '') + usd(gain, 4)}</td>
      <td class="num">${pct(p.apy_now)}</td><td class="num">${pct(p.entry_apy)}</td>
      <td class="num">${big(p.tvl_now)}</td><td class="num">${Number(p.held_hours || 0).toFixed(0)}h</td></tr>`;
  });
  $('positions').innerHTML = rows.join('') || empty(9, 'Nessuna posizione aperta: il capitale e\' in USDC.');
}

function renderPools() {
  const pools = (data?.pools || []).filter(p => tab === 'approved' ? p.status === 'approved' : p.status !== 'approved');
  if (tab === 'approved') {
    $('pools-head').innerHTML = '<tr><th>Pool</th><th>Asset</th><th>APY effettivo</th><th>Base</th><th>Reward</th><th>Media 30g</th><th>TVL</th><th>Esecuzione</th></tr>';
    $('pools').innerHTML = pools.map(p => `<tr><td>${esc(p.project)} <b>${esc(p.symbol)}</b>${p.pool_meta ? ' <small>' + esc(p.pool_meta) + '</small>' : ''}</td>
      <td>${esc(p.asset)}</td><td class="num"><b>${pct(p.effective_apy)}</b></td><td class="num">${pct(p.apy_base)}</td>
      <td class="num">${pct(p.apy_reward)}</td><td class="num">${pct(p.apy_mean_30d)}</td><td class="num">${big(p.tvl_usd)}</td>
      <td>${p.adapter ? '<span class="badge b-ok">' + esc(p.adapter) + '</span>' : '<span class="badge b-no" title="' + esc(p.reason) + '">monitoraggio</span>'}</td></tr>`).join('')
      || empty(8, 'Nessun pool approvato nell\'ultima scansione.');
  } else {
    $('pools-head').innerHTML = '<tr><th>Pool</th><th>APY</th><th>TVL</th><th>Motivo dello scarto</th></tr>';
    $('pools').innerHTML = pools.slice(0, 200).map(p => `<tr><td>${esc(p.project)} <b>${esc(p.symbol)}</b></td>
      <td class="num">${pct(p.apy)}</td><td class="num">${big(p.tvl_usd)}</td><td class="reason">${esc(p.reason)}</td></tr>`).join('')
      || empty(4, 'Nessuno scarto.');
  }
}

function renderChart(points) {
  if (!window.Chart || !points.length) return;
  const labels = points.map(p => p.created_at.replace('T', ' ').slice(5, 16));
  const cfg = {
    type: 'line',
    data: { labels, datasets: [
      { label: 'Totale', data: points.map(p => p.total_value_usd), borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,.08)', fill: true, tension: .25, pointRadius: 0 },
      { label: 'Investito', data: points.map(p => p.invested_usd), borderColor: '#14b8a6', borderDash: [4, 4], tension: .25, pointRadius: 0 },
    ]},
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { color: '#9ca3af' } } },
      scales: { x: { ticks: { color: '#9ca3af', maxTicksLimit: 8 }, grid: { color: '#1e2942' } },
                y: { ticks: { color: '#9ca3af' }, grid: { color: '#1e2942' } } } },
  };
  if (chart) { chart.data = cfg.data; chart.update('none'); } else { chart = new Chart($('equity'), cfg); }
}

function renderOps(ops) {
  $('ops').innerHTML = ops.map(o => {
    const st = o.status || '';
    const cls = ['success', 'paper'].includes(st) ? 'b-ok' : st === 'dry_run' ? 'b-dry' : ['rejected', 'error'].includes(st) ? 'b-live' : 'b-no';
    return `<tr><td class="num">${esc((o.created_at || '').replace('T', ' ').slice(0, 16))}</td>
      <td><b>${esc((o.operation || '').toUpperCase())}</b>${o.trigger ? ' <small>' + esc(o.trigger) + '</small>' : ''}</td>
      <td>${esc(o.project || '')} ${esc(o.symbol || '')}</td><td class="num">${usd(o.amount_usd)}</td>
      <td><span class="badge ${cls}">${esc(st)}</span></td>
      <td class="reason">${esc(o.llm_reason || '')}${o.result_reason ? '<br><span class="neg">' + esc(o.result_reason) + '</span>' : ''}</td></tr>`;
  }).join('') || empty(6, 'Nessuna operazione registrata.');
}

async function load() {
  try {
    data = await (await fetch('/api/data')).json();
  } catch (e) { $('updated').textContent = 'dashboard non raggiungibile'; return; }
  renderStatus(data.status);
  renderChart(data.equity || []);
  renderPools();
  renderOps(data.operations || []);
  $('errors').innerHTML = (data.errors || []).map(e => `<tr><td class="num">${esc((e.created_at || '').replace('T', ' ').slice(0, 16))}</td>
    <td>${esc(e.error_type)}</td><td class="reason">${esc(e.message)}</td></tr>`).join('') || empty(3, 'Nessun errore.');
  $('scanat').textContent = data.scan_at ? data.scan_at.replace('T', ' ').slice(0, 16) + ' UTC' : '';
  $('updated').textContent = data.snapshot_at ? 'aggiornato ' + data.snapshot_at.replace('T', ' ').slice(0, 16) + ' UTC' : '';
  $('run').disabled = !data.run_enabled;
}

document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b));
  tab = b.dataset.tab; renderPools();
}));

$('run').addEventListener('click', async () => {
  let token = '';
  try { token = localStorage.getItem('runToken') || ''; } catch (e) {}
  if (!token) token = prompt('Token di esecuzione (DASHBOARD_RUN_TOKEN):') || '';
  if (!token) return;
  const r = await fetch('/api/run', { method: 'POST', headers: { 'X-Run-Token': token } });
  const body = await r.json().catch(() => ({}));
  if (r.ok) { try { localStorage.setItem('runToken', token); } catch (e) {} }
  else if (r.status === 403) { try { localStorage.removeItem('runToken'); } catch (e) {} }
  alert(body.message || r.statusText);
});

load();
setInterval(load, 30000);
</script>
</body>
</html>
"""


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
        elif path == "/api/data":
            try:
                data = db_utils.fetch_dashboard_data()
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
            data["run_enabled"] = bool(RUN_TOKEN)
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

    def do_POST(self):
        if urlparse(self.path).path != "/api/run":
            self.send_response(404)
            self.end_headers()
            return
        if not RUN_TOKEN:
            self._json(403, {"message": "Esecuzione da dashboard disattivata: imposta DASHBOARD_RUN_TOKEN."})
            return
        if not hmac.compare_digest(self.headers.get("X-Run-Token", ""), RUN_TOKEN):
            self._json(403, {"message": "Token non valido."})
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
