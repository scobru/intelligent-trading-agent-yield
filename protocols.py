"""
Adattatori verso i protocolli di rendimento su Base.

Il bot non si fida dell'id DefiLlama per sapere dove depositare: per ogni
pool candidato riconosce il contratto on-chain e verifica che l'asset
sottostante sia davvero quello atteso. Solo i pool riconosciuti diventano
"eseguibili"; gli altri restano in dashboard come monitoraggio.

Quattro famiglie coprono la gran parte del lending su Base:

  - Aave V3          supply/withdraw sul Pool, saldo = aToken
  - ERC-4626         vault standard: Morpho (MetaMorpho), Fluid, Euler, Spark...
  - Compound V3      Comet: supply/withdraw del base token
  - Compound V2 fork cToken/mToken (Moonwell): mint/redeemUnderlying
"""

import logging
import math
from typing import Any, Dict, Optional, Tuple

from web3 import Web3

import config
from base_client import MAX_UINT256, BaseClient

logger = logging.getLogger(__name__)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
SECONDS_PER_YEAR = 31_536_000

AAVE_POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getReserveData",
     "outputs": [{"components": [
         {"name": "configuration", "type": "uint256"},
         {"name": "liquidityIndex", "type": "uint128"},
         {"name": "currentLiquidityRate", "type": "uint128"},
         {"name": "variableBorrowIndex", "type": "uint128"},
         {"name": "currentVariableBorrowRate", "type": "uint128"},
         {"name": "currentStableBorrowRate", "type": "uint128"},
         {"name": "lastUpdateTimestamp", "type": "uint40"},
         {"name": "id", "type": "uint16"},
         {"name": "aTokenAddress", "type": "address"},
         {"name": "stableDebtTokenAddress", "type": "address"},
         {"name": "variableDebtTokenAddress", "type": "address"},
         {"name": "interestRateStrategyAddress", "type": "address"},
         {"name": "accruedToTreasury", "type": "uint128"},
         {"name": "unbacked", "type": "uint128"},
         {"name": "isolationModeTotalDebt", "type": "uint128"}],
         "name": "", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "onBehalfOf", "type": "address"}, {"name": "referralCode", "type": "uint16"}],
     "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "to", "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "getUserAccountData",
     "outputs": [
         {"name": "totalCollateralBase", "type": "uint256"},
         {"name": "totalDebtBase", "type": "uint256"},
         {"name": "availableBorrowsBase", "type": "uint256"},
         {"name": "currentLiquidationThreshold", "type": "uint256"},
         {"name": "ltv", "type": "uint256"},
         {"name": "healthFactor", "type": "uint256"}
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "interestRateMode", "type": "uint256"}, {"name": "referralCode", "type": "uint16"},
                {"name": "onBehalfOf", "type": "address"}],
     "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "interestRateMode", "type": "uint256"}, {"name": "onBehalfOf", "type": "address"}],
     "name": "repay", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "useAsCollateral", "type": "bool"}],
     "name": "setUserUseReserveAsCollateral", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]

