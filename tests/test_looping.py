"""
Test offline del looping: matematica di leva e health factor, lettura della
configurazione Aave, simulazione paper e instradamento nel gestore.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import looping  # noqa: E402
import yield_scanner  # noqa: E402
from paper import PaperBook  # noqa: E402
from positions import PositionStore  # noqa: E402
from yield_manager import YieldManager  # noqa: E402

YEAR = 31_536_000


def market(**over):
    m = {
        "loop_id": "loop:WSTETH", "asset": "WSTETH", "symbol": "wstETH/WETH",
        "token": config.LOOP_TOKENS["WSTETH"]["address"], "staking_apy": 3.0, "supply_apy": 0.1,
        "collateral_apy": 3.1, "borrow_apy": 2.0, "ltv": 0.93, "lt": 0.95, "emode_id": 1,
        "leverage": 3.0, "net_apy": looping.net_apy(3.0, 3.1, 2.0), "hf_target": 1.425,
        "price_usd": 3500.0, "weth_price_usd": 3000.0, "capacity_usd": 1e6,
        "weth_liquidity_usd": 5e7, "executable": True, "reason": "",
    }
    m.update(over)
    return m


class MathTest(unittest.TestCase):
    def test_net_apy(self):
        # 3x: 3 x 3.1% di collaterale, 2 x 2% di debito
        self.assertAlmostEqual(looping.net_apy(3, 3.1, 2.0), 5.3)
        self.assertAlmostEqual(looping.net_apy(1, 3.1, 50.0), 3.1)   # senza leva il debito non conta

    def test_health_factor_and_leverage_are_inverse(self):
        lev = looping.leverage_for_hf(1.25, 0.95)
        self.assertAlmostEqual(looping.health_factor(lev, lev - 1, 0.95), 1.25)
        self.assertEqual(looping.health_factor(100, 0, 0.8), float("inf"))
        self.assertEqual(looping.leverage_for_hf(0.9, 0.95), float("inf"))

    def test_target_leverage_respects_every_limit(self):
        saved = (config.LOOP_MAX_LEVERAGE, config.LOOP_TARGET_HEALTH_FACTOR)
        try:
            config.LOOP_MAX_LEVERAGE, config.LOOP_TARGET_HEALTH_FACTOR = 3.0, 1.25
            self.assertAlmostEqual(looping.target_leverage(0.93, 0.95), 3.0)          # cap di config
            self.assertAlmostEqual(looping.target_leverage(0.75, 0.79), 1.25 / 0.46)  # cap da HF
            self.assertLess(looping.target_leverage(0.30, 0.95), 1.5)                 # cap da LTV
        finally:
            config.LOOP_MAX_LEVERAGE, config.LOOP_TARGET_HEALTH_FACTOR = saved

    def test_plan_reaches_target_within_ltv(self):
        borrows, coll, debt = looping.plan_iterations(1.0, 3.0, 0.93)
        self.assertAlmostEqual(coll, 3.0, places=6)
        self.assertAlmostEqual(debt, 2.0, places=6)
        # ogni prestito sta dentro l'LTV del collaterale accumulato fino a li'
        c, d = 1.0, 0.0
        for b in borrows:
            self.assertLessEqual(d + b, c * 0.93 * config.LOOP_LTV_BUFFER + 1e-12)
            c, d = c + b, d + b

    def test_plan_stops_at_iteration_limit(self):
        borrows, coll, _ = looping.plan_iterations(1.0, 10.0, 0.5, max_iterations=3)
        self.assertEqual(len(borrows), 3)
        self.assertLess(coll, 10.0)

    def test_reserve_config_bits(self):
        cfg = 9300 | (9500 << 16) | (18 << 48) | (1 << 56) | (1 << 58) | (4000 << 80) | (12000 << 116) | (1 << 168)
        r = looping.parse_reserve_config(cfg)
        self.assertEqual((r["ltv"], r["lt"], r["decimals"]), (0.93, 0.95, 18))
        self.assertTrue(r["active"] and r["borrowing"])
        self.assertFalse(r["frozen"] or r["paused"])
        self.assertEqual((r["borrow_cap"], r["supply_cap"], r["legacy_emode"]), (4000, 12000, 1))

    def test_rate_to_apy(self):
        self.assertAlmostEqual(looping.rate_to_apy(0), 0.0)
        self.assertAlmostEqual(looping.rate_to_apy(int(0.05 * 1e27)), 5.127, places=2)


class FakeReader:
    """Pool Aave finto per la scelta della categoria e-mode."""

    def __init__(self, categories):
        self.categories = categories
        self._emode_cache = {}

    def _category(self, cat):
        return self.categories.get(cat)


class EmodeTest(unittest.TestCase):
    def test_picks_category_with_both_bitmaps(self):
        coll, weth = {"id": 5, "legacy_emode": 0}, {"id": 2, "legacy_emode": 0}
        cats = {
            1: {"id": 1, "ltv": 0.90, "lt": 0.93, "collateral": 1 << 5, "borrowable": 1 << 3},   # WETH no
            2: {"id": 2, "ltv": 0.93, "lt": 0.95, "collateral": 1 << 5, "borrowable": 1 << 2},
            3: {"id": 3, "ltv": 0.80, "lt": 0.85, "collateral": 1 << 5, "borrowable": 1 << 2},
        }
        reader = FakeReader(cats)
        best = looping.AaveLoopReader.emode_for(reader, coll, weth)
        self.assertEqual(best["id"], 2)

    def test_disabled_emode(self):
        saved = config.LOOP_EMODE_CATEGORY
        try:
            config.LOOP_EMODE_CATEGORY = 0
            self.assertIsNone(looping.AaveLoopReader.emode_for(FakeReader({}), {"id": 1}, {"id": 2}))
        finally:
            config.LOOP_EMODE_CATEGORY = saved


class PaperLoopTest(unittest.TestCase):
    def setUp(self):
        self.book = PaperBook(path=os.path.join(tempfile.mkdtemp(), "p.json"), start_usdc=1000)

    def test_open_accrue_close(self):
        m = market()
        res = self.book.open_loop("loop:WSTETH", m, 500, eth_price=3000)
        self.assertAlmostEqual(self.book.usdc, 500)
        self.assertGreater(res["cost_usd"], 0)
        self.assertAlmostEqual(res["leverage"], 3.0, places=6)
        self.assertAlmostEqual(res["health_factor"], 1.425, places=6)
        pos = self.book.positions["loop:WSTETH"]
        v0 = pos["value_usd"]

        # un anno allo stesso prezzo: l'equity cresce del netto (composto sulle due gambe)
        earned = self.book.accrue_loops({"WSTETH": m}, 3000, now=pos["last_accrual"] + YEAR)
        self.assertAlmostEqual(earned / v0 * 100, m["net_apy"], delta=0.2)

        # ETH -10%: l'equity (in ETH) perde il 10% in dollari
        v1 = self.book.positions["loop:WSTETH"]["value_usd"]
        self.book.accrue_loops({}, 2700)
        self.assertAlmostEqual(self.book.positions["loop:WSTETH"]["value_usd"], v1 * 0.9, places=6)

        out = self.book.close_loop("loop:WSTETH", 1.0, 2700)
        self.assertNotIn("loop:WSTETH", self.book.positions)
        self.assertLess(out["withdrawn_usd"], v1 * 0.9)
        self.assertGreater(out["withdrawn_usd"], v1 * 0.9 * 0.95)

    def test_plain_accrue_skips_loops(self):
        self.book.open_loop("loop:WSTETH", market(), 500, 3000)
        before = dict(self.book.positions["loop:WSTETH"])
        self.book.accrue({"loop:WSTETH": 50.0}, now=time.time() + YEAR)
        self.assertEqual(self.book.positions["loop:WSTETH"]["collateral_eth"], before["collateral_eth"])

    def test_missing_rates_do_not_lose_time(self):
        self.book.open_loop("loop:WSTETH", market(), 500, 3000)
        t0 = self.book.positions["loop:WSTETH"]["last_accrual"]
        self.book.accrue_loops({}, 3000, now=t0 + 3600)
        self.assertEqual(self.book.positions["loop:WSTETH"]["last_accrual"], t0)


class FakeClient:
    address = "0x" + "c" * 40


class ManagerLoopTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.m = YieldManager.__new__(YieldManager)
        self.m.client = FakeClient()
        self.m.paper = PaperBook(path=os.path.join(tmp, "paper.json"), start_usdc=1000)
        self.m.store = PositionStore(os.path.join(tmp, "pos.json"))
        self.m.adapters = {}
        self.m._prices = {"USDC": 1.0, "WETH": 3000.0}
        self.m._uniswap = None
        self.m.loops = looping.LoopManager(self.m)
        self.m.loops.markets = {"WSTETH": market()}
        self.m.universe = self.m.loops.pseudo_pools()
        self._orig = yield_scanner.pools_by_id
        yield_scanner.pools_by_id = lambda ids: {}

    def tearDown(self):
        yield_scanner.pools_by_id = self._orig

    def deposit(self, portion=0.5):
        return self.m.execute_signal({"operation": "deposit", "pool": "loop:WSTETH",
                                      "target_portion_of_portfolio": portion},
                                     self.m.get_account_status())

    def test_pseudo_pool_only_when_worth_it(self):
        self.assertEqual([p["pool_id"] for p in self.m.universe], ["loop:WSTETH"])
        self.m.loops.markets = {"WSTETH": market(net_apy=config.LOOP_MIN_NET_APY - 1)}
        self.assertEqual(self.m.loops.pseudo_pools(), [])
        self.m.loops.markets = {"WSTETH": market(executable=False, reason="cap")}
        self.assertEqual(self.m.loops.pseudo_pools(), [])
        text = yield_scanner.format_universe_for_prompt(self.m.universe)
        self.assertIn("LOOP wstETH/WETH", text)

    def test_deposit_capped_by_loop_share(self):
        res = self.deposit(1.0)
        self.assertEqual(res["status"], "paper", res)
        self.assertAlmostEqual(res["amount_usd"], 1000 * min(config.LOOP_MAX_CAPITAL_PCT, config.MAX_POOL_PCT))
        status = self.m.get_account_status()
        row = status["positions"][0]
        self.assertAlmostEqual(row["loop"]["leverage"], 3.0, places=4)
        self.assertAlmostEqual(row["loop"]["health_factor"], 1.425, places=4)
        self.assertAlmostEqual(row["apy_now"], market()["net_apy"], places=4)
        self.assertAlmostEqual(status["total_value_usd"], 1000 - res["cost_usd"], places=4)
        self.assertIn("loops", status)

    def test_health_factor_exit(self):
        self.deposit(0.2)
        pos = self.m.paper.positions["loop:WSTETH"]
        pos["debt_eth"] = pos["collateral_eth"] * pos["lt"] / (config.LOOP_MIN_HEALTH_FACTOR - 0.02)
        exits = self.m.risk_exits(self.m.get_account_status())
        self.assertTrue(exits and exits[0]["trigger"].startswith("HEALTH_FACTOR"), exits)
        res = self.m.liquidate(exits[0], exits[0]["trigger"])
        self.assertEqual(res["status"], "paper", res)
        self.assertNotIn("loop:WSTETH", self.m.paper.positions)
        self.assertNotIn("loop:WSTETH", self.m.store.positions)

    def test_negative_carry_needs_consecutive_cycles(self):
        self.deposit(0.2)
        self.m.loops.markets = {"WSTETH": market(borrow_apy=20.0, net_apy=looping.net_apy(3, 3.1, 20))}
        for _ in range(config.LOOP_EXIT_CYCLES - 1):
            self.assertEqual(self.m.risk_exits(self.m.get_account_status()), [])
        exits = self.m.risk_exits(self.m.get_account_status())
        self.assertTrue(exits and exits[0]["trigger"].startswith("CARRY_NEGATIVO"), exits)

    def test_carry_counter_resets(self):
        self.deposit(0.2)
        self.m.loops.markets = {"WSTETH": market(borrow_apy=20.0)}
        self.m.risk_exits(self.m.get_account_status())
        self.m.loops.markets = {"WSTETH": market()}
        self.m.risk_exits(self.m.get_account_status())
        self.assertEqual(self.m.store.get("loop:WSTETH").get("negative_cycles"), 0)

    def test_rebalance_refused_and_withdraw_works(self):
        self.deposit(0.2)
        self.m.store.positions["loop:WSTETH"]["opened_at"] = time.time() - 72 * 3600
        res = self.m.execute_signal({"operation": "rebalance", "from_pool": "loop:WSTETH", "pool": "loop:WSTETH"},
                                    self.m.get_account_status())
        self.assertEqual(res["status"], "rejected")
        res = self.m.execute_signal({"operation": "withdraw", "pool": "loop:WSTETH", "withdraw_portion": 0.5},
                                    self.m.get_account_status())
        self.assertEqual(res["status"], "paper", res)
        self.assertIn("loop:WSTETH", self.m.paper.positions)

    def test_dry_run_plan(self):
        self.m.paper = None
        saved = config.DRY_RUN
        try:
            config.DRY_RUN = True
            pool = self.m.universe[0]
            res = self.m.loops.open(pool, 300, {"operation": "deposit"})
        finally:
            config.DRY_RUN = saved
        self.assertEqual(res["status"], "dry_run")
        self.assertTrue(any("borrow" in step for step in res["plan"]))


if __name__ == "__main__":
    unittest.main()
