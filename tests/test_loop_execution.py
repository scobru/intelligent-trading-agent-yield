"""
Test offline dell'esecuzione live del looping, contro un Aave simulato in
memoria che applica LTV e health factor come quello vero: un prestito oltre
l'LTV o un prelievo che porterebbe l'HF sotto 1 viene rifiutato.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import loop_math  # noqa: E402
from base_client import BaseChainError  # noqa: E402
from positions import PositionStore  # noqa: E402
from yield_manager import YieldManager  # noqa: E402

LST = config.WSTETH
WETH = config.WETH
USDC = config.USDC
E18 = 10 ** 18


class Revert(BaseChainError):
    pass


class FakeChain:
    """Wallet e conto Aave. Prezzi in USD, collaterale e debito in raw (18 decimali)."""

    def __init__(self, ltv=0.80, lt=0.83, lst_price=3600.0, eth_price=3000.0):
        self.address = "0x" + "c" * 40
        self.wallet = {USDC.lower(): 1000 * 10 ** 6, LST.lower(): 0, WETH.lower(): 0}
        self.coll_raw = 0
        self.debt_raw = 0
        self.ltv, self.lt = ltv, lt
        self.lst_price, self.eth_price = lst_price, eth_price
        self.sent = []

    # -------- client
    def balance_of(self, token, address=None):
        return self.wallet[token.lower()]

    def decimals(self, token):
        return 6 if token.lower() == USDC.lower() else 18

    def ensure_allowance(self, token, spender, amount):
        return None

    # -------- Aave
    def coll_usd(self, raw=None):
        return (self.coll_raw if raw is None else raw) / E18 * self.lst_price

    def debt_usd(self, raw=None):
        return (self.debt_raw if raw is None else raw) / E18 * self.eth_price

    def supply(self, raw):
        self._take(LST, raw)
        self.coll_raw += raw

    def borrow(self, raw):
        if self.debt_usd(self.debt_raw + raw) > self.coll_usd() * self.ltv + 1e-9:
            raise Revert("borrow oltre l'LTV")
        self.debt_raw += raw
        self.wallet[WETH.lower()] += raw

    def repay(self, raw):
        paid = min(raw, self.debt_raw)
        self._take(WETH, paid)
        self.debt_raw -= paid

    def withdraw(self, raw):
        raw = self.coll_raw if raw is None else raw
        if raw > self.coll_raw:
            raise Revert("withdraw oltre il saldo")
        left = self.coll_raw - raw
        if self.debt_raw and loop_math.health_factor(self.coll_usd(left), self.debt_usd(), self.lt) < 1:
            raise Revert("withdraw porterebbe l'HF sotto 1")
        self.coll_raw = left
        self.wallet[LST.lower()] += raw

    def swap(self, token_in, token_out, raw, label):
        """Swap al prezzo, con lo 0.1% di costo."""
        usd = {USDC.lower(): 1.0, LST.lower(): self.lst_price, WETH.lower(): self.eth_price}
        self._take(token_in, raw)
        value = raw / 10 ** self.decimals(token_in) * usd[token_in.lower()] * 0.999
        self.wallet[token_out.lower()] += int(value / usd[token_out.lower()] * 10 ** self.decimals(token_out))
        return {"swap": label}

    def _take(self, token, raw):
        if self.wallet[token.lower()] < raw:
            raise Revert(f"saldo insufficiente di {token}")
        self.wallet[token.lower()] -= raw


class FakeLoopAdapter:
    kind = "aave-v3-loop"

    def __init__(self, chain, leverage=2.2):
        self.chain = chain
        self.asset = LST
        self.spender = config.AAVE_V3_POOL
        self.emode = None
        self.ltv, self.liq_threshold = chain.ltv, chain.lt
        self.leverage = loop_math.safe_leverage(self.ltv, self.liq_threshold, leverage)

    def _fn(self, action):
        return action

    def deposit_fn(self, raw):
        return lambda: self.chain.supply(raw)

    def borrow_fn(self, raw):
        return lambda: self.chain.borrow(raw)

    def repay_fn(self, raw=None):
        return lambda: self.chain.repay(self.chain.debt_raw if raw is None else raw)

    def withdraw_fn(self, raw, owner=None):
        return lambda: self.chain.withdraw(raw)

    def position(self, owner=None):
        c = self.chain
        coll, debt = c.coll_usd(), c.debt_usd()
        return {"coll_raw": c.coll_raw, "debt_raw": c.debt_raw, "coll_price": c.lst_price,
                "debt_price": c.eth_price, "collateral_usd": coll, "debt_usd": debt,
                "equity_usd": coll - debt, "lt": c.lt,
                "health_factor": loop_math.health_factor(coll, debt, c.lt)}

    def to_dict(self):
        return {"kind": self.kind, "address": self.spender, "asset": self.asset, "leverage": self.leverage}


def make_manager(chain, adapter, tmp):
    m = YieldManager.__new__(YieldManager)
    m.client = chain
    m.paper = None
    m.store = PositionStore(os.path.join(tmp, "pos.json"))
    m.adapters = {"loop": adapter}
    m._prices = {"USDC": 1.0, "WETH": chain.eth_price, "wstETH": chain.lst_price}
    m._uniswap = None
    m.universe = []

    def send(fn, description):
        fn()
        chain.sent.append(description)
        return {"tx": description}

    m._send = send
    m._swap = chain.swap
    return m


POOL = {"pool_id": "loop", "project": "aave-v3-loop", "symbol": "wstETH-WETH", "asset": "wstETH",
        "is_loop": True, "effective_apy": 5.0, "tvl_usd": 1e8}


class LoopMathTest(unittest.TestCase):
    def test_single_borrow_cannot_reach_leverage(self):
        # e' il motivo dei giri: a 2.2x il debito vale 1.2 volte il capitale,
        # piu' dell'LTV di qualunque collaterale
        borrows, coll, debt = loop_math.plan_iterations(1.0, 2.2, 0.80)
        self.assertGreater(len(borrows), 1)
        self.assertLessEqual(borrows[0], 0.80)
        self.assertAlmostEqual(coll, 2.2, places=6)
        self.assertAlmostEqual(debt, 1.2, places=6)

    def test_safe_leverage_limits(self):
        self.assertAlmostEqual(loop_math.safe_leverage(0.93, 0.95, 2.2), 2.2)
        # HF obiettivo 1.55 con LT 0.83: 1.55 / 0.72 = 2.15x
        self.assertAlmostEqual(loop_math.safe_leverage(0.80, 0.83, 3.0),
                               config.LOOP_TARGET_HEALTH_FACTOR / (config.LOOP_TARGET_HEALTH_FACTOR - 0.83))
        self.assertEqual(loop_math.safe_leverage(0.0, 0.0, 2.2), 1.0)

    def test_unwind_never_crosses_safe_hf(self):
        w = loop_math.next_unwind_usd(collateral_usd=2200, debt_usd=1200, target_debt_usd=0,
                                      liq_threshold=0.83, safe_hf=1.3)
        left = 2200 - w
        self.assertGreaterEqual(loop_math.health_factor(left, 1200, 0.83), 1.3 - 1e-9)

    def test_reserve_config_bits(self):
        cfg = 8000 | (8300 << 16) | (1 << 56) | (1 << 58) | (4000 << 80) | (12000 << 116) | (1 << 168)
        r = loop_math.parse_reserve_config(cfg)
        self.assertEqual((r["ltv"], r["lt"], r["borrow_cap"], r["supply_cap"], r["legacy_emode"]),
                         (0.80, 0.83, 4000, 12000, 1))
        self.assertTrue(r["active"] and r["borrowing"])
        self.assertFalse(r["frozen"] or r["paused"])

    def test_emode_needs_both_bitmaps(self):
        cats = {
            1: {"id": 1, "ltv": 0.90, "lt": 0.93, "collateral": 1 << 5, "borrowable": 1 << 3},
            2: {"id": 2, "ltv": 0.93, "lt": 0.95, "collateral": 1 << 5, "borrowable": 1 << 2},
        }
        best = loop_math.pick_emode(cats.get, {"id": 5, "legacy_emode": 0}, {"id": 2, "legacy_emode": 0})
        self.assertEqual(best["id"], 2)


class LiveLoopExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chain = FakeChain()
        self.adapter = FakeLoopAdapter(self.chain)
        self.m = make_manager(self.chain, self.adapter, self.tmp)

    def test_old_single_borrow_would_revert(self):
        # il vecchio codice prendeva (leva - 1) x collaterale in un colpo solo
        self.chain.wallet[LST.lower()] = E18
        self.chain.supply(E18)
        with self.assertRaises(Revert):
            self.chain.borrow(int(E18 * 1.2))

    def test_open_reaches_leverage_within_ltv(self):
        res = self.m._live_deposit(POOL, 500.0)
        pos = self.adapter.position()
        lev = pos["collateral_usd"] / pos["equity_usd"]
        self.assertAlmostEqual(lev, self.adapter.leverage, delta=0.05)
        self.assertGreater(sum(1 for d in self.chain.sent if d.startswith("borrow")), 1)
        self.assertGreaterEqual(pos["health_factor"], config.LOOP_TARGET_HEALTH_FACTOR - 0.05)
        self.assertAlmostEqual(res["leverage"], lev, places=3)
        self.assertIn("loop", self.m.store.positions)

    def test_full_unwind_repays_everything(self):
        self.m._live_deposit(POOL, 500.0)
        usdc_before = self.chain.wallet[USDC.lower()]
        res = self.m._live_unwind({"pool_id": "loop", "asset": "wstETH"}, self.adapter, 1.0)
        self.assertEqual(self.chain.debt_raw, 0)
        self.assertEqual(self.chain.coll_raw, 0)
        self.assertEqual(self.chain.wallet[WETH.lower()], 0)
        self.assertGreater(res["withdrawn_usd"], 480)        # 500 meno i costi degli swap
        self.assertAlmostEqual((self.chain.wallet[USDC.lower()] - usdc_before) / 1e6, res["withdrawn_usd"], places=3)
        self.assertNotIn("loop", self.m.store.positions)

    def test_partial_unwind_keeps_leverage(self):
        self.m._live_deposit(POOL, 500.0)
        lev0 = self.adapter.position()["collateral_usd"] / self.adapter.position()["equity_usd"]
        self.m._live_unwind({"pool_id": "loop", "asset": "wstETH"}, self.adapter, 0.5)
        pos = self.adapter.position()
        self.assertAlmostEqual(pos["equity_usd"], 250, delta=15)
        self.assertAlmostEqual(pos["collateral_usd"] / pos["equity_usd"], lev0, delta=0.1)
        self.assertIn("loop", self.m.store.positions)

    def test_unwind_after_depeg(self):
        # l'LST perde il 20% contro ETH: HF sotto la soglia, lo smontaggio deve riuscire lo stesso
        self.m._live_deposit(POOL, 500.0)
        self.chain.lst_price *= 0.80
        self.assertLess(self.adapter.position()["health_factor"], config.LOOP_MIN_HEALTH_FACTOR)
        self.m._live_unwind({"pool_id": "loop", "asset": "wstETH"}, self.adapter, 1.0)
        self.assertEqual(self.chain.debt_raw, 0)
        self.assertEqual(self.chain.coll_raw, 0)

    def test_status_values_equity_not_collateral(self):
        self.m._live_deposit(POOL, 500.0)
        meta = self.m.store.get("loop")
        row = self.m._live_loop_row.__func__(self.m, "loop", meta, POOL, _AccountAdapter(self.adapter))
        self.assertAlmostEqual(row["value_usd"], self.adapter.position()["equity_usd"], places=2)
        self.assertLess(row["value_usd"], self.adapter.position()["collateral_usd"] * 0.6)
        self.assertTrue(row["is_loop"])
        self.assertIsNotNone(row["health_factor"])


class _AccountAdapter:
    """Aggiunge getUserAccountData (HF del conto) al finto adapter."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def user_account_data(self):
        return {"health_factor": float("inf")}


if __name__ == "__main__":
    unittest.main()