ATOKEN_ABI = [
    {"inputs": [], "name": "UNDERLYING_ASSET_ADDRESS",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

VARIABLE_DEBT_TOKEN_ABI = [
    {"inputs": [], "name": "UNDERLYING_ASSET_ADDRESS",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

ERC4626_ABI = [
    {"inputs": [], "name": "asset", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalAssets", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "shares", "type": "uint256"}], "name": "convertToAssets",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "maxWithdraw",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}], "name": "maxRedeem",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"}],
     "name": "deposit", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"},
                {"name": "owner", "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "shares", "type": "uint256"}, {"name": "receiver", "type": "address"},
                {"name": "owner", "type": "address"}],
     "name": "redeem", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

COMET_ABI = [
    {"inputs": [], "name": "baseToken", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "isSupplyPaused", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "isWithdrawPaused", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

CTOKEN_ABI = [
    {"inputs": [], "name": "underlying", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "exchangeRateStored", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getCash", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "mintAmount", "type": "uint256"}], "name": "mint",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "redeemTokens", "type": "uint256"}], "name": "redeem",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "redeemAmount", "type": "uint256"}], "name": "redeemUnderlying",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]


def _cs(address: str) -> str:
    return Web3.to_checksum_address(address)


class Adapter:
    """Interfaccia comune: importi sempre in unita' raw dell'asset sottostante."""

    kind = "base"

    def __init__(self, client: BaseClient, address: str, asset: str):
        self.client = client
        self.w3 = client.w3
        self.address = _cs(address)   # contratto con cui si interagisce
        self.asset = _cs(asset)

    @property
    def spender(self) -> str:
        return self.address

    def balance_raw(self, owner: str = None) -> int:
        raise NotImplementedError

    def withdrawable_raw(self, owner: str = None) -> int:
        """Quanto si puo' ritirare adesso (la liquidita' del pool puo' essere impegnata)."""
        return self.balance_raw(owner)

    def deposit_fn(self, amount_raw: int):
        raise NotImplementedError

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        """amount_raw=None ritira tutto."""
        raise NotImplementedError

    def onchain_apy(self) -> Optional[float]:
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "address": self.address, "asset": self.asset}

    def _owner(self, owner: str = None) -> str:
        return _cs(owner or self.client.address)


class AaveV3Adapter(Adapter):
    kind = "aave-v3"

    def __init__(self, client: BaseClient, asset: str):
        super().__init__(client, config.AAVE_V3_POOL, asset)
        self.pool = self.w3.eth.contract(address=self.address, abi=AAVE_POOL_ABI)
        reserve = self.pool.functions.getReserveData(self.asset).call()
        self.a_token = _cs(reserve[8])
        self.liquidity_rate = int(reserve[2])
        if self.a_token == ZERO_ADDRESS:
            raise ValueError("asset non listato su Aave V3")
        self.a_token_contract = self.w3.eth.contract(address=self.a_token, abi=ATOKEN_ABI)
        underlying = self.a_token_contract.functions.UNDERLYING_ASSET_ADDRESS().call()
        if underlying.lower() != self.asset.lower():
            raise ValueError("aToken con sottostante diverso")

    def balance_raw(self, owner: str = None) -> int:
        return int(self.a_token_contract.functions.balanceOf(self._owner(owner)).call())

    def withdrawable_raw(self, owner: str = None) -> int:
        # la liquidita' disponibile e' il saldo di asset detenuto dall'aToken
        cash = self.client.balance_of(self.asset, self.a_token)
        return min(self.balance_raw(owner), cash)

    def deposit_fn(self, amount_raw: int):
        return self.pool.functions.supply(self.asset, int(amount_raw), self.client.address, 0)

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        amount = MAX_UINT256 if amount_raw is None else int(amount_raw)
        return self.pool.functions.withdraw(self.asset, amount, self.client.address)

    def onchain_apy(self) -> Optional[float]:
        apr = self.liquidity_rate / 1e27
        return ((1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100

    def to_dict(self) -> Dict[str, Any]:
        return dict(super().to_dict(), receipt_token=self.a_token)


class AaveV3LoopAdapter(Adapter):
    """
    Adattatore per posizioni di Looping su Aave V3 (es. wstETH / WETH o cbETH / WETH).

    Meccanica:
      - Collateral: Liquid Staking Token (wstETH o cbETH) che matura staking yield nativo.
      - Debt: WETH preso in prestito contro il collateral wstETH/cbETH.
      - Rischio di cambio cross-asset nullo o minimo (entrambi denominati in ETH).
      - Monitoraggio rigoroso dell'Health Factor on-chain con getUserAccountData.
    """
    kind = "aave-v3-loop"

    def __init__(self, client: BaseClient, collateral_asset: str,
                 borrow_asset: str = None, leverage: float = None,
                 staking_yield: float = None):
        borrow_asset = borrow_asset or config.WETH
        super().__init__(client, config.AAVE_V3_POOL, collateral_asset)
        self.collateral_asset = self.asset
        self.borrow_asset = _cs(borrow_asset)
        self.leverage = float(leverage or config.LOOP_MAX_LEVERAGE)

        self.collateral_symbol = "wstETH"
        self.borrow_symbol = "WETH"
        for sym, meta in config.KNOWN_ASSETS.items():
            if meta["address"].lower() == self.collateral_asset.lower():
                self.collateral_symbol = sym
            if meta["address"].lower() == self.borrow_asset.lower():
                self.borrow_symbol = sym

        self.staking_yield = float(
            staking_yield if staking_yield is not None
            else config.KNOWN_ASSETS.get(self.collateral_symbol, {}).get("staking_yield", 3.2)
        )

        self.pool = self.w3.eth.contract(address=self.address, abi=AAVE_POOL_ABI)

        res_collateral = self.pool.functions.getReserveData(self.collateral_asset).call()
        self.a_token = _cs(res_collateral[8])
        self.collateral_liquidity_rate = int(res_collateral[2])
        if self.a_token == ZERO_ADDRESS:
            raise ValueError("collateral asset non listato su Aave V3")
        self.a_token_contract = self.w3.eth.contract(address=self.a_token, abi=ATOKEN_ABI)

        res_borrow = self.pool.functions.getReserveData(self.borrow_asset).call()
        self.var_debt_token = _cs(res_borrow[10])
        self.variable_borrow_rate = int(res_borrow[4])
        if self.var_debt_token == ZERO_ADDRESS:
            raise ValueError("borrow asset non listato su Aave V3")
        self.var_debt_contract = self.w3.eth.contract(address=self.var_debt_token, abi=VARIABLE_DEBT_TOKEN_ABI)

    def balance_raw(self, owner: str = None) -> int:
        """Saldo di collateral depositato (aToken)."""
        return int(self.a_token_contract.functions.balanceOf(self._owner(owner)).call())

    def debt_raw(self, owner: str = None) -> int:
        """Debito variabile contratto (variable debt token)."""
        return int(self.var_debt_contract.functions.balanceOf(self._owner(owner)).call())

    def user_account_data(self, owner: str = None) -> Dict[str, Any]:
        """Dati della posizione su Aave V3 (collateral, debt, LTV, Health Factor)."""
        data = self.pool.functions.getUserAccountData(self._owner(owner)).call()
        hf_raw = int(data[5])
        hf = float("inf") if hf_raw >= 10**30 else round(hf_raw / 1e18, 4)
        return {
            "total_collateral_usd": round(data[0] / 1e8, 4),
            "total_debt_usd": round(data[1] / 1e8, 4),
            "available_borrows_usd": round(data[2] / 1e8, 4),
            "liquidation_threshold_pct": round(data[3] / 100.0, 2),
            "ltv_pct": round(data[4] / 100.0, 2),
            "health_factor": hf,
        }

    def health_factor(self, owner: str = None) -> float:
        return self.user_account_data(owner)["health_factor"]

    def rates(self) -> Dict[str, float]:
        """Calcola i tassi componenti e il Net APY alla leva specificata."""
        supply_apr = self.collateral_liquidity_rate / 1e27
        supply_apy = ((1 + supply_apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100

        borrow_apr = self.variable_borrow_rate / 1e27
        borrow_apy = ((1 + borrow_apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100

        l = self.leverage
        net_apy = (self.staking_yield + supply_apy) * l - borrow_apy * (l - 1.0)

        return {
            "staking_yield": round(self.staking_yield, 4),
            "supply_apy": round(supply_apy, 4),
            "borrow_apy": round(borrow_apy, 4),
            "leverage": round(l, 2),
            "net_apy": round(net_apy, 4),
        }

    def onchain_apy(self) -> Optional[float]:
        return self.rates()["net_apy"]

    def withdrawable_raw(self, owner: str = None) -> int:
        cash = self.client.balance_of(self.asset, self.a_token)
        return min(self.balance_raw(owner), cash)

    def deposit_fn(self, amount_raw: int):
        return self.pool.functions.supply(self.collateral_asset, int(amount_raw), self.client.address, 0)

    def borrow_fn(self, amount_raw: int):
        return self.pool.functions.borrow(self.borrow_asset, int(amount_raw), 2, 0, self.client.address)

    def repay_fn(self, amount_raw: Optional[int] = None):
        amount = MAX_UINT256 if amount_raw is None else int(amount_raw)
        return self.pool.functions.repay(self.borrow_asset, amount, 2, self.client.address)

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        amount = MAX_UINT256 if amount_raw is None else int(amount_raw)
        return self.pool.functions.withdraw(self.collateral_asset, amount, self.client.address)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "address": self.address,
            "asset": self.collateral_asset,
            "borrow_asset": self.borrow_asset,
            "collateral_symbol": self.collateral_symbol,
            "borrow_symbol": self.borrow_symbol,
            "leverage": self.leverage,
            "staking_yield": self.staking_yield,
            "a_token": self.a_token,
            "debt_token": self.var_debt_token,
        }


class ERC4626Adapter(Adapter):
    kind = "erc4626"

    def __init__(self, client: BaseClient, address: str, asset: str):
        super().__init__(client, address, asset)
        self.vault = self.w3.eth.contract(address=self.address, abi=ERC4626_ABI)
        vault_asset = self.vault.functions.asset().call()
        if vault_asset.lower() != self.asset.lower():
            raise ValueError("vault ERC-4626 con asset diverso")
        # deve rispondere anche alla conversione, non solo ad asset()
        self.vault.functions.convertToAssets(10 ** 6).call()

    def total_assets_raw(self) -> int:
        return int(self.vault.functions.totalAssets().call())

    def balance_raw(self, owner: str = None) -> int:
        shares = int(self.vault.functions.balanceOf(self._owner(owner)).call())
        if shares == 0:
            return 0
        return int(self.vault.functions.convertToAssets(shares).call())

    def withdrawable_raw(self, owner: str = None) -> int:
        try:
            return int(self.vault.functions.maxWithdraw(self._owner(owner)).call())
        except Exception:
            return self.balance_raw(owner)

    def deposit_fn(self, amount_raw: int):
        return self.vault.functions.deposit(int(amount_raw), self.client.address)

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        owner = self._owner(owner)
        if amount_raw is None:
            shares = int(self.vault.functions.maxRedeem(owner).call())
            return self.vault.functions.redeem(shares, self.client.address, owner)
        return self.vault.functions.withdraw(int(amount_raw), self.client.address, owner)


class CometAdapter(Adapter):
    kind = "compound-v3"

    def __init__(self, client: BaseClient, address: str, asset: str):
        super().__init__(client, address, asset)
        self.comet = self.w3.eth.contract(address=self.address, abi=COMET_ABI)
        base = self.comet.functions.baseToken().call()
        if base.lower() != self.asset.lower():
            raise ValueError("Comet con base token diverso")

    def balance_raw(self, owner: str = None) -> int:
        return int(self.comet.functions.balanceOf(self._owner(owner)).call())

    def withdrawable_raw(self, owner: str = None) -> int:
        cash = self.client.balance_of(self.asset, self.address)
        return min(self.balance_raw(owner), cash)

    def deposit_fn(self, amount_raw: int):
        return self.comet.functions.supply(self.asset, int(amount_raw))

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        amount = MAX_UINT256 if amount_raw is None else int(amount_raw)
        return self.comet.functions.withdraw(self.asset, amount)


class CTokenAdapter(Adapter):
    kind = "ctoken"

    def __init__(self, client: BaseClient, address: str, asset: str):
        super().__init__(client, address, asset)
        self.ctoken = self.w3.eth.contract(address=self.address, abi=CTOKEN_ABI)
        underlying = self.ctoken.functions.underlying().call()
        if underlying.lower() != self.asset.lower():
            raise ValueError("cToken con sottostante diverso")
        if int(self.ctoken.functions.exchangeRateStored().call()) <= 0:
            raise ValueError("exchangeRate nullo")

    def balance_raw(self, owner: str = None) -> int:
        shares = int(self.ctoken.functions.balanceOf(self._owner(owner)).call())
        rate = int(self.ctoken.functions.exchangeRateStored().call())
        return shares * rate // 10 ** 18

    def withdrawable_raw(self, owner: str = None) -> int:
        cash = int(self.ctoken.functions.getCash().call())
        return min(self.balance_raw(owner), cash)

    def deposit_fn(self, amount_raw: int):
        return self.ctoken.functions.mint(int(amount_raw))

    def withdraw_fn(self, amount_raw: Optional[int], owner: str = None):
        if amount_raw is None:
            shares = int(self.ctoken.functions.balanceOf(self._owner(owner)).call())
            return self.ctoken.functions.redeem(shares)
        return self.ctoken.functions.redeemUnderlying(int(amount_raw))


def _usd_price(asset_symbol: str, prices: Dict[str, float]) -> Optional[float]:
    if asset_symbol == "USDC":
        return 1.0
    return prices.get(asset_symbol)


def _tvl_plausible(adapter: ERC4626Adapter, pool: Dict[str, Any], prices: Dict[str, float]) -> bool:
    """
    Controllo incrociato per i vault: il totalAssets on-chain deve essere
    dello stesso ordine di grandezza del TVL dichiarato da DefiLlama.
    Protegge da un id che punta al contratto sbagliato.
    """
    price = _usd_price(pool.get("asset") or "", prices)
    tvl = float(pool.get("tvl_usd") or 0)
    if not price or tvl <= 0:
        return True
    decimals = config.KNOWN_ASSETS[pool["asset"]]["decimals"]
    onchain_usd = adapter.total_assets_raw() / 10 ** decimals * price
    if onchain_usd <= 0:
        return False
    ratio = onchain_usd / tvl
    # DefiLlama a volte conta il TVL al netto dei prestiti: tolleranza larga
    return 0.2 <= ratio <= 5.0


def resolve(client: BaseClient, pool: Dict[str, Any],
            prices: Dict[str, float] = None) -> Tuple[Optional[Adapter], str]:
    """
    Riconosce il contratto di un pool normalizzato dallo scanner.
    Restituisce (adapter, "") oppure (None, motivo).
    """
    prices = prices or {}
    asset = pool.get("asset_address")
    if not asset:
        return None, "asset sottostante sconosciuto"

    project = str(pool.get("project") or "").lower()
    if pool.get("is_loop") or project.startswith("aave-v3-loop"):
        try:
            collateral = pool.get("asset_address")
            borrow = pool.get("borrow_asset_address", config.WETH)
            leverage = pool.get("leverage", config.LOOP_MAX_LEVERAGE)
            staking = pool.get("staking_yield")
            return AaveV3LoopAdapter(client, collateral, borrow, leverage=leverage, staking_yield=staking), ""
        except Exception as exc:
            return None, f"Aave V3 Loop non riconosciuto: {exc}"

    if project.startswith("aave-v3"):
        try:
            return AaveV3Adapter(client, asset), ""
        except Exception as exc:
            return None, f"Aave V3 non riconosciuto: {exc}"

    candidates = pool.get("addresses") or []
    if not candidates:
        return None, "nessun indirizzo di contratto nell'id del pool"

    errors = []
    for address in candidates:
        if address.lower() == str(asset).lower():
            continue
        # Comet e cToken prima: alcuni espongono anche funzioni simili a 4626
        for cls in (CometAdapter, CTokenAdapter, ERC4626Adapter):
            try:
                adapter = cls(client, address, asset)
            except Exception as exc:
                errors.append(f"{cls.kind}: {type(exc).__name__}")
                continue
            if isinstance(adapter, ERC4626Adapter) and not _tvl_plausible(adapter, pool, prices):
                return None, "totalAssets on-chain incoerente con il TVL di DefiLlama"
            return adapter, ""
    return None, "contratto non riconosciuto (" + ", ".join(errors[:3]) + ")"


def raw_to_float(amount_raw: int, asset_symbol: str) -> float:
    decimals = config.KNOWN_ASSETS[asset_symbol]["decimals"]
    return amount_raw / 10 ** decimals


def float_to_raw(amount: float, asset_symbol: str) -> int:
    decimals = config.KNOWN_ASSETS[asset_symbol]["decimals"]
    return int(math.floor(amount * 10 ** decimals))
