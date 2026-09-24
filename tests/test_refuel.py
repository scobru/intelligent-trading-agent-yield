"""
Test unitari per la logica di auto-refuel ETH -> USDC nell'agente yield.
Nessuna chiamata di rete (mock offline).
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from tools.refuel import get_wallet_balances
from uniswap import Route, UniswapV3
from yield_manager import YieldManager


class YieldAutoRefuelTest(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.address = "0x1111111111111111111111111111111111111111"
        self.mock_client.w3 = MagicMock()
        self.uniswap = UniswapV3(self.mock_client)

    def test_refuel_skipped_if_usdc_above_threshold(self):
        # USDC = $10.0, soglia = $5.0 -> nessun refuel
        self.mock_client.balance_of_float.side_effect = lambda token: 10.0 if token == config.USDC else 0.0
        self.mock_client.eth_balance.return_value = 0.05

        res = self.uniswap.auto_refuel_usdc(
            gas_reserve=0.003, min_swap_eth=0.002, threshold_usdc=5.0
        )
        self.assertIsNone(res)

    def test_refuel_skipped_if_eth_below_reserve(self):
        # USDC = $0.0, ETH = 0.002, riserva gas = 0.003 -> eccesso < 0 -> skip
        self.mock_client.balance_of_float.side_effect = lambda token: 0.0
        self.mock_client.eth_balance.return_value = 0.002

        res = self.uniswap.auto_refuel_usdc(
            gas_reserve=0.003, min_swap_eth=0.002, threshold_usdc=5.0
        )
        self.assertIsNone(res)

    def test_refuel_skipped_if_excess_eth_below_min_swap(self):
        # USDC = $0.0, ETH = 0.004, riserva gas = 0.003 -> eccesso 0.001 < min_swap 0.002 -> skip
        self.mock_client.balance_of_float.side_effect = lambda token: 0.0
        self.mock_client.eth_balance.return_value = 0.004

        res = self.uniswap.auto_refuel_usdc(
            gas_reserve=0.003, min_swap_eth=0.002, threshold_usdc=5.0
        )
        self.assertIsNone(res)

    def test_refuel_triggers_when_usdc_zero_and_excess_eth_available(self):
        # USDC = $0.0, ETH = 0.05, riserva gas = 0.003 -> eccesso 0.047 >= min_swap
        self.mock_client.balance_of_float.side_effect = lambda token: 0.0
        self.mock_client.balance_of.return_value = 0  # 0 WETH raw
        self.mock_client.eth_balance.return_value = 0.05

        with patch.object(self.uniswap, "swap_eth_to_usdc") as mock_swap:
            mock_swap.return_value = {"status": "success", "tx_hash": "0xabc"}
            res = self.uniswap.auto_refuel_usdc(
                gas_reserve=0.003, min_swap_eth=0.002, threshold_usdc=5.0
            )
            self.assertIsNotNone(res)
            mock_swap.assert_called_once()
            swapped_eth = mock_swap.call_args[0][0]
            self.assertAlmostEqual(swapped_eth, 0.047, places=6)

    def test_swap_eth_to_usdc_wraps_shortfall_and_executes_swap(self):
        eth_to_swap = 0.02
        amount_wei = int(eth_to_swap * 1e18)

        self.mock_client.balance_of.return_value = 0
        self.mock_client.wrap_eth.return_value = {"status": "success", "tx_hash": "0xwrap"}

        fake_route = Route([config.WETH, config.USDC], [500], amount_wei, 50_000_000)
        with patch.object(self.uniswap, "best_route", return_value=fake_route):
            with patch.object(self.uniswap, "swap", return_value={"status": "success", "tx_hash": "0xswap"}):
                res = self.uniswap.swap_eth_to_usdc(eth_to_swap)
                self.mock_client.wrap_eth.assert_called_once_with(eth_to_swap)
                self.assertEqual(res["status"], "success")
                self.assertEqual(res["wrap_tx"], "0xwrap")
                self.assertEqual(res["eth_swapped"], eth_to_swap)

    def test_yield_manager_ensure_usdc_balance(self):
        manager = YieldManager(client=self.mock_client)
        with patch.object(manager.uniswap, "auto_refuel_usdc", return_value={"status": "success"}) as mock_refuel:
            res = manager.ensure_usdc_balance()
            self.assertEqual(res, {"status": "success"})
            mock_refuel.assert_called_once()

    def test_refuel_tool_balances(self):
        self.mock_client.balance_of_float.side_effect = lambda token: 0.0 if token == config.USDC else 0.01
        self.mock_client.eth_balance.return_value = 0.05

        balances = get_wallet_balances(self.mock_client)
        self.assertTrue(balances["needs_refuel"])
        self.assertAlmostEqual(balances["swappable_native_eth"], 0.047, places=4)
        self.assertAlmostEqual(balances["total_swappable_eth"], 0.057, places=4)


if __name__ == "__main__":
    unittest.main()
