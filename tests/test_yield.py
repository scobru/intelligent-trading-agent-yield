"""
Test offline: screening, parsing della decisione, paper book e limiti
dell'esecutore. Nessuna chiamata di rete, nessun RPC.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import protocols  # noqa: E402
import yield_scanner  # noqa: E402
from paper import PaperBook  # noqa: E402
from positions import PositionStore  # noqa: E402
from yield_agent import _clean_and_parse_json  # noqa: E402
from yield_manager import YieldManager  # noqa: E402

USDC = config.USDC.lower()
WETH = config.WETH.lower()


def llama_pool(**over):
    pool = {
        "chain": "Base", "project": "aave-v3", "symbol": "USDC",
        "pool": "7e0661bf-8cf3-45e6-9424-31916d4c7b84",
        "tvlUsd": 150_000_000, "apy": 5.0, "apyBase": 4.5, "apyReward": 0.5,
        "apyMean30d": 4.8, "apyPct7D": 0.1, "exposure": "single", "ilRisk": "no",
        "stablecoin": True, "underlyingTokens": [USDC], "outlier": False, "count": 400,
    }
    pool.update(over)
    return pool


class ScreeningTest(unittest.TestCase):
    def test_good_pool_passes(self):
        self.assertIsNone(yield_scanner.screen_pool(llama_pool()))

    def test_rejections_keep_reason(self):
        cases = {
            "LP": llama_pool(exposure="multi"),
            "impermanent": llama_pool(ilRisk="yes"),
            "TVL": llama_pool(tvlUsd=50_000),
            "outlier": llama_pool(outlier=True),
            "emissioni": llama_pool(apy=10, apyBase=1, apyReward=9),
            "allowlist": llama_pool(project="random-farm"),
            "DEPOSIT_ASSETS": llama_pool(symbol="WETH", underlyingTokens=[WETH]),
            "storico": llama_pool(count=3),
            "credibile": llama_pool(apy=90, apyBase=90, apyReward=0),
            "gestito": llama_pool(underlyingTokens=["0x" + "1" * 40]),
        }
        for needle, pool in cases.items():
            reason = yield_scanner.screen_pool(pool)
            self.assertIsNotNone(reason, needle)
            self.assertIn(needle, reason, f"{needle}: {reason}")

    def test_watchlist_bypasses_only_allowlist(self):
        addr = "0x" + "a" * 40
        pool = llama_pool(project="new-vault", pool=addr)
        old = config.POOL_WATCHLIST
        try:
            config.POOL_WATCHLIST = {addr}
            self.assertIsNone(yield_scanner.screen_pool(pool))
            self.assertIsNotNone(yield_scanner.screen_pool(dict(pool, tvlUsd=10)))
        finally:
            config.POOL_WATCHLIST = old

    def test_effective_apy_discounts_rewards(self):
        pool = llama_pool(apyBase=4.0, apyReward=2.0)
        self.assertAlmostEqual(yield_scanner.effective_apy(pool), 4.0 + 2.0 * config.REWARD_APY_WEIGHT)

    def test_score_dampens_spikes_and_prefers_depth(self):
        steady = yield_scanner.score_pool(llama_pool(apyBase=6, apyReward=0, apyMean30d=6))
        spike = yield_scanner.score_pool(llama_pool(apyBase=12, apyReward=0, apyMean30d=4))
        self.assertLess(spike, 12 * 0.5 + 12 * 0.5)  # non vale il picco pieno
        self.assertLessEqual(spike, 4 * 1.5)
        shallow = yield_scanner.score_pool(llama_pool(apyBase=6, apyReward=0, apyMean30d=6, tvlUsd=1_000_000))
        self.assertGreater(steady, shallow)

    def test_screen_sorts_and_normalizes(self):
        vault = "0x" + "b" * 40
        approved, rejected = yield_scanner.screen([
            llama_pool(),
            llama_pool(project="morpho-v1", pool=vault, apyBase=7, apyMean30d=7, apyReward=0),
            llama_pool(exposure="multi"),
        ])
        self.assertEqual([p["project"] for p in approved], ["morpho-v1", "aave-v3"])
        self.assertEqual(approved[0]["addresses"], [vault])
        self.assertEqual(approved[0]["asset"], "USDC")
        self.assertEqual(len(rejected), 1)


class DecisionParsingTest(unittest.TestCase):
    def test_fenced_json_and_aliases(self):
        d = _clean_and_parse_json('```json\n{"operation":"DEPOSIT","pool_id":"abc",'
                                  '"target_portion_of_portfolio":1.7}\n```')
        self.assertEqual(d["operation"], "deposit")
        self.assertEqual(d["pool"], "abc")
        self.assertEqual(d["target_portion_of_portfolio"], 1.0)

    def test_unknown_operation_becomes_hold(self):
        self.assertEqual(_clean_and_parse_json('{"operation":"buy"}')["operation"], "hold")

    def test_bad_numbers_are_neutralized(self):
        d = _clean_and_parse_json('{"operation":"withdraw","pool":"x","withdraw_portion":"tutto"}')
        self.assertEqual(d["withdraw_portion"], 1.0)


class PaperBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book = PaperBook(path=os.path.join(self.tmp, "p.json"), start_usdc=1000, start_eth=0.01)

    def test_accrual_matches_apy(self):
        pool = {"pool_id": "p1", "asset": "USDC"}
        dep = self.book.deposit(pool, 500)
        self.assertAlmostEqual(dep["deposited_usd"], 500 - 2 * config.PAPER_GAS_USD)
        start = self.book.positions["p1"]["last_accrual"]
        earned = self.book.accrue({"p1": 10.0}, now=start + 31_536_000)
        self.assertAlmostEqual(earned, dep["deposited_usd"] * 0.10, places=6)

    def test_withdraw_and_reload(self):
        self.book.deposit({"pool_id": "p1", "asset": "USDC"}, 400)
        res = self.book.withdraw("p1", 1.0)
        self.assertNotIn("p1", self.book.positions)
        self.assertGreater(res["withdrawn_usd"], 399)
        again = PaperBook(path=self.book.path)
        self.assertAlmostEqual(again.usdc, self.book.usdc)

    def test_swap_costs_slippage(self):
        dep = self.book.deposit({"pool_id": "w", "asset": "WETH"}, 100)
        self.assertLess(dep["deposited_usd"], 100 - 100 * config.DEFAULT_SLIPPAGE_BPS / 10_000 + 1e-9)


class FakeClient:
    address = "0x" + "c" * 40


def pool_entry(pid, project, apy, adapter="erc4626", asset="USDC"):
    return {
        "pool_id": pid, "project": project, "symbol": asset, "asset": asset,
        "asset_address": USDC, "addresses": [], "effective_apy": apy, "tvl_usd": 50e6,
        "adapter": adapter, "contract": "0x" + "d" * 40, "pool_meta": None,
    }


class ManagerLimitsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.m = YieldManager.__new__(YieldManager)
        self.m.client = FakeClient()
        self.m.paper = PaperBook(path=os.path.join(tmp, "paper.json"), start_usdc=1000)
        self.m.store = PositionStore(os.path.join(tmp, "pos.json"))
        self.m.adapters = {}
        self.m._prices = {"USDC": 1.0}
        self.m._uniswap = None
        self.m.universe = [
            pool_entry("aave", "aave-v3", 4.0, adapter="aave-v3"),
            pool_entry("morpho", "morpho-v1", 6.5),
            pool_entry("fluid", "fluid-lending", 4.3),
            pool_entry("watch", "euler-v2", 9.0, adapter=None),
        ]

    def status(self, positions=(), usdc=1000.0):
        invested = sum(p["value_usd"] for p in positions)
        return {"total_value_usd": usdc + invested, "usdc_balance": usdc, "eth_balance": 1.0,
                "positions": list(positions)}

    def position(self, pid, project, value, apy, hours=48):
        return {"pool_id": pid, "project": project, "symbol": "USDC", "asset": "USDC",
                "value_usd": value, "apy_now": apy, "held_hours": hours, "entry_tvl": 50e6,
                "tvl_now": 50e6}

    def test_deposit_is_clamped_to_pool_cap(self):
        res = self.m.execute_signal({"operation": "deposit", "pool": "morpho",
                                     "target_portion_of_portfolio": 1.0}, self.status())
        self.assertEqual(res["status"], "paper")
        self.assertAlmostEqual(res["amount_usd"], 1000 * config.MAX_POOL_PCT)

    def test_protocol_cap_counts_existing_exposure(self):
        held = self.position("aave-other", "aave-v3", 550, 3.0)
        cap = self.m._deposit_cap(self.m.universe[0], self.status([held], usdc=450))
        self.assertAlmostEqual(cap, config.MAX_PROTOCOL_PCT * 1000 - 550)

    def test_non_executable_pool_is_rejected(self):
        res = self.m.execute_signal({"operation": "deposit", "pool": "watch",
                                     "target_portion_of_portfolio": 0.2}, self.status())
        self.assertEqual(res["status"], "rejected")
        self.assertIn("non eseguibile", res["reason"])

    def test_unknown_pool_is_rejected(self):
        res = self.m.execute_signal({"operation": "deposit", "pool": "nope",
                                     "target_portion_of_portfolio": 0.2}, self.status())
        self.assertEqual(res["status"], "rejected")

    def test_max_positions(self):
        held = [self.position(f"p{i}", f"proto{i}", 10, 3.0) for i in range(config.MAX_POSITIONS)]
        res = self.m.execute_signal({"operation": "deposit", "pool": "morpho",
                                     "target_portion_of_portfolio": 0.1}, self.status(held, usdc=900))
        self.assertEqual(res["status"], "rejected")
        self.assertIn("posizioni", res["reason"])

    def test_rebalance_needs_real_improvement(self):
        held = self.position("fluid", "fluid-lending", 400, 4.3)
        res = self.m.execute_signal({"operation": "rebalance", "from_pool": "fluid", "pool": "aave"},
                                    self.status([held], usdc=600))
        self.assertEqual(res["status"], "rejected")
        self.assertIn("miglioramento", res["reason"])

    def test_rebalance_respects_min_hold(self):
        held = self.position("fluid", "fluid-lending", 400, 4.3, hours=1)
        res = self.m.execute_signal({"operation": "rebalance", "from_pool": "fluid", "pool": "morpho"},
                                    self.status([held], usdc=600))
        self.assertEqual(res["status"], "rejected")
        self.assertIn("minimo", res["reason"])

    def test_rebalance_breakeven_on_tiny_position(self):
        held = self.position("fluid", "fluid-lending", 12, 4.3)
        res = self.m.execute_signal({"operation": "rebalance", "from_pool": "fluid", "pool": "morpho"},
                                    self.status([held], usdc=988))
        self.assertEqual(res["status"], "rejected")
        self.assertIn("ripaga", res["reason"])

    def test_switch_cost_includes_swaps(self):
        plain = YieldManager.switch_cost_usd(1000, "USDC", "USDC")
        swapped = YieldManager.switch_cost_usd(1000, "USDC", "WETH")
        self.assertGreater(swapped, plain + 1000 * config.DEFAULT_SLIPPAGE_BPS / 10_000 - 1e-9)

    def test_risk_exit_on_tvl_collapse(self):
        ok = self.position("a", "aave-v3", 100, 4)
        bad = dict(self.position("b", "morpho-v1", 100, 6), tvl_now=10e6)
        exits = self.m.risk_exits(self.status([ok, bad]))
        self.assertEqual([e["pool_id"] for e in exits], ["b"])
        self.assertTrue(exits[0]["trigger"].startswith("TVL_DROP"))

    def test_rebalance_moves_paper_capital(self):
        self.m.paper.deposit({"pool_id": "fluid", "asset": "USDC"}, 400)
        self.m.store.record_deposit(self.m.universe[2], {"kind": "erc4626"}, 400, 400)
        self.m.store.positions["fluid"]["opened_at"] = time.time() - 72 * 3600
        orig = yield_scanner.pools_by_id
        yield_scanner.pools_by_id = lambda ids: {}
        try:
            status = self.m.get_account_status()
            res = self.m.execute_signal({"operation": "rebalance", "from_pool": "fluid",
                                         "pool": "morpho", "withdraw_portion": 1.0}, status)
        finally:
            yield_scanner.pools_by_id = orig
        self.assertEqual(res["status"], "paper", res)
        self.assertNotIn("fluid", self.m.paper.positions)
        self.assertGreater(self.m.paper.value_of("morpho"), 399)
        self.assertIn("morpho", self.m.store.positions)
        self.assertNotIn("fluid", self.m.store.positions)

    def test_withdraw_requires_held_pool(self):
        res = self.m.execute_signal({"operation": "withdraw", "pool": "morpho"}, self.status())
        self.assertEqual(res["status"], "rejected")


class ResolveTest(unittest.TestCase):
    def test_aave_routed_by_project(self):
        calls = []

        class FakeAave:
            kind = "aave-v3"

            def __init__(self, client, asset):
                calls.append(asset)

        orig = protocols.AaveV3Adapter
        protocols.AaveV3Adapter = FakeAave
        try:
            adapter, reason = protocols.resolve(None, {"project": "aave-v3", "asset_address": USDC})
        finally:
            protocols.AaveV3Adapter = orig
        self.assertIsInstance(adapter, FakeAave)
        self.assertEqual(calls, [USDC])

    def test_no_address_means_not_executable(self):
        adapter, reason = protocols.resolve(None, {"project": "morpho-v1", "asset_address": USDC,
                                                   "addresses": []})
        self.assertIsNone(adapter)
        self.assertIn("indirizzo", reason)

    def test_raw_conversions(self):
        self.assertEqual(protocols.float_to_raw(12.345678, "USDC"), 12_345_678)
        self.assertAlmostEqual(protocols.raw_to_float(10 ** 18, "WETH"), 1.0)


if __name__ == "__main__":
    unittest.main()
