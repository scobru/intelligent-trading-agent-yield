"""
Accesso alla chain Base: connessione RPC, lettura ERC-20, invio transazioni.

Tutto passa da qui, cosi' i limiti di sicurezza (gas massimo, dry-run,
riserva di ETH) stanno in un punto solo.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config

logger = logging.getLogger(__name__)

ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "totalSupply",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True,
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False,
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

WETH_ABI = [
    {
        "constant": False,
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "payable": True,
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "wad", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

MAX_UINT256 = 2 ** 256 - 1


class BaseChainError(RuntimeError):
    """Errore non recuperabile nell'interazione con la chain."""


class BaseClient:
    def __init__(self, rpc_url: str = None, private_key: str = None, address: str = None):
        self.rpc_url = rpc_url or config.BASE_RPC_URL
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": config.HTTP_TIMEOUT}))
        # Base e' una L2 OP-stack: gli header hanno extraData fuori standard
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self._private_key = private_key if private_key is not None else config.PRIVATE_KEY
        self.account = Account.from_key(self._private_key) if self._private_key else None

        configured = address or config.WALLET_ADDRESS
        if configured:
            self.address = Web3.to_checksum_address(configured)
        elif self.account:
            self.address = self.account.address
        else:
            self.address = None

        if self.account and self.address and self.account.address.lower() != self.address.lower():
            raise BaseChainError(
                f"PRIVATE_KEY corrisponde a {self.account.address}, "
                f"ma WALLET_ADDRESS e' {self.address}"
            )

        self._decimals_cache: Dict[str, int] = {}
        self._symbol_cache: Dict[str, str] = {}

    # ------------------------------------------------------------ stato rete
    def is_connected(self) -> bool:
        try:
            return self.w3.is_connected()
        except Exception:
            return False

    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    def gas_price_gwei(self) -> float:
        return float(self.w3.from_wei(self.w3.eth.gas_price, "gwei"))

    def eth_balance(self, address: str = None) -> float:
        addr = Web3.to_checksum_address(address or self.address)
        return float(self.w3.from_wei(self.w3.eth.get_balance(addr), "ether"))

    # ------------------------------------------------------------ ERC-20
    def erc20(self, token_address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
        )

    def decimals(self, token_address: str) -> int:
        key = token_address.lower()
        if key not in self._decimals_cache:
            self._decimals_cache[key] = int(self.erc20(token_address).functions.decimals().call())
        return self._decimals_cache[key]

    def symbol(self, token_address: str) -> str:
        key = token_address.lower()
        if key not in self._symbol_cache:
            try:
                self._symbol_cache[key] = str(self.erc20(token_address).functions.symbol().call())
            except Exception:
                self._symbol_cache[key] = "???"
        return self._symbol_cache[key]

    def balance_of(self, token_address: str, address: str = None) -> int:
        addr = Web3.to_checksum_address(address or self.address)
        return int(self.erc20(token_address).functions.balanceOf(addr).call())

    def balance_of_float(self, token_address: str, address: str = None) -> float:
        raw = self.balance_of(token_address, address)
        return raw / (10 ** self.decimals(token_address))

    def allowance(self, token_address: str, spender: str, address: str = None) -> int:
        addr = Web3.to_checksum_address(address or self.address)
        return int(
            self.erc20(token_address)
            .functions.allowance(addr, Web3.to_checksum_address(spender))
            .call()
        )

    # ------------------------------------------------------------ verifica indirizzi
    def verify_contracts(self, tokens: Dict[str, Dict[str, Any]] = None) -> List[str]:
        """
        Controlla sulla chain che gli indirizzi configurati siano quello che
        diciamo che siano. Restituisce la lista dei problemi trovati (vuota = ok).
        """
        problems: List[str] = []
        tokens = tokens if tokens is not None else config.KNOWN_ASSETS

        for name, addr in (
            ("UNISWAP_V3_FACTORY", config.UNISWAP_V3_FACTORY),
            ("UNISWAP_V3_QUOTER_V2", config.UNISWAP_V3_QUOTER_V2),
            ("UNISWAP_V3_SWAP_ROUTER_02", config.UNISWAP_V3_SWAP_ROUTER_02),
            ("AAVE_V3_POOL", config.AAVE_V3_POOL),
        ):
            try:
                code = self.w3.eth.get_code(Web3.to_checksum_address(addr))
                if not code or code == b"":
                    problems.append(f"{name} ({addr}): nessun contratto a questo indirizzo")
            except Exception as exc:
                problems.append(f"{name} ({addr}): verifica fallita — {exc}")

        for sym, meta in tokens.items():
            addr = meta["address"]
            try:
                on_chain_symbol = self.erc20(addr).functions.symbol().call()
                on_chain_decimals = int(self.erc20(addr).functions.decimals().call())
            except Exception as exc:
                problems.append(f"{sym} ({addr}): non risponde come ERC-20 — {exc}")
                continue

            if str(on_chain_symbol).upper() != sym.upper():
                problems.append(
                    f"{sym} ({addr}): on-chain il symbol e' '{on_chain_symbol}'"
                )
            if "decimals" in meta and int(meta["decimals"]) != on_chain_decimals:
                problems.append(
                    f"{sym} ({addr}): decimals configurati {meta['decimals']}, "
                    f"on-chain {on_chain_decimals}"
                )

        return problems

    # ------------------------------------------------------------ WETH wrap/unwrap
    def wrap_eth(self, eth_amount: float) -> Dict[str, Any]:
        """
        Deposita ETH nativo nel contratto WETH canonico su Base (1:1).
        """
        self._require_signer()
        amount_wei = int(eth_amount * 1e18)
        if amount_wei <= 0:
            raise BaseChainError("Importo ETH non valido per wrap")

        weth_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.WETH), abi=WETH_ABI
        )
        tx = weth_contract.functions.deposit().build_transaction({
            "from": self.address,
            "value": amount_wei,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "chainId": config.CHAIN_ID,
        })
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        return self.send_transaction(tx, description=f"wrap {eth_amount:.5f} ETH -> WETH")

    def unwrap_weth(self, weth_amount: float) -> Dict[str, Any]:
        """
        Ritira WETH per ricevere ETH nativo 1:1.
        """
        self._require_signer()
        amount_wei = int(weth_amount * 1e18)
        if amount_wei <= 0:
            raise BaseChainError("Importo WETH non valido per unwrap")

        weth_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.WETH), abi=WETH_ABI
        )
        tx = weth_contract.functions.withdraw(amount_wei).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "chainId": config.CHAIN_ID,
        })
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        return self.send_transaction(tx, description=f"unwrap {weth_amount:.5f} WETH -> ETH")

    # ------------------------------------------------------------ transazioni
    def _require_signer(self):
        if not self.account:
            raise BaseChainError("PRIVATE_KEY non configurata: impossibile firmare")

    def _check_gas_price(self) -> int:
        gas_price = self.w3.eth.gas_price
        gwei = float(self.w3.from_wei(gas_price, "gwei"))
        if gwei > config.MAX_GAS_PRICE_GWEI:
            raise BaseChainError(
                f"Gas price {gwei:.4f} gwei sopra il massimo consentito "
                f"({config.MAX_GAS_PRICE_GWEI} gwei): transazione annullata"
            )
        return gas_price

    def send_transaction(self, tx: Dict[str, Any], description: str = "") -> Dict[str, Any]:
        """
        Firma e invia una transazione, poi aspetta la ricevuta.
        In dry-run stima soltanto il gas e non manda nulla.
        """
        self._require_signer()

        tx = dict(tx)
        tx.setdefault("from", self.address)
        tx.setdefault("chainId", config.CHAIN_ID)
        tx.setdefault("nonce", self.w3.eth.get_transaction_count(self.address))

        gas_price = self._check_gas_price()
        tx.setdefault("gasPrice", gas_price)

        if "gas" not in tx:
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.25)
            except Exception as exc:
                if config.DRY_RUN:
                    logger.debug("[DRY-RUN] Stima gas fallita (normale se senza stato on-chain): %s", exc)
                    tx["gas"] = 250_000
                else:
                    raise BaseChainError(f"Stima gas fallita per '{description}': {exc}") from exc

        if config.DRY_RUN:
            logger.info("[DRY-RUN] transazione non inviata: %s", description)
            return {
                "status": "dry_run",
                "description": description,
                "gas": tx.get("gas"),
                "gas_price_gwei": float(self.w3.from_wei(gas_price, "gwei")),
                "to": tx.get("to"),
                "value": tx.get("value", 0),
            }

        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=config.TX_TIMEOUT_SECONDS
        )

        result = {
            "status": "success" if receipt.status == 1 else "failed",
            "description": description,
            "tx_hash": receipt.transactionHash.hex(),
            "block": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "explorer": f"https://basescan.org/tx/{receipt.transactionHash.hex()}",
        }
        if receipt.status != 1:
            raise BaseChainError(f"Transazione fallita on-chain: {result['explorer']}")
        return result

    def ensure_allowance(self, token_address: str, spender: str, amount: int) -> Optional[Dict[str, Any]]:
        """Approva lo spender se l'allowance corrente non basta."""
        current = self.allowance(token_address, spender)
        if current >= amount:
            return None

        logger.info(
            "Allowance insufficiente per %s (%s < %s): invio approve",
            self.symbol(token_address), current, amount,
        )
        contract = self.erc20(token_address)
        tx = contract.functions.approve(
            Web3.to_checksum_address(spender), MAX_UINT256
        ).build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "chainId": config.CHAIN_ID,
        })
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        return self.send_transaction(tx, description=f"approve {self.symbol(token_address)}")

    def deadline(self) -> int:
        return int(time.time()) + config.TX_DEADLINE_SECONDS
