import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from core.trade_manager import TradeManager
from core.state_manager import COOLDOWN


class TestHandlePositionUpdate(unittest.IsolatedAsyncioTestCase):
    """Tests for TradeManager.handle_position_update outcome routing."""

    def setUp(self):
        self.risk     = MagicMock()
        self.telegram = MagicMock()

        state                 = MagicMock()
        state.open_positions  = {}
        state.h4_sweep_candle = {}
        state.h4_swing_level  = {}
        state.reconnecting    = False
        self.state = state

        self.tm = TradeManager(
            deriv_client=MagicMock(),
            state_manager=self.state,
            risk_manager=self.risk,
            telegram=self.telegram,
        )

    def _put_position(self, symbol, contract_id='abc123', direction='BUY'):
        """Seed state with an open position so handle_position_update can find it."""
        self.state.open_positions[symbol] = {
            'contract_id': contract_id,
            'direction':   direction,
            'stake':       10.0,
        }
        self.state.h4_sweep_candle[symbol] = None
        self.state.h4_swing_level[symbol]  = None

    # ── outcome routing ───────────────────────────────────

    async def test_won_status_calls_record_win(self):
        self._put_position('R_75')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'won', 'profit': 8.0},
        )
        self.risk.record_win.assert_called_once()
        self.risk.record_loss.assert_not_called()

    async def test_lost_status_calls_record_loss(self):
        self._put_position('R_75', direction='SELL')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'lost', 'profit': -10.0},
        )
        self.risk.record_loss.assert_called_once()
        self.risk.record_win.assert_not_called()

    async def test_sold_with_positive_profit_calls_record_win(self):
        # status='sold' + profit > 0 → the code's `profit > 0` branch fires
        self._put_position('R_75')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'sold', 'profit': 5.0},
        )
        self.risk.record_win.assert_called_once()
        self.risk.record_loss.assert_not_called()

    # ── contract-id mismatch guard ────────────────────────

    async def test_contract_id_mismatch_is_ignored(self):
        self._put_position('R_75', contract_id='abc123')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'xyz999', 'status': 'won', 'profit': 8.0},
        )
        self.risk.record_win.assert_not_called()
        self.risk.record_loss.assert_not_called()

    # ── no open position ──────────────────────────────────

    async def test_unknown_symbol_is_ignored(self):
        # open_positions is empty → position is None → early return
        await self.tm.handle_position_update(
            'R_50',
            {'contract_id': 'abc123', 'status': 'won', 'profit': 8.0},
        )
        self.risk.record_win.assert_not_called()
        self.risk.record_loss.assert_not_called()

    # ── post-close state changes ──────────────────────────

    async def test_closed_trade_sends_telegram_alert(self):
        self._put_position('R_75')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'won', 'profit': 8.0},
        )
        self.telegram.send.assert_called_once()

    async def test_closed_trade_nulls_position_in_state(self):
        self._put_position('R_75')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'lost', 'profit': -10.0},
        )
        self.assertIsNone(self.state.open_positions['R_75'])

    async def test_closed_trade_sets_cooldown_state(self):
        self._put_position('R_75')
        await self.tm.handle_position_update(
            'R_75',
            {'contract_id': 'abc123', 'status': 'won', 'profit': 8.0},
        )
        self.state.set_state.assert_called_with('R_75', COOLDOWN)


class TestOpenTradeContractUpdateFailure(unittest.IsolatedAsyncioTestCase):
    """Tests for the contract_update retry + emergency-close + kill-switch path."""

    def setUp(self):
        self.telegram = MagicMock()

        state = MagicMock()
        state.open_positions  = {}
        state.reconnecting    = False
        state.trading_enabled = True
        self.state = state

        self.client = MagicMock()
        self.client.subscribe_to_contract = AsyncMock()

        self.tm = TradeManager(
            deriv_client=self.client,
            state_manager=self.state,
            risk_manager=MagicMock(),
            telegram=self.telegram,
        )

    async def test_contract_update_double_failure_closes_and_kills(self):
        """When contract_update fails twice, the bot sends an emergency sell
        and activates the kill-switch."""
        self.client.send = AsyncMock(side_effect=[
            # Step 1: proposal
            {'proposal': {'id': 'p1'}},
            # Step 2: buy
            {'buy': {'contract_id': 'cid1', 'start_spot': 100.0}},
            # Step 3a: contract_update (first attempt) — fails
            {'error': {'message': 'SL update rejected'}},
            # Step 3b: contract_update (retry) — fails again
            {'error': {'message': 'Still failing'}},
            # Emergency sell
            {'sell': {'contract_id': 'cid1', 'sold_for': 9.5}},
        ])

        with patch('config.ACCOUNT_BALANCE') as mock_bal:
            mock_bal.value = 1000.0
            await self.tm.open_trade(
                symbol='R_75',
                direction='BUY',
                entry_price=100.0,
                sl=99.0,
                tp=102.0,
                stake=10.0,
                order_block=None,
            )

        # Emergency sell must have been sent
        sell_call_args = [
            call.args[0] for call in self.client.send.call_args_list
        ]
        self.assertIn({'sell': 'cid1', 'price': 0}, sell_call_args)

        # Kill-switch must be activated
        self.assertFalse(self.state.trading_enabled)
        self.state.save_daily_stats.assert_called()

        # Telegram must have been notified with KILL SWITCH message
        sent_messages = [call.args[0] for call in self.telegram.send.call_args_list]
        self.assertTrue(any('KILL SWITCH' in m for m in sent_messages))

        # Position must NOT be recorded (contract is being closed)
        self.assertNotIn('R_75', self.state.open_positions)

        # Contract subscription must NOT happen (we returned early)
        self.client.subscribe_to_contract.assert_not_called()

    async def test_contract_update_retry_success_continues_normally(self):
        """When the first contract_update fails but the retry succeeds,
        the trade proceeds normally without triggering the kill-switch."""
        self.client.send = AsyncMock(side_effect=[
            {'proposal': {'id': 'p1'}},
            {'buy': {'contract_id': 'cid2', 'start_spot': 100.0}},
            {'error': {'message': 'Transient error'}},   # first attempt fails
            {'contract_update': {'status': 'ok'}},       # retry succeeds
        ])

        with patch('config.ACCOUNT_BALANCE') as mock_bal:
            mock_bal.value = 1000.0
            await self.tm.open_trade(
                symbol='R_75',
                direction='BUY',
                entry_price=100.0,
                sl=99.0,
                tp=102.0,
                stake=10.0,
                order_block=None,
            )

        # Kill-switch must remain off
        self.assertTrue(self.state.trading_enabled)

        # Position must be recorded
        self.assertIn('R_75', self.state.open_positions)
        self.assertEqual(self.state.open_positions['R_75']['contract_id'], 'cid2')

        # Subscription must have happened
        self.client.subscribe_to_contract.assert_called_once_with('cid2')


if __name__ == '__main__':
    unittest.main()
