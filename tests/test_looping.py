"""
Test per la modalita' Looping (leveraged staking yield su Aave V3).
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import protocols
import yield_scanner
from paper import PaperBook
from yield_manager import YieldManager


class LoopApyCalculationTest(unittest.TestCase):
    def test_net_apy_formula(self):
        staking_yield = 3.2
        supply_apy = 0.1
        borrow_apy = 2.0
        leverage = 2.2

        # Net APY = (staking_yield + supply_apy) * L - borrow_apy * (L - 1)
        expected_net = (staking_yield + supply_apy) * leverage - borrow_apy * (leverage - 1.0)
        self.assertAlmostEqual(expected_net, 4.86, places=2)

    def test_health_factor_scaling(self):
        # Health Factor Aave V3 e' in 1e18
        hf_raw = int(1.55 * 1e18)
        hf_scaled = round(hf_raw / 1e18, 4)
        self.assertEqual(hf_scaled, 1.55)


class LoopScannerTest(unittest.TestCase):
    def test_build_loop_pools_with_wsteth_and_cbeth(self):
        mock_pools = [
            {
                "chain": "Base", "project": "aave-v3", "symbol": "WETH",
                "pool": "weth-pool-id", "apyBase": 1.4, "tvlUsd": 40_000_000,
            },
            {
                "chain": "Base", "project": "aave-v3", "symbol": "WSTETH",
                "pool": "wsteth-pool-id", "apyBase": 0.01, "tvlUsd": 35_000_000,
            },
            {
                "chain": "Base", "project": "aave-v3", "symbol": "CBETH",
                "pool": "cbeth-pool-id", "apyBase": 0.01, "tvlUsd": 120_000_000,
            }
        ]

        approved, rejected = yield_scanner.build_loop_pools(mock_pools)
        self.assertEqual(len(approved), 2)
        pids = [p["pool_id"] for p in approved]
        self.assertIn("aave-v3-loop-wsteth-weth", pids)
        self.assertIn("aave-v3-loop-cbeth-weth", pids)

        wsteth_loop = next(p for p in approved if p["pool_id"] == "aave-v3-loop-wsteth-weth")
        self.assertTrue(wsteth_loop["is_loop"])
        self.assertEqual(wsteth_loop["project"], "aave-v3-loop")
        self.assertEqual(wsteth_loop["asset"], "wstETH")
        self.assertEqual(wsteth_loop["borrow_asset"], "WETH")
        self.assertEqual(wsteth_loop["leverage"], config.LOOP_MAX_LEVERAGE)
        self.assertGreaterEqual(wsteth_loop["effective_apy"], config.LOOP_MIN_NET_APY)

    def test_build_loop_pools_rejected_if_low_apy(self):
        mock_pools = [
            {
                "chain": "Base", "project": "aave-v3", "symbol": "WETH",
                "pool": "weth-pool-id", "apyBase": 10.0, "tvlUsd": 40_000_000, # borrow rate altissimo
            },
            {
                "chain": "Base", "project": "aave-v3", "symbol": "WSTETH",
                "pool": "wsteth-pool-id", "apyBase": 0.0, "tvlUsd": 35_000_000,
            },
        ]
        approved, rejected = yield_scanner.build_loop_pools(mock_pools)
        self.assertEqual(len(approved), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("sotto soglia", rejected[0]["reason"])


class LoopPaperTradingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        self.book = PaperBook(path=self.tmp.name, start_usdc=1000.0)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_deposit_and_withdraw_loop_position(self):
        loop_pool = {
            "pool_id": "aave-v3-loop-wsteth-weth",
            "project": "aave-v3-loop",
            "symbol": "wstETH-WETH (2.2x Loop)",
            "asset": "wstETH",
            "is_loop": True,
            "leverage": 2.2,
            "target_health_factor": 1.55,
        }

        dep = self.book.deposit(loop_pool, 500.0)
        self.assertGreater(dep["deposited_usd"], 0)
        self.assertLess(self.book.usdc, 500.01)

        pos = self.book.positions["aave-v3-loop-wsteth-weth"]
        self.assertTrue(pos["is_loop"])
        self.assertEqual(pos["leverage"], 2.2)
        self.assertEqual(pos["health_factor"], 1.55)

        # Accrual
        earned = self.book.accrue({"aave-v3-loop-wsteth-weth": 5.0}, now=pos["last_accrual"] + 86400 * 30)
        self.assertGreater(earned, 0)
        self.assertGreater(pos["value_usd"], dep["deposited_usd"])

        # Withdraw
        withdrawn = self.book.withdraw("aave-v3-loop-wsteth-weth", 1.0)
        self.assertGreater(withdrawn["withdrawn_usd"], 0)
        self.assertNotIn("aave-v3-loop-wsteth-weth", self.book.positions)


class LoopRiskExitsTest(unittest.TestCase):
    def test_risk_exit_triggered_on_low_health_factor(self):
        client = MagicMock()
        client.address = "0x" + "1" * 40
        manager = YieldManager(client)

        status_unsafe = {
            "positions": [
                {
                    "pool_id": "aave-v3-loop-wsteth-weth",
                    "project": "aave-v3-loop",
                    "symbol": "wstETH-WETH (2.2x Loop)",
                    "asset": "wstETH",
                    "value_usd": 1000.0,
                    "is_loop": True,
                    "leverage": 2.2,
                    "health_factor": 1.25, # Sotto 1.30!
                }
            ]
        }

        exits = manager.risk_exits(status_unsafe)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["trigger"], "HEALTH_FACTOR_LOW_1.25")

    def test_risk_exit_not_triggered_on_safe_health_factor(self):
        client = MagicMock()
        client.address = "0x" + "1" * 40
        manager = YieldManager(client)

        status_safe = {
            "positions": [
                {
                    "pool_id": "aave-v3-loop-wsteth-weth",
                    "project": "aave-v3-loop",
                    "symbol": "wstETH-WETH (2.2x Loop)",
                    "asset": "wstETH",
                    "value_usd": 1000.0,
                    "is_loop": True,
                    "leverage": 2.2,
                    "health_factor": 1.55, # Sicuro
                }
            ]
        }

        exits = manager.risk_exits(status_safe)
        self.assertEqual(len(exits), 0)


class LoopPlanTest(unittest.TestCase):
    def test_plan_deposit_shows_loop_steps(self):
        client = MagicMock()
        client.address = "0x" + "1" * 40
        manager = YieldManager(client)

        loop_pool = {
            "pool_id": "aave-v3-loop-wsteth-weth",
            "project": "aave-v3-loop",
            "symbol": "wstETH-WETH (2.2x Loop)",
            "asset": "wstETH",
            "borrow_asset": "WETH",
            "is_loop": True,
            "leverage": 2.2,
            "adapter": "aave-v3-loop",
            "contract": config.AAVE_V3_POOL,
        }

        plan = manager._plan_deposit(loop_pool, 500.0)
        plan_text = " ".join(plan)
        self.assertIn("swap $500.00 USDC -> wstETH", plan_text)
        self.assertIn("supply wstETH come collateral", plan_text)
        self.assertIn("loop: borrow WETH", plan_text)
        self.assertIn("2.2x", plan_text)


if __name__ == "__main__":
    unittest.main()
