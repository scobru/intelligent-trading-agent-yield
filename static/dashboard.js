/*
 * Helper condivisi dalle dashboard dei quattro agenti. Identico nei quattro
 * repository, come dashboard.css: se lo modifichi, copialo negli altri tre.
 *
 * Ogni backend espone in /api/... un blocco "meta" con lo stesso schema:
 *   { mode: 'live'|'paper'|'dry_run'|null, updated_at, run_enabled,
 *     paper: { initial_usd, value_usd, pnl_usd, operations, costs_usd,
 *              costs_label, started_at, extra: [[etichetta, valore], ...], note },
 *     wallet: { address, eth, eth_usd, min_eth, warn_eth, extra: [[etichetta, valore], ...] } }
 * e questi helper disegnano header, modalita', pannello paper ed errori
 * sempre allo stesso modo.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const isNum = (v) => v !== null && v !== undefined && v !== '' && !isNaN(Number(v));

  const usd = (v, d = 2) => !isNum(v) ? '--'
    : '$' + Number(v).toLocaleString('it-IT', {minimumFractionDigits: d, maximumFractionDigits: d});
  const signedUsd = (v, d = 2) => !isNum(v) ? '--' : (Number(v) >= 0 ? '+' : '-') + usd(Math.abs(v), d);
  const pct = (v, d = 2) => !isNum(v) ? '--' : Number(v).toFixed(d) + '%';
  const signedPct = (v, d = 2) => !isNum(v) ? '--' : (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(d) + '%';
  const big = (v) => !isNum(v) ? '--' : Math.abs(v) >= 1e9 ? '$' + (v / 1e9).toFixed(2) + 'B'
    : Math.abs(v) >= 1e6 ? '$' + (v / 1e6).toFixed(1) + 'M'
    : Math.abs(v) >= 1e3 ? '$' + (v / 1e3).toFixed(1) + 'K' : usd(v, 0);
  // prezzi adattivi: le meme costano 0.00000012 e non devono diventare "$0.00"
  const price = (v) => {
    if (!isNum(v) || Number(v) === 0) return '--';
    const a = Math.abs(v);
    if (a >= 1000) return usd(v, 2);
    if (a >= 1) return '$' + Number(v).toFixed(4);
    if (a >= 0.0001) return '$' + Number(v).toFixed(6);
    return '$' + Number(v).toExponential(3);
  };
  const cls = (v) => !isNum(v) ? '' : Number(v) >= 0 ? 'pos' : 'neg';
  // timestamp SQLite ("2026-09-23 10:15:00") o ISO -> "09-23 10:15"
  const time = (s) => s ? String(s).replace('T', ' ').slice(5, 16) : '--';
  const empty = (cols, text) => `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function since(epochSeconds) {
    if (!isNum(epochSeconds)) return '--';
    const h = (Date.now() / 1000 - Number(epochSeconds)) / 3600;
    if (h < 1) return Math.max(1, Math.round(h * 60)) + ' min';
    if (h < 48) return Math.round(h) + ' h';
    return Math.round(h / 24) + ' giorni';
  }

  // Stato di un'operazione -> badge coerente in tutte le dashboard
  function statusBadge(status) {
    const s = String(status || '').toLowerCase();
    const map = {
      success: 'b-ok', filled: 'b-ok', paper: 'b-paper', dry_run: 'b-dry', hold: 'b-no',
      rejected: 'b-bad', error: 'b-bad', failed: 'b-bad', skipped: 'b-no',
    };
    return `<span class="badge ${map[s] || 'b-no'}">${esc(s || '--')}</span>`;
  }

  function sideBadge(side) {
    const s = String(side || '').toLowerCase();
    const c = ['long', 'buy', 'open', 'deposit'].includes(s) ? 'b-ok'
      : ['short', 'sell', 'close', 'withdraw'].includes(s) ? 'b-bad' : 'b-no';
    return `<span class="badge ${c}">${esc(s.toUpperCase() || '--')}</span>`;
  }

  const MODES = {
    live: ['● LIVE', 'b-live', 'Transazioni reali firmate dal wallet'],
    paper: ['📝 PAPER', 'b-paper', 'Portafoglio virtuale: prezzi reali, esecuzione simulata'],
    dry_run: ['🧪 DRY-RUN', 'b-dry', 'Decide e valida, ma non firma nulla'],
  };

  function renderMeta(meta) {
    meta = meta || {};
    const [label, klass, title] = MODES[meta.mode] || ['in attesa del primo ciclo', 'b-no', ''];
    const badge = $('mode');
    if (badge) { badge.textContent = label; badge.className = 'badge ' + klass; badge.title = title; }
    if ($('updated')) $('updated').textContent = meta.updated_at ? 'aggiornato ' + time(meta.updated_at) + ' UTC' : '';
    if ($('run')) {
      $('run').disabled = !meta.run_enabled;
      $('run').title = meta.run_enabled ? '' : 'Disattivato: imposta DASHBOARD_RUN_TOKEN';
    }
    renderPaper(meta.mode === 'paper' ? meta.paper : null);
    // in paper il gas e' virtuale: il wallet vero non va tenuto d'occhio
    renderWallet(meta.mode === 'paper' ? null : meta.wallet);
  }

  // Wallet del bot: ETH per il gas, con avviso quando va ricaricato
  function renderWallet(w) {
    const panel = $('wallet-panel');
    if (!panel) return;
    panel.hidden = !w || !isNum(w.eth);
    if (panel.hidden) return;
    const eth = Number(w.eth);
    let state = ['b-ok', 'OK', ''];
    if (isNum(w.min_eth) && eth < Number(w.min_eth)) {
      state = ['b-bad', 'RICARICA ORA', `Sotto la riserva minima di ${w.min_eth} ETH: il bot non riesce a pagare il gas.`];
    } else if (isNum(w.warn_eth) && eth < Number(w.warn_eth)) {
      state = ['b-warn', 'IN ESAURIMENTO', `Sotto ${w.warn_eth} ETH: ricarica presto per non fermare il bot.`];
    }
    panel.classList.toggle('low', state[0] !== 'b-ok');
    const short = w.address ? String(w.address).slice(0, 6) + '…' + String(w.address).slice(-4) : '--';
    const link = /^0x[0-9a-fA-F]{40}$/.test(w.address || '')
      ? `<a href="https://basescan.org/address/${esc(w.address)}" target="_blank" rel="noopener" class="mono">${esc(short)}</a>`
      : `<span class="mono">${esc(short)}</span>`;
    const items = [
      ['⛽ Gas (ETH)', `${eth.toFixed(5)} <small>${isNum(w.eth_usd) ? '≈ ' + usd(w.eth_usd) : ''}</small>`],
      ...(w.extra || []).map(([k, v]) => [k, esc(v)]),
      ['Wallet', link],
    ];
    $('wallet-items').innerHTML = items.map(([k, v]) =>
      `<div class="wallet-item"><span>${esc(k)}</span><b>${v}</b></div>`).join('')
      + `<div class="wallet-item"><span>Stato</span><b><span class="badge ${state[0]}">${state[1]}</span></b></div>`;
    $('wallet-note').textContent = state[2];
    $('wallet-note').hidden = !state[2];
  }

  function renderPaper(p) {
    const panel = $('paper-panel');
    if (!panel) return;
    panel.hidden = !p;
    if (!p) return;
    const pnlPct = isNum(p.initial_usd) && Number(p.initial_usd) > 0 && isNum(p.pnl_usd)
      ? Number(p.pnl_usd) / Number(p.initial_usd) * 100 : null;
    const cells = [
      ['Capitale iniziale', usd(p.initial_usd)],
      ['Valore attuale', usd(p.value_usd)],
      ['P&L', `<span class="${cls(p.pnl_usd)}">${signedUsd(p.pnl_usd)} <small>${signedPct(pnlPct)}</small></span>`],
      ['Operazioni simulate', isNum(p.operations) ? p.operations : '--'],
      [p.costs_label || 'Costi simulati', usd(p.costs_usd, 2)],
      ['Attivo da', since(p.started_at)],
      ...(p.extra || []).map(([k, v]) => [k, esc(v)]),
    ];
    $('paper-grid').innerHTML = cells.map(([k, v]) =>
      `<div class="cell"><span>${esc(k)}</span><b>${v}</b></div>`).join('');
    if ($('paper-note')) $('paper-note').textContent = p.note || '';
  }

  function renderErrors(errors, tbodyId = 'errors') {
    $(tbodyId).innerHTML = (errors || []).map(e => `<tr>
      <td class="num">${esc(time(e.created_at))}</td>
      <td>${esc(e.error_type || e.source || '--')}</td>
      <td class="reason">${esc(e.message || e.error_message || '')}</td></tr>`).join('')
      || empty(3, 'Nessun errore registrato.');
  }

  // Esecuzione manuale protetta da token (DASHBOARD_RUN_TOKEN)
  function setupRun(onDone) {
    const btn = $('run');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      let token = '';
      try { token = localStorage.getItem('runToken') || ''; } catch (e) {}
      if (!token) token = prompt('Token di esecuzione (DASHBOARD_RUN_TOKEN):') || '';
      if (!token) return;
      const r = await fetch('/api/run', { method: 'POST', headers: { 'X-Run-Token': token } });
      const body = await r.json().catch(() => ({}));
      if (r.ok) { try { localStorage.setItem('runToken', token); } catch (e) {} }
      else if (r.status === 403) { try { localStorage.removeItem('runToken'); } catch (e) {} }
      alert(body.message || r.statusText);
      if (r.ok && onDone) setTimeout(onDone, 3000);
    });
  }

  // Tabs generici: <div class="tabs" data-tabs="gruppo"><button class="tab" data-tab="x">
  function setupTabs(group, onChange) {
    const buttons = document.querySelectorAll(`[data-tabs="${group}"] .tab`);
    buttons.forEach(b => b.addEventListener('click', () => {
      buttons.forEach(x => x.classList.toggle('active', x === b));
      onChange(b.dataset.tab);
    }));
  }

  // Grafico a linee con i colori del progetto (--primary / --accent)
  function lineChart(existing, canvas, labels, series) {
    if (!window.Chart || !canvas) return existing;
    const palette = [cssVar('--primary'), cssVar('--accent'), cssVar('--paper')];
    const datasets = series.map((s, i) => ({
      label: s.label, data: s.data, borderColor: palette[i % palette.length],
      backgroundColor: i === 0 ? palette[0] + '22' : 'transparent', fill: i === 0,
      borderWidth: i === 0 ? 2.5 : 1.5, borderDash: i === 0 ? [] : [4, 4],
      tension: .25, pointRadius: labels.length > 60 ? 0 : 2,
    }));
    if (existing) {
      existing.data.labels = labels; existing.data.datasets = datasets; existing.update('none');
      return existing;
    }
    const muted = cssVar('--muted'), grid = cssVar('--border');
    return new Chart(canvas, {
      type: 'line', data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: muted, font: { family: 'Inter' } } },
          tooltip: { callbacks: { label: (c) => ' ' + c.dataset.label + ': ' + usd(c.parsed.y) } },
        },
        scales: {
          x: { ticks: { color: muted, maxTicksLimit: 8 }, grid: { color: grid } },
          y: { ticks: { color: muted, callback: (v) => '$' + v }, grid: { color: grid } },
        },
      },
    });
  }

  window.ITA = { $, esc, isNum, usd, signedUsd, pct, signedPct, big, price, cls, time, empty, cssVar,
                 since, statusBadge, sideBadge, renderMeta, renderPaper, renderWallet, renderErrors, setupRun,
                 setupTabs, lineChart };
})();
