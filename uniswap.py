"""
Rotte e swap su Uniswap V3 (Base), senza aggregatori esterni.

Il quoting passa da QuoterV2: si provano le fee tier su rotta diretta e,
quando serve, la rotta a due salti via WETH. Lo swap va su SwapRouter02.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3

import config
from base_client import BaseChainError, BaseClient

logger = logging.getLogger(__name__)

QUOTER_V2_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "fee", "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"}],
            "name": "params", "type": "tuple"}],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"}],
        "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [
            {"name": "path", "type": "bytes"},
            {"name": "amountIn", "type": "uint256"}],
        "name": "quoteExactInput",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96AfterList", "type": "uint160[]"},
            {"name": "initializedTicksCrossedList", "type": "uint32[]"},
            {"name": "gasEstimate", "type": "uint256"}],
        "stateMutability": "nonpayable", "type": "function",
    },
]

SWAP_ROUTER_02_ABI = [
    {
        "inputs": [{"components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"}],
            "name": "params", "type": "tuple"}],
        "name": "exactInputSingle",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
    {
        "inputs": [{"components": [
            {"name": "path", "type": "bytes"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"}],
            "name": "params", "type": "tuple"}],
        "name": "exactInput",
        "outputs": [{"name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable", "type": "function",
    },
]

FACTORY_ABI = [
    {
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"}],
        "name": "getPool",
        "outputs": [{"name": "pool", "type": "address"}],
        "stateMutability": "view", "type": "function",
    }
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def encode_path(tokens: List[str], fees: List[int]) -> bytes:
    """
    Codifica una rotta multi-hop nel formato Uniswap V3:
    token (20 byte) + fee (3 byte) + token + fee + ... + token.
    """
    if len(tokens) != len(fees) + 1:
        raise ValueError("encode_path: serve un token in piu' rispetto alle fee")

    encoded = b""
    for i, fee in enumerate(fees):
        encoded += bytes.fromhex(Web3.to_checksum_address(tokens[i])[2:])
        encoded += int(fee).to_bytes(3, "big")
    encoded += bytes.fromhex(Web3.to_checksum_address(tokens[-1])[2:])
    return encoded


class Route:
    """Una rotta quotata, pronta per essere eseguita."""

    def __init__(self, tokens: List[str], fees: List[int], amount_in: int, amount_out: int):
        self.tokens = tokens
        self.fees = fees
        self.amount_in = amount_in
        self.amount_out = amount_out

    @property
    def is_single_hop(self) -> bool:
        return len(self.fees) == 1

    @property
    def path_bytes(self) -> bytes:
        return encode_path(self.tokens, self.fees)

    def describe(self, client: BaseClient = None) -> str:
        if client:
            names = [client.symbol(t) for t in self.tokens]
        else:
            names = [t[:6] for t in self.tokens]
        parts = [names[0]]
        for i, fee in enumerate(self.fees):
            parts.append(f"-[{fee/10_000:.2f}%]->")
            parts.append(names[i + 1])
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tokens": self.tokens,
            "fees": self.fees,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "hops": len(self.fees),
        }


class UniswapV3:
    def __init__(self, client: BaseClient):
        self.client = client
        self.w3 = client.w3
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.UNISWAP_V3_QUOTER_V2), abi=QUOTER_V2_ABI
        )
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.UNISWAP_V3_SWAP_ROUTER_02), abi=SWAP_ROUTER_02_ABI
        )
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.UNISWAP_V3_FACTORY), abi=FACTORY_ABI
        )
        self._pool_cache: Dict[Tuple[str, str, int], bool] = {}

    # ------------------------------------------------------------ pool
    def pool_exists(self, token_a: str, token_b: str, fee: int) -> bool:
        key = (token_a.lower(), token_b.lower(), fee)
        if key not in self._pool_cache:
            try:
                pool = self.factory.functions.getPool(
                    Web3.to_checksum_address(token_a),
                    Web3.to_checksum_address(token_b),
                    int(fee),
                ).call()
                self._pool_cache[key] = pool != ZERO_ADDRESS
            except Exception:
                self._pool_cache[key] = False
        return self._pool_cache[key]

    # ------------------------------------------------------------ quoting
    def _quote_single(self, token_in: str, token_out: str, fee: int, amount_in: int) -> Optional[int]:
        if not self.pool_exists(token_in, token_out, fee):
            return None
        try:
            result = self.quoter.functions.quoteExactInputSingle({
                "tokenIn": Web3.to_checksum_address(token_in),
                "tokenOut": Web3.to_checksum_address(token_out),
                "amountIn": int(amount_in),
                "fee": int(fee),
                "sqrtPriceLimitX96": 0,
            }).call()
            return int(result[0])
        except Exception as exc:
            logger.debug("quote single %s->%s fee %s fallita: %s", token_in, token_out, fee, exc)
            return None

    def _quote_path(self, tokens: List[str], fees: List[int], amount_in: int) -> Optional[int]:
        for i, fee in enumerate(fees):
            if not self.pool_exists(tokens[i], tokens[i + 1], fee):
                return None
        try:
            result = self.quoter.functions.quoteExactInput(
                encode_path(tokens, fees), int(amount_in)
            ).call()
            return int(result[0])
        except Exception as exc:
            logger.debug("quote path %s fallita: %s", tokens, exc)
            return None

    def best_route(self, token_in: str, token_out: str, amount_in: int) -> Optional[Route]:
        """
        Cerca la rotta migliore: prima i salti diretti su ogni fee tier,
        poi quelli a due salti via WETH (dove vive la liquidita' delle meme).
        """
        if amount_in <= 0:
            return None
        if token_in.lower() == token_out.lower():
            return None

        best: Optional[Route] = None

        for fee in config.FEE_TIERS:
            out = self._quote_single(token_in, token_out, fee, amount_in)
            if out and (best is None or out > best.amount_out):
                best = Route([token_in, token_out], [fee], amount_in, out)

        weth = config.WETH.lower()
        if weth not in (token_in.lower(), token_out.lower()):
            for fee_in in config.FEE_TIERS:
                for fee_out in config.FEE_TIERS:
                    out = self._quote_path(
                        [token_in, config.WETH, token_out], [fee_in, fee_out], amount_in
                    )
                    if out and (best is None or out > best.amount_out):
                        best = Route(
                            [token_in, config.WETH, token_out], [fee_in, fee_out], amount_in, out
                        )

        return best

    def price_in_quote(self, token: str, quote_token: str = None, probe_units: float = 1.0) -> Optional[float]:
        """
        Prezzo indicativo di `token` espresso in `quote_token`, ottenuto
        quotando una piccola quantita'. None se non esiste rotta.
        """
        quote_token = quote_token or config.USDC
        if token.lower() == quote_token.lower():
            return 1.0

        decimals_in = self.client.decimals(token)
        decimals_out = self.client.decimals(quote_token)
        amount_in = int(probe_units * (10 ** decimals_in))

        route = self.best_route(token, quote_token, amount_in)
        if not route or route.amount_out <= 0:
            return None
        return (route.amount_out / (10 ** decimals_out)) / probe_units

    # ------------------------------------------------------------ esecuzione
    def swap(self, route: Route, slippage_bps: int, recipient: str = None) -> Dict[str, Any]:
        """Esegue la rotta quotata applicando lo slippage massimo indicato."""
        slippage_bps = max(1, min(int(slippage_bps), config.MAX_SLIPPAGE_BPS))
        recipient = Web3.to_checksum_address(recipient or self.client.address)
        amount_out_min = int(route.amount_out * (10_000 - slippage_bps) / 10_000)

        token_in = route.tokens[0]
        self.client.ensure_allowance(
            token_in, config.UNISWAP_V3_SWAP_ROUTER_02, route.amount_in
        )

        if route.is_single_hop:
            fn = self.router.functions.exactInputSingle({
                "tokenIn": Web3.to_checksum_address(route.tokens[0]),
                "tokenOut": Web3.to_checksum_address(route.tokens[1]),
                "fee": int(route.fees[0]),
                "recipient": recipient,
                "amountIn": int(route.amount_in),
                "amountOutMinimum": amount_out_min,
                "sqrtPriceLimitX96": 0,
            })
        else:
            fn = self.router.functions.exactInput({
                "path": route.path_bytes,
                "recipient": recipient,
                "amountIn": int(route.amount_in),
                "amountOutMinimum": amount_out_min,
            })

        tx = fn.build_transaction({
            "from": self.client.address,
            "value": 0,
            "nonce": self.w3.eth.get_transaction_count(self.client.address),
            "chainId": config.CHAIN_ID,
        })
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)

        result = self.client.send_transaction(
            tx, description=f"swap {route.describe(self.client)}"
        )
        result.update({
            "route": route.to_dict(),
            "amount_out_min": amount_out_min,
            "slippage_bps": slippage_bps,
        })
        return result

    # ------------------------------------------------------------ auto-refuel USDC
    def swap_eth_to_usdc(self, eth_amount: float, slippage_bps: int = None) -> Dict[str, Any]:
        """
        Converte un ammontare di ETH in USDC:
        1. Se il wallet ha gia' abbastanza WETH, usa WETH. Altrimenti wrappa l'ETH nativo mancante in WETH (1:1).
        2. Esegue swap Uniswap V3 WETH -> USDC.
        """
        if eth_amount <= 0:
            raise BaseChainError("Importo ETH non valido per swap in USDC")

        slippage_bps = slippage_bps or config.DEFAULT_SLIPPAGE_BPS
        amount_wei = int(eth_amount * 1e18)

        # Controlla saldo WETH disponibile
        weth_raw = self.client.balance_of(config.WETH)
        wrap_needed = amount_wei - weth_raw

        wrap_res = None
        if wrap_needed > 0:
            wrap_eth_amount = wrap_needed / 1e18
            logger.info("Wrap di %.5f ETH in WETH per rifornimento USDC...", wrap_eth_amount)
            wrap_res = self.client.wrap_eth(wrap_eth_amount)

        # Cerca rotta WETH -> USDC
        route = self.best_route(config.WETH, config.USDC, amount_wei)
        if not route or route.amount_out <= 0:
            raise BaseChainError("Impossibile trovare una rotta Uniswap V3 per WETH -> USDC")

        swap_res = self.swap(route, slippage_bps=slippage_bps)
        swap_res["wrap_tx"] = wrap_res.get("tx_hash") if wrap_res else None
        swap_res["eth_swapped"] = eth_amount
        usdc_est = route.amount_out / (10 ** 6)
        logger.info(
            "Swap completato: %.5f ETH -> ~%.2f USDC (tx: %s)",
            eth_amount, usdc_est, swap_res.get("tx_hash")
        )
        return swap_res

    def auto_refuel_usdc(
        self,
        gas_reserve: float = None,
        min_swap_eth: float = None,
        threshold_usdc: float = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Se il saldo USDC nel wallet e' sotto la soglia (default config.USDC_AUTO_SWAP_THRESHOLD),
        e c'e' ETH nativo in eccesso rispetto alla riserva per gas (default config.ETH_GAS_RESERVE),
        swappa in automatico l'eccesso di ETH in USDC tenendosi l'ETH necessario per le fee.
        """
        if not getattr(config, "AUTO_SWAP_ETH_TO_USDC", True):
            return None

        gas_reserve = gas_reserve if gas_reserve is not None else getattr(config, "ETH_GAS_RESERVE", 0.003)
        min_swap_eth = min_swap_eth if min_swap_eth is not None else getattr(config, "MIN_ETH_SWAP_AMOUNT", 0.002)
        threshold_usdc = threshold_usdc if threshold_usdc is not None else getattr(config, "USDC_AUTO_SWAP_THRESHOLD", 5.0)

        usdc_bal = self.client.balance_of_float(config.USDC)
        if usdc_bal >= threshold_usdc:
            return None

        eth_bal = self.client.eth_balance()
        weth_bal = self.client.balance_of_float(config.WETH)

        # Calcola ETH spendibile preservando la riserva per il gas nativo
        swappable_native_eth = max(0.0, eth_bal - gas_reserve)
        total_swappable_eth = swappable_native_eth + weth_bal

        if total_swappable_eth < min_swap_eth:
            logger.info(
                "Auto-refuel USDC saltato: USDC=%.2f, ETH=%.5f, WETH=%.5f (riserva gas=%.5f, disponibile=%.5f < min=%.5f)",
                usdc_bal, eth_bal, weth_bal, gas_reserve, total_swappable_eth, min_swap_eth
            )
            return None

        logger.info(
            "Auto-refuel USDC attivato: USDC=%.2f < soglia=%.2f. ETH=%.5f (riserva gas=%.5f), WETH=%.5f -> swappo %.5f ETH/WETH in USDC",
            usdc_bal, threshold_usdc, eth_bal, gas_reserve, weth_bal, total_swappable_eth
        )
        try:
            return self.swap_eth_to_usdc(total_swappable_eth)
        except Exception as exc:
            logger.error("Errore durante auto-refuel ETH -> USDC: %s", exc)
            return None
