from __future__ import annotations
import asyncio, json, logging, websockets, config, credentials
from utils.logger import get_logger, log_event

class DerivClient:
    def __init__(self, state_manager, candle_store, on_candle, on_contract):
        self.state = state_manager
        self.candle_store = candle_store
        self.on_candle = on_candle
        self.on_contract = on_contract
        self.ws = None
        self.logger = get_logger('deriv_client')
        self._pending = {}
        self._req_id = 0
        self._pong = asyncio.Event()
        self._heartbeat = None

    async def connect(self):
        while True:
            recv_task = None
            try:
                url = f"{config.WS_ENDPOINT}?app_id={credentials.DERIV_APP_ID}"
                log_event(self.logger, logging.INFO, '------', f'Connecting to {url}')
                self.ws = await websockets.connect(url)
                recv_task = asyncio.create_task(self._recv_loop())
                await self._authorize()
                self.state.reconnecting = False
                await self._subscribe_all()
                if self._heartbeat and not self._heartbeat.done():
                    self._heartbeat.cancel()
                    try:
                        await self._heartbeat
                    except asyncio.CancelledError:
                        pass
                self._heartbeat = asyncio.create_task(self._heartbeat_loop())
                log_event(self.logger, logging.INFO, '------', 'Connected and Hunting.')
                await recv_task
            except Exception as e:
                log_event(self.logger, logging.WARNING, '------',
                          f'Connection lost or failed: {e} — reconnecting in {config.RECONNECT_DELAY}s')
                if recv_task and not recv_task.done():
                    recv_task.cancel()
                    try:
                        await recv_task
                    except asyncio.CancelledError:
                        pass
                if self.ws:
                    await self.ws.close()
                self.state.reconnecting = True
                # Clear all pending futures so callers don't hang forever
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError('WebSocket disconnected'))
                self._pending.clear()
                await asyncio.sleep(config.RECONNECT_DELAY)

    async def _authorize(self):
        try:
            res = await asyncio.wait_for(
                self.send({'authorize': credentials.DERIV_API_TOKEN}),
                timeout=30
            )
        except asyncio.TimeoutError:
            raise RuntimeError('Authorize timed out after 30 s')
        if res is None:
            raise RuntimeError('Authorization request returned None')
        if 'error' in res:
            raise RuntimeError(f'Authorization failed: {res["error"].get("message")}')
        auth = res.get('authorize', {})
        await config.ACCOUNT_BALANCE.set(float(auth.get('balance', 0)))
        self.state.balance_ready = True
        log_event(self.logger, logging.INFO, '------',
                  f'Authorised as {auth.get("loginid", "?")} | '
                  f'Account balance: ${config.ACCOUNT_BALANCE.value:.2f}')

    async def _subscribe_all(self):
        for s in config.SYMBOLS:
            for tf, g in config.TIMEFRAMES.items():
                await self._raw_send({
                    'ticks_history': s, 'granularity': g,
                    'style': 'candles', 'subscribe': 1,
                    'end': 'latest',
                    'count': config.BUFFER[tf],
                })
                await asyncio.sleep(config.SUBSCRIBE_DELAY)
        log_event(self.logger, logging.INFO, '------',
                  f'Subscribed to candles for {len(config.SYMBOLS)} symbols × '
                  f'{len(config.TIMEFRAMES)} TFs')

    async def _heartbeat_loop(self):
        while self.ws:
            await asyncio.sleep(config.HEARTBEAT_SECS)
            self._pong.clear()
            await self._raw_send({'ping': 1})
            try:
                await asyncio.wait_for(self._pong.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.state.reconnecting = True
                log_event(self.logger, logging.WARNING, '------',
                          'Heartbeat pong timeout — closing connection')
                break
        if self.ws:
            await self.ws.close()

    async def _recv_loop(self):
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log_event(self.logger, logging.WARNING, '------',
                          f'Received non-JSON message: {raw[:100]}')
                continue

            rid = msg.get('req_id')

            # ── Priority 1: resolve any pending Future by req_id ──
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg)
                continue

            # ── Priority 2: known streaming message types ──
            msg_type = msg.get('msg_type')

            if msg_type == 'ping':
                self._pong.set()
            elif msg_type == 'ohlc':
                await self._handle_ohlc(msg)
            elif msg_type == 'candles':
                await self._handle_candles(msg)
            elif msg_type == 'proposal_open_contract':
                # Streaming updates for open positions
                await self._handle_open_contract(msg)
            elif msg_type == 'proposal':
                # Streaming proposal updates (subscribe:1) — first response
                # resolved via req_id above, subsequent ones land here. Ignore.
                log_event(self.logger, logging.DEBUG, '------',
                          f'Streaming proposal update (no req_id) — ignoring')
            elif 'error' in msg:
                err = msg['error']
                log_event(self.logger, logging.WARNING, '------',
                          f'Unhandled API error: {err.get("code")} — {err.get("message")}')

    async def _handle_ohlc(self, msg):
        o = msg['ohlc']
        s, tf = o['symbol'], self._g_tf(o['granularity'])
        if tf is None:
            log_event(self.logger, logging.WARNING, '------',
                      f'Unknown granularity {o["granularity"]} for {s} — ignoring ohlc tick')
            return
        c = {
            'time': int(o['open_time']),
            'open': float(o['open']),
            'high': float(o['high']),
            'low': float(o['low']),
            'close': float(o['close']),
        }
        self.candle_store.add_candle(s, tf, c)
        if self.candle_store.is_ready(s):
            self.state.symbol_ready[s] = True
            # CRITICAL: dispatch as task so _recv_loop keeps reading WS
            # Awaiting here would deadlock: on_candle → send() waits for
            # a response that only _recv_loop can read.
            asyncio.create_task(self.on_candle(s, tf, c))

    async def _handle_candles(self, msg):
        s = msg['echo_req']['ticks_history']
        tf = self._g_tf(msg['echo_req']['granularity'])
        for r in msg.get('candles', []):
            self.candle_store.add_candle(s, tf, {
                'time': int(r['epoch']),
                'open': float(r['open']),
                'high': float(r['high']),
                'low': float(r['low']),
                'close': float(r['close']),
            })
        self.state.symbol_ready[s] = self.candle_store.is_ready(s)

    async def _handle_open_contract(self, msg):
        """Handle proposal_open_contract streaming updates — route to on_contract."""
        poc = msg.get('proposal_open_contract', {})
        symbol = poc.get('underlying', '')
        if symbol and self.on_contract:
            asyncio.create_task(self.on_contract(symbol, poc))

    async def subscribe_to_contract(self, contract_id):
        """Subscribe to real-time updates for an open contract (fire-and-forget)."""
        await self._raw_send({
            'proposal_open_contract': 1,
            'contract_id': contract_id,
            'subscribe': 1,
        })

    async def _raw_send(self, m):
        if self.ws:
            await self.ws.send(json.dumps(m))

    async def send(self, m):
        """Send a request and wait for the response matched by req_id."""
        self._req_id += 1
        m['req_id'] = self._req_id
        f = asyncio.get_running_loop().create_future()
        self._pending[m['req_id']] = f
        await self._raw_send(m)
        try:
            return await asyncio.wait_for(f, timeout=60)
        except asyncio.TimeoutError:
            self._pending.pop(m['req_id'], None)
            log_event(self.logger, logging.ERROR, '------',
                      f'Request timed out (req_id={m["req_id"]}, '
                      f'type={list(m.keys())[0]})')
            return None

    async def fetch_balance(self):
        r = await self.send({'balance': 1})
        if r is None or 'error' in r:
            raise RuntimeError('Failed to fetch balance')
        return float(r['balance']['balance'])

    def _g_tf(self, g):
        return {v: k for k, v in config.TIMEFRAMES.items()}.get(int(g))
