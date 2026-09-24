"""
Configurazione del bot yield su Base (chain id 8453).

Gli indirizzi qui sotto sono contratti noti di Base mainnet. Non ci fidiamo
della costante scritta a mano: all'avvio `base_client.verify_contracts()`
interroga la chain e confronta symbol/decimals, e ogni vault viene
riconosciuto on-chain (asset(), baseToken(), underlying()) prima di
depositarci anche un solo dollaro.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "si")


def _set(name: str, default: str = "") -> set:
    return {
        a.strip().lower()
        for a in os.getenv(name, default).split(",")
        if a.strip()
    }


# ---------------------------------------------------------------- rete
CHAIN_ID = 8453
CHAIN_NAME = "Base"  # come la chiama DefiLlama
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

# ---------------------------------------------------------------- contratti Base
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

# Uniswap V3 su Base: serve solo quando l'asset del vault non e' USDC
UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNISWAP_V3_SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"
FEE_TIERS = (100, 500, 3000, 10000)

# Aave V3 Pool su Base
AAVE_V3_POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"

# Asset che il bot sa detenere. Il capitale inattivo sta in USDC; gli altri
# si comprano via Uniswap solo quando un vault su quell'asset vince.
KNOWN_ASSETS = {
    "USDC": {"address": USDC, "decimals": 6, "stable": True},
    "WETH": {"address": WETH, "decimals": 18, "stable": False},
    "CBBTC": {"address": CBBTC, "decimals": 8, "stable": False},
}

# Quali asset il bot puo' depositare. Default solo USDC: niente rischio
# di prezzo, il rendimento e' l'unica variabile.
DEPOSIT_ASSETS = {
    s.strip().upper()
    for s in os.getenv("DEPOSIT_ASSETS", "USDC").split(",")
    if s.strip().upper() in KNOWN_ASSETS
} or {"USDC"}

BASE_CURRENCY = "USDC"

# ---------------------------------------------------------------- sicurezza operativa
# Di default NON firma nulla: mettere DRY_RUN=false per operare davvero.
DRY_RUN = _b("DRY_RUN", True)

# Paper: portafoglio virtuale che matura il rendimento reale dei pool.
PAPER_TRADING = _b("PAPER_TRADING", False)
PAPER_START_USDC = _f("PAPER_START_USDC", 1000.0)
PAPER_START_ETH = _f("PAPER_START_ETH", 0.01)
PAPER_GAS_USD = _f("PAPER_GAS_USD", 0.02)

if PAPER_TRADING:
    DRY_RUN = True

MIN_ETH_RESERVE = _f("MIN_ETH_RESERVE", 0.001)
MAX_SLIPPAGE_BPS = _i("MAX_SLIPPAGE_BPS", 100)          # 1%: si comprano solo major
DEFAULT_SLIPPAGE_BPS = _i("DEFAULT_SLIPPAGE_BPS", 50)
MAX_GAS_PRICE_GWEI = _f("MAX_GAS_PRICE_GWEI", 0.5)
TX_DEADLINE_SECONDS = _i("TX_DEADLINE_SECONDS", 300)
TX_TIMEOUT_SECONDS = _i("TX_TIMEOUT_SECONDS", 180)
# Stima del costo gas di una singola transazione, usata nei conti di
# convenienza di un ribilanciamento (su Base e' tipicamente pochi centesimi)
EST_TX_COST_USD = _f("EST_TX_COST_USD", 0.05)

# ---------------------------------------------------------------- auto-refuel USDC da ETH
AUTO_SWAP_ETH_TO_USDC = _b("AUTO_SWAP_ETH_TO_USDC", True)
ETH_GAS_RESERVE = _f("ETH_GAS_RESERVE", 0.003)          # Riserva di ETH nativo per gas fee
MIN_ETH_SWAP_AMOUNT = _f("MIN_ETH_SWAP_AMOUNT", 0.002)   # Soglia minima di ETH spendibile
USDC_AUTO_SWAP_THRESHOLD = _f("USDC_AUTO_SWAP_THRESHOLD", 5.0)  # Attiva swap se USDC < soglia

# ---------------------------------------------------------------- limiti di rischio
MAX_POOL_PCT = _f("MAX_POOL_PCT", 0.40)          # quota massima in un solo pool
MAX_PROTOCOL_PCT = _f("MAX_PROTOCOL_PCT", 0.60)  # quota massima su un protocollo
MAX_POSITIONS = _i("MAX_POSITIONS", 4)
MIN_DEPOSIT_USD = _f("MIN_DEPOSIT_USD", 10.0)
MAX_DEPOSIT_USD = _f("MAX_DEPOSIT_USD", 2500.0)
DUST_USD = _f("DUST_USD", 1.0)

# Anti-churn: un ribilanciamento deve valere la pena
MIN_APY_IMPROVEMENT = _f("MIN_APY_IMPROVEMENT", 1.0)   # punti percentuali
MIN_HOLD_HOURS = _f("MIN_HOLD_HOURS", 24.0)
MAX_BREAKEVEN_DAYS = _f("MAX_BREAKEVEN_DAYS", 14.0)

# Uscite automatiche, prima di sentire il modello
AUTO_RISK_EXITS = _b("AUTO_RISK_EXITS", True)
EXIT_TVL_DROP_PCT = _f("EXIT_TVL_DROP_PCT", 40.0)     # TVL crollato rispetto all'ingresso

# ---------------------------------------------------------------- screening pool
# Protocolli ammessi (slug DefiLlama). Solo lending e vault single-asset,
# niente farm con emissioni gonfiate: si parte da nomi con storia.
PROTOCOL_ALLOWLIST = _set(
    "PROTOCOL_ALLOWLIST",
    "aave-v3,compound-v3,morpho-blue,morpho-v1,moonwell-lending,fluid-lending,"
    "euler-v2,spark-savings,seamless-protocol",
)
SCREEN_MIN_TVL_USD = _f("SCREEN_MIN_TVL_USD", 1_000_000)
SCREEN_MIN_APY = _f("SCREEN_MIN_APY", 1.0)
SCREEN_MAX_APY = _f("SCREEN_MAX_APY", 40.0)      # sopra e' quasi sempre emissione o bug
SCREEN_MIN_HISTORY_DAYS = _i("SCREEN_MIN_HISTORY_DAYS", 14)
SCREEN_MAX_REWARD_SHARE = _f("SCREEN_MAX_REWARD_SHARE", 0.7)
SCREEN_MAX_CANDIDATES = _i("SCREEN_MAX_CANDIDATES", 15)
SCREEN_CACHE_SECONDS = _i("SCREEN_CACHE_SECONDS", 1800)
# Le ricompense in token vanno reclamate e vendute: il bot non lo fa da solo,
# quindi nel confronto valgono meno del rendimento base.
REWARD_APY_WEIGHT = _f("REWARD_APY_WEIGHT", 0.5)

# Pool sempre esclusi / sempre considerati (id DefiLlama o indirizzo vault)
POOL_BLOCKLIST = _set("POOL_BLOCKLIST")
POOL_WATCHLIST = _set("POOL_WATCHLIST")

# ---------------------------------------------------------------- API esterne
DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
DEFILLAMA_CHART_URL = "https://yields.llama.fi/chart/"
HTTP_TIMEOUT = _i("HTTP_TIMEOUT", 30)


def persistent_path(filename: str) -> str:
    """
    Su CapRover/Docker /app/data e' la directory montata come volume
    persistente: i file di stato vanno li', altrimenti a ogni redeploy si
    perde lo storico. In locale restano accanto al codice.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_dir, "data")
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return os.path.join(data_dir, filename)
    return os.path.join(project_dir, filename)


SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH") or persistent_path("yield_agent.db")
POSITIONS_PATH = os.getenv("POSITIONS_PATH") or persistent_path("positions.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH") or persistent_path("paper_portfolio.json")
PAPER_POSITIONS_PATH = os.getenv("PAPER_POSITIONS_PATH") or persistent_path("paper_positions.json")
