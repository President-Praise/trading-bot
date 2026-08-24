import unittest
from core.trade_manager import calculate_stake


class TestStakeCalculation(unittest.TestCase):
    """
    Tests for calculate_stake(entry_price, sl, multiplier, account_balance).

    Fix #11 update: tests now pass account_balance explicitly to avoid
    relying on the config.ACCOUNT_BALANCE global.
    """

    def test_stake_formula_correct(self):
        # risk = 1000 * 0.02 = 20; sl_dist = |100 - 95| = 5
        # stake = 20 * 100 / (10 * 5) = 40.0
        stake = calculate_stake(entry_price=100.0, sl=95.0, multiplier=10,
                                account_balance=1000.0)
        self.assertAlmostEqual(stake, 40.0)

    def test_stake_floored_to_minimum(self):
        # Tiny risk_amount → stake < 1.0 → floored to 1.0
        stake = calculate_stake(entry_price=100.0, sl=99.99, multiplier=10,
                                account_balance=1.0)
        self.assertEqual(stake, 1.0)

    def test_stake_capped_at_25_pct_balance(self):
        # Large sl_distance → raw stake would be small, but let's force cap
        # by using a balance of 100 and crafting a huge raw stake
        # risk = 100 * 0.02 = 2; sl_dist = 0.001; mult = 1
        # raw stake = 2 * 100 / (1 * 0.001) = 200_000 → capped at 100 * 0.25 = 25
        stake = calculate_stake(entry_price=100.0, sl=99.999, multiplier=1,
                                account_balance=100.0)
        self.assertEqual(stake, 25.0)

    def test_stake_rounded_to_two_decimals(self):
        stake = calculate_stake(entry_price=100.0, sl=96.667, multiplier=10,
                                account_balance=1000.0)
        self.assertEqual(stake, round(stake, 2))

    def test_stake_zero_sl_distance_returns_minimum(self):
        stake = calculate_stake(entry_price=100.0, sl=100.0, multiplier=10,
                                account_balance=1000.0)
        self.assertEqual(stake, 1.0)

    def test_stake_zero_entry_price_returns_minimum(self):
        stake = calculate_stake(entry_price=0.0, sl=95.0, multiplier=10,
                                account_balance=1000.0)
        self.assertEqual(stake, 1.0)

    def test_stake_uses_config_balance_when_not_provided(self):
        """Falls back to config.ACCOUNT_BALANCE when account_balance is None."""
        import config
        original = config.ACCOUNT_BALANCE
        try:
            config.ACCOUNT_BALANCE = type(config.ACCOUNT_BALANCE)(500.0)
            stake = calculate_stake(entry_price=100.0, sl=95.0, multiplier=10)
            # risk = 500 * 0.02 = 10; stake = 10 * 100 / (10 * 5) = 20
            self.assertAlmostEqual(stake, 20.0)
        finally:
            config.ACCOUNT_BALANCE = original


if __name__ == '__main__':
    unittest.main()
