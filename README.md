<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Yield Agent (Base passive yield)

<br clear="left">

Agente che cerca **rendimento passivo su Base**: trova i mercati di lending e
i vault che pagano interesse (Aave, Compound, Morpho, Moonwell, Fluid,
Euler...), ci deposita il capitale inattivo e lo sposta solo quando un pool
migliore ripaga davvero il costo dello spostamento.

È il terzo fratello della famiglia:

| Bot | Cosa fa |
|---|---|
| [intelligent-trading-agent](https://github.com/scobru/intelligent-trading-agent) | perpetual su SynFutures V3 |
| [intelligent-trading-agent-degen](https://github.com/scobru/intelligent-trading-agent-degen) | spot di token su Uniswap V3 (Base) |
| **intelligent-trading-agent-yield** | rendimento passivo su lending e vault (Base) |

Struttura, dashboard, notifiche Telegram, modalità dry-run/paper e ciclo
decisionale LLM sono gli stessi. Cambia l'obiettivo: qui non si specula sul
prezzo, si massimizza l'interesse aggiustato per il rischio.

---

## 🧠 Come funziona un ciclo

1. **Scoperta** — tutti i pool di Base da [DefiLlama Yields](https://defillama.com/yields)
   (APY base, APY da ricompense, media a 30 giorni, TVL, esposizione, rischio IL).

2. **Screening** — resta solo ciò che è:
   - di un protocollo in allowlist (lending e vault con storia);
   - single-asset e senza impermanent loss (niente LP);
   - su un asset che il bot sa detenere (`DEPOSIT_ASSETS`, default solo USDC);
   - con TVL, storico e APY credibili, non gonfiati da emissioni o da un outlier.

   Ogni scarto conserva il motivo e finisce in dashboard.

3. **Riconoscimento on-chain** — l'id DefiLlama non basta per sapere dove
   depositare. Per ogni candidato il bot riconosce il contratto e verifica
   l'asset sottostante sulla chain:

   | Famiglia | Riconoscimento | Protocolli tipici |
   |---|---|---|
   | Aave V3 | `getReserveData(asset)` → aToken → `UNDERLYING_ASSET_ADDRESS()` | Aave |
   | ERC-4626 | `asset()` + `convertToAssets()` + TVL coerente con DefiLlama | Morpho, Fluid, Euler, Spark... |
   | Compound V3 | `baseToken()` | Compound (Comet) |
   | Compound V2 fork | `underlying()` + `exchangeRateStored()` | Moonwell |

   Solo i pool riconosciuti sono **eseguibili**; gli altri restano in
   dashboard come monitoraggio e il modello non può sceglierli.

4. **Portafoglio** — saldi letti on-chain (aToken, share del vault,
   saldo Comet, cToken). Prezzo di ingresso, APY e TVL all'ingresso vivono in
   `positions.json`. Un deposito fatto a mano in un pool riconosciuto viene
   adottato; una posizione ritirata fuori dal bot sparisce dal registro.

5. **Uscite automatiche** — se il TVL di un pool crolla rispetto
   all'ingresso (`EXIT_TVL_DROP_PCT`, default 40%) o il pool finisce in
   blocklist, il bot esce *prima* di sentire il modello.

6. **Decisione** — l'LLM (via OpenRouter) riceve universo, posizioni e
   limiti, e risponde con `deposit` / `withdraw` / `rebalance` / `hold`.

7. **Esecuzione** — approve + deposito/prelievo sul contratto riconosciuto.
   Se l'asset del vault non è USDC (con `DEPOSIT_ASSETS` esteso a WETH o
   cbBTC) il bot compra/vende l'asset su Uniswap V3.

---

## 🛡️ Barriere prima di ogni transazione

| Limite | Default | Variabile |
|---|---|---|
| Nessuna transazione firmata | **attivo** | `DRY_RUN=true` |
| Quota massima in un pool | 40% del portafoglio | `MAX_POOL_PCT` |
| Quota massima su un protocollo | 60% | `MAX_PROTOCOL_PCT` |
| Pool detenuti contemporaneamente | 4 | `MAX_POSITIONS` |
| Importo per deposito | $10 – $2500 | `MIN_DEPOSIT_USD`, `MAX_DEPOSIT_USD` |
| Miglioramento minimo per spostarsi | +1 punto di APY | `MIN_APY_IMPROVEMENT` |
| Permanenza minima in un pool | 24h | `MIN_HOLD_HOURS` |
| Lo spostamento deve ripagarsi entro | 14 giorni | `MAX_BREAKEVEN_DAYS` |
| Riserva ETH per il gas | 0.001 ETH | `MIN_ETH_RESERVE` |
| Gas price massimo | 0.5 gwei | `MAX_GAS_PRICE_GWEI` |

Il modello **non può aggirarli**: li applica l'esecutore, non il prompt.
Un segnale fuori limite viene rifiutato e il motivo finisce a DB, in
dashboard e su Telegram.

Il conto di convenienza di un ribilanciamento è esplicito: costo stimato
(transazioni × `EST_TX_COST_USD` + slippage degli eventuali swap) diviso il
guadagno giornaliero dato dal differenziale di APY. Spostare $50 per mezzo
punto non passa; spostare $2000 per due punti sì.

### APY "effettivo"

Il confronto fra pool usa **APY base + ricompense × `REWARD_APY_WEIGHT`**
(default 0.5). Le ricompense sono token di emissione che vanno reclamati e
venduti — il bot non lo fa ancora da solo — e possono finire da un giorno
all'altro. Nell'ordinamento l'APY di oggi è anche limitato a 1.5 volte la
media a 30 giorni: un picco di utilizzo non deve far spostare il capitale.

---

## 🚀 Avvio

```bash
cp .env.example .env     # compila wallet, RPC e chiave OpenRouter
pip install -r requirements.txt
python main.py           # un ciclo, in dry-run
python dashboard.py      # dashboard su http://localhost:3000
python -m unittest discover -s tests   # test offline
```

Con Docker:

```bash
docker compose up --build
```

### Deploy su CapRover

Come per il bot degen: `captain-definition` in root, un solo processo.

1. **Crea l'app** e collega il repo (o `caprover deploy` dalla cartella).
2. **App Configs → Container HTTP Port: `3000`.**
3. **Persistent Directories** → mappa `/app/data`. Database SQLite,
   `positions.json` e lo stato paper ci finiscono da soli. Senza volume il
   bot ritrova i depositi sulla chain (li adotta), ma perde APY e TVL
   d'ingresso e lo storico.
4. **Environment Variables**: almeno `OPENROUTER_API_KEY`, `WALLET_ADDRESS`,
   `PRIVATE_KEY`, `BASE_RPC_URL`. Lascia `DRY_RUN=true` per i primi cicli.
5. **Enable HTTPS** se esponi la dashboard.

Niente Prophet qui: l'immagine è leggera e il build dura pochi secondi.
L'intervallo fra i cicli è `TRADING_INTERVAL`, default **3600s**: i
rendimenti cambiano lentamente e ogni ciclo interroga RPC e LLM.

### Le tre modalità

| Modalità | `.env` | Cosa succede |
|---|---|---|
| **Dry-run** (default) | `DRY_RUN=true` | Legge il wallet vero, decide, valida i limiti e mostra il piano delle transazioni senza firmarle. |
| **Paper** | `PAPER_TRADING=true` | Portafoglio virtuale (`PAPER_START_USDC`, default $1000): i depositi maturano **l'APY reale corrente** di ogni pool, ciclo dopo ciclo. Vedi la strategia di rotazione lavorare senza capitale. |
| **Live** | `DRY_RUN=false` | Firma e manda le transazioni. |

In paper le posizioni sono tenute in USD: per un vault su WETH o cbBTC si
simula il rendimento ma non l'oscillazione di prezzo dell'asset. Ogni
transazione costa `PAPER_GAS_USD` e gli swap scontano lo slippage di default.
Per ricominciare, cancella `paper_portfolio.json` e `paper_positions.json`.

**Parti sempre in dry-run o paper.** In live usa un wallet dedicato.

---

## 🎯 Pool specifici

```bash
# considerato anche se il protocollo non e' in allowlist
# (id DefiLlama o indirizzo del vault; gli altri filtri restano)
POOL_WATCHLIST="0xIndirizzoDelVault"

# mai toccato; se lo detieni, il bot ne esce al ciclo successivo
POOL_BLOCKLIST="0xIndirizzoDelVault"
```

Un vault in watchlist diventa eseguibile solo se il riconoscimento on-chain
lo accetta (asset giusto, TVL coerente).

## 🔁 Looping su Aave (modalità opzionale)

Con `LOOP_ENABLED=true` il bot aggiunge all'universo dei pool i **loop a leva
su Aave V3**: un liquid staking token (wstETH, cbETH, weETH) come
collaterale, WETH preso in prestito, ricomprato in LST e ridepositato, a giri,
fino alla leva voluta.

```
APY netto = leva × (staking + supply LST) − (leva − 1) × borrow WETH
es. 3x, staking 3.0%, supply 0.1%, borrow 2.0%  →  3 × 3.1 − 2 × 2.0 = 5.3%
```

Per il modello un loop è un pool come gli altri (`pool_id` = `loop:WSTETH`,
`loop:CBETH`, `loop:WEETH`): lo propone con `deposit` e ne esce con
`withdraw`. Il resto lo fa l'esecutore:

- **tutto letto dalla chain a ogni ciclo**: tassi, LTV e soglia di
  liquidazione (con la categoria e-mode giusta, riconosciuta da sola), cap di
  supply/borrow, WETH disponibile, prezzi dell'oracolo Aave. Lo staking arriva
  da DefiLlama. Il simbolo on-chain di ogni LST viene verificato;
- **leva** = la minore fra `LOOP_MAX_LEVERAGE`, quella che lascia l'health
  factor a `LOOP_TARGET_HEALTH_FACTOR` e quella raggiungibile con l'LTV;
- **limiti**: al massimo `LOOP_MAX_CAPITAL_PCT` del portafoglio nei loop, metà
  dello spazio libero sotto i cap di Aave, più i soliti limiti per pool e per
  protocollo (un loop conta come Aave);
- **uscite automatiche**: health factor del loop sotto
  `LOOP_MIN_HEALTH_FACTOR` (depeg LST/ETH), oppure APY netto sotto
  `LOOP_EXIT_NET_APY` per `LOOP_EXIT_CYCLES` cicli di fila (tasso di
  prestito esploso). Lo smontaggio procede a giri — ritira LST, vende in WETH,
  rimborsa — senza mai scendere sotto l'HF minimo, poi riporta tutto in USDC;
- un loop **non si ribilancia**: si ritira e si rientra.

⚠️ Collaterale e debito sono entrambi ETH, quindi il prezzo di ETH non
avvicina la liquidazione, ma **il valore in dollari del loop segue ETH**: è
l'unico punto del bot in cui il capitale smette di essere in USDC. Il rischio
di liquidazione viene da un depeg dell'LST o da interessi che corrono.

In paper il loop matura staking e interessi reali letti dalla chain e viene
valutato al prezzo corrente di ETH (si vede l'esposizione); il dry-run
stampa il piano dei giri. La dashboard mostra i mercati di looping (staking,
borrow, leva, APY netto, HF, e-mode, capienza) e, per ogni loop aperto, leva
e health factor, che finiscono anche nel report Telegram.

## 📱 Telegram e dashboard

Comandi: `/status`, `/positions`, `/pools`, `/last`, `/run`. Sono accettati
**solo** dalla chat configurata in `TELEGRAM_CHAT_ID`, perché `/run` avvia un
ciclo che può firmare.

Per lo stesso motivo il pulsante "Esegui ciclo ora" della dashboard è
disattivato finché non imposti `DASHBOARD_RUN_TOKEN`; il browser lo chiede
una volta e lo ricorda.

#### Dashboard coerente fra i tre agenti

Le dashboard di `intelligent-trading-agent`, `-degen` e `-yield` condividono lo
stesso design system: `static/dashboard.css` e `static/dashboard.js` sono
**identici nei tre repository** (se li modifichi, copiali negli altri due).
Ogni pagina ha la stessa struttura: header con badge di modalità
(`LIVE` / `PAPER` / `DRY-RUN`), pannello paper trading, KPI, andamento del
capitale, posizioni e ultima decisione AI, sezioni specifiche del bot, storico
operazioni ed errori. Cambia solo il colore d'accento (blu, arancio, verde)
e l'icona.


#### Wallet e gas

Sotto l'header, fuori dal paper trading, la dashboard mostra il wallet del
bot: ETH per il gas (con il controvalore), USDC liberi, indirizzo con link a
Basescan e uno stato: **OK**, **IN ESAURIMENTO** (sotto `GAS_WARN_ETH`) o
**RICARICA ORA** (sotto la riserva minima). Lo stesso avviso compare nel
report Telegram del ciclo.

## 📁 Struttura

| File | Ruolo |
|---|---|
| `main.py` | il ciclo: scoperta → riconoscimento → uscite di rischio → decisione → esecuzione |
| `config.py` | contratti Base, soglie di screening e di rischio |
| `yield_scanner.py` | DefiLlama Yields, screening, punteggio |
| `protocols.py` | adattatori Aave V3, ERC-4626, Compound V3, cToken |
| `looping.py` | loop LST/WETH su Aave V3: mercati, leva, health factor, apertura e smontaggio |
| `yield_manager.py` | stato del portafoglio, limiti, depositi/prelievi/ribilanciamenti |
| `positions.py` | registro delle posizioni (ingresso, APY, TVL) |
| `paper.py` | portafoglio virtuale che matura l'APY reale |
| `yield_agent.py` | chiamata LLM, schema JSON, fallback su `hold` |
| `base_client.py` | RPC, ERC-20, invio transazioni, dry-run, tetto gas (dal bot degen) |
| `uniswap.py` | swap USDC ↔ WETH/cbBTC quando serve (dal bot degen) |
| `http_client.py` | pacing, retry e cache per le API pubbliche (dal bot degen) |
| `db_utils.py` | SQLite: snapshot, scansioni, operazioni, errori |
| `dashboard.py` | dashboard web |
| `static/dashboard.css`, `static/dashboard.js` | design system condiviso con i bot fratelli |
| `telegram_bot.py` | notifiche e comandi |

---

## ⚠️ Avvertenza

Il rendimento passivo **non è senza rischio**: un exploit del protocollo, un
oracolo manipolato o una cattiva liquidazione possono far perdere il
capitale depositato; un mercato al 100% di utilizzo può bloccare il
prelievo per ore o giorni; una stablecoin può perdere il peg. Screening e
limiti di diversificazione riducono l'esposizione, non la eliminano.
Nessuna garanzia, nessuna promessa di rendimento. Software fornito così com'è.

## 📜 Licenza

MIT.
