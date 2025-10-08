#!/usr/bin/env python3
# bot_okx_macd_bb_hedge.py
"""
OKX 永续合约 5m MACD + Bollinger (双向持仓/Hedge) 自动策略
- 环境变量:
    OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
    SANDBOX (optional, 'true' to enable)
- 核心币对 & 杠杆规则写在 DEFAULT_SYMBOLS
"""

import os
import time
import math
import logging
from typing import Dict, Any, Optional

import ccxt
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
# 尝试从 .env 与 OKX.env 加载环境变量（若文件存在）
try:
    from dotenv import load_dotenv  # pip install python-dotenv
    # 默认加载 .env
    load_dotenv()
    logger.info(".env 已加载（若存在）")
    # 兼容自定义文件名 OKX.env（不覆盖已有环境变量）
    try:
        load_dotenv(dotenv_path='OKX.env', override=False)
        logger.info("OKX.env 已加载（若存在）")
    except Exception:
        pass
except Exception:
    # 未安装或其它异常时忽略，仍可用系统环境变量
    pass

def _getenv_strip(key: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白"""
    val = os.getenv(key, default)
    return val.strip() if isinstance(val, str) else val

# ---------------- Config ----------------
def _getenv_multi(keys, default=""):
    for k in keys:
        v = _getenv_strip(k, "")
        if v:
            if k != keys[0]:
                logger.warning(f"Using alternate env var '{k}' for '{keys[0]}'")
            return v
    return default

ENV_ALIASES = {
    'OKX_API_KEY': ['OKX_API_KEY', 'OKX_KEY'],
    'OKX_API_SECRET': [
        'OKX_API_SECRET',
        'OKX_SECRET',
        'OKX_API_SECRETS',
        'OKX_SECRET_KEY',
        'OKX_API_SECRET_KEY',
        'API_SECRET'
    ],
    'OKX_API_PASSPHRASE': ['OKX_API_PASSPHRASE', 'OKX_PASSPHRASE', 'OKX_PASSWORD', 'OKX_API_PASSWORD'],
    'SANDBOX': ['SANDBOX']
}

OKX_API_KEY = _getenv_multi(ENV_ALIASES['OKX_API_KEY'], '')
OKX_API_SECRET = _getenv_multi(ENV_ALIASES['OKX_API_SECRET'], '')
OKX_API_PASSPHRASE = _getenv_multi(ENV_ALIASES['OKX_API_PASSPHRASE'], '')
SANDBOX = _getenv_strip('SANDBOX', 'false').lower() in ('1', 'true', 'yes')

# 启动时仅打印是否存在，便于在 Railway Logs 排查；不打印真实值
for base, aliases in ENV_ALIASES.items():
    try:
        present = any(bool(os.getenv(a)) for a in aliases)
        logger.info(f"{base} present={present}")
    except Exception:
        logger.info(f"{base} present=False")

# 额外调试：列出所有包含“OKX”的环境变量名（不含值），帮助定位键名不一致问题
try:
    okx_keys = [k for k in os.environ.keys() if 'OKX' in k.upper()]
    logger.info(f"OKX-related env keys detected: {okx_keys}")
except Exception:
    pass

TIMEFRAME = '5m'
BB_PERIOD = 20
BB_STD = 2
MACD_FAST = 6
MACD_SLOW = 16
MACD_SIGNAL = 9
SLEEP_SECONDS = 20  # loop sleep

# your desired symbols (unified ccxt format)
DEFAULT_SYMBOLS = ['FIL/USDT', 'ZRO/USDT', 'WIF/USDT', 'WLD/USDT']

# leverage rules (symbol base name -> leverage)
DEFAULT_LEVERAGES = {'ZRO': 20, 'default': 30}

# trading mode: 'cross' or 'isolated'
TD_MODE = os.getenv('TD_MODE', 'cross')

# minimum USDT exposure per symbol (to avoid zero orders)
# 支持通过环境变量配置，兼容 MIN_USDT_PER_SYMBOL 与 PER_SYMBOL_MIN_USDT
def _getfloat_env(keys, default: float):
    for k in keys:
        v = _getenv_strip(k, '')
        if v:
            try:
                return float(v)
            except Exception:
                logger.warning(f"环境变量 {k} 值无效（需为数字），已使用默认 {default}")
                break
    return default

MIN_USDT_PER_SYMBOL = _getfloat_env(['MIN_USDT_PER_SYMBOL', 'PER_SYMBOL_MIN_USDT'], 0.5)  # still try for tiny balances

# ---------------- Indicators ----------------
def compute_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - signal_line
    return macd, signal_line, hist

def compute_bollinger(close: pd.Series, period=20, std=2.0):
    ma = close.rolling(window=period).mean()
    sd = close.rolling(window=period).std()
    upper = ma + (sd * std)
    lower = ma - (sd * std)
    return ma, upper, lower

# ---------------- Exchange wrapper ----------------
class OKXHedgeBot:
    def __init__(self, symbols):
        self.symbols = symbols
        self.exchange = ccxt.okx({
            'apiKey': OKX_API_KEY,
            'secret': OKX_API_SECRET,
            'password': OKX_API_PASSPHRASE,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        if SANDBOX:
            logger.warning("SANDBOX MODE ENABLED")
            self.exchange.set_sandbox_mode(True)

        # load markets: limit to swap to avoid OKX entries with missing base/quote
        self.markets = self.exchange.load_markets(True, {'type': 'swap'})
        # map symbol->market & instId
        self.market_map = {}
        for s in symbols:
            m = self.markets.get(s)
            if not m:
                logger.error(f"Symbol {s} not in exchange.load_markets()")
                raise ValueError(f"{s} not available")
            inst = None
            info = m.get('info', {})
            inst = info.get('instId') or info.get('symbol') or m.get('id')
            self.market_map[s] = {'market': m, 'instId': inst}
        # prepare market info (min size, step)
        self._prepare_market_infos()
        # try to set position mode to hedge (OKX: dual/hedge)
        self._ensure_hedge_mode()

    def _prepare_market_infos(self):
        for s, meta in self.market_map.items():
            m = meta['market']
            info = m.get('info', {}) or {}
            min_sz = None
            size_inc = None
            tick = None
            for key in ('minSz', 'min_size', 'min_size', 'minSize'):
                if info.get(key) is not None:
                    try:
                        min_sz = float(info.get(key))
                        break
                    except Exception:
                        pass
            for key in ('lotSz', 'sizeIncrement', 'increment', 'size_step'):
                if info.get(key) is not None:
                    try:
                        size_inc = float(info.get(key))
                        break
                    except Exception:
                        pass
            for key in ('tickSz', 'tickSize', 'priceIncrement', 'price_step'):
                if info.get(key) is not None:
                    try:
                        tick = float(info.get(key))
                        break
                    except Exception:
                        pass
            if min_sz is None:
                min_sz = m.get('limits', {}).get('amount', {}).get('min', 0.000001)
            if size_inc is None:
                size_inc = m.get('precision', {}).get('amount', 0.000001)
            if tick is None:
                tick = m.get('precision', {}).get('price', 0.01)
            meta.update({'min_size': min_sz, 'size_increment': size_inc, 'tick': tick})
            logger.info(f"{s} min_size={min_sz} size_inc={size_inc} tick={tick}")

    def _ensure_hedge_mode(self):
        """将 OKX 设置为双向持仓（hedge, long_short_mode）"""
        try:
            if hasattr(self.exchange, 'set_position_mode'):
                self.exchange.set_position_mode(True)
                logger.info("Position mode set to hedge (long_short_mode)")
            else:
                logger.warning("exchange.set_position_mode 不可用；请在 OKX 后台确认账户持仓模式为双向（对冲）。")
        except Exception as e:
            logger.warning(f"Unable to set hedge mode automatically: {e}")

    def fetch_balance_usdt(self) -> float:
        try:
            # OKX 合约账户可加类型参数；不加也可，由 ccxt 适配
            bal = self.exchange.fetch_balance()
            usdt = 0.0
            # 优先从 free 读取
            free = bal.get('free') or {}
            if 'USDT' in free:
                usdt = float(free.get('USDT') or 0)
            else:
                # 兼容不同结构
                totals = bal.get('total') or {}
                for k, v in totals.items():
                    if str(k).upper() == 'USDT':
                        usdt = float(v or 0)
                        break
            return usdt
        except ccxt.AuthenticationError as e:
            logger.error(f"认证失败：请检查 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 是否配置正确。详细：{e}")
            return 0.0
        except Exception as e:
            logger.exception(f"fetch_balance 异常：{e}")
            return 0.0

    def safe_amount(self, symbol: str, raw_amount: float) -> float:
        meta = self.market_map[symbol]
        step = meta.get('size_increment', 1e-6)
        min_size = meta.get('min_size', 1e-6)
        if raw_amount <= 0:
            return 0.0
        # compute precision digits
        precision = 0
        if step < 1:
            precision = int(round(-math.log10(step)))
        amount = float(round(raw_amount, precision))
        if amount < min_size:
            amount = min_size
        return amount

    def set_leverage_for_symbol(self, symbol: str, leverage: int):
        instId = self.market_map[symbol]['instId']
        try:
            params = {'instId': instId, 'lever': str(leverage), 'mgnMode': 'cross'}
            try:
                resp = self.exchange.private_post_account_set_leverage(params)
                logger.info(f"Set leverage {leverage} for {symbol} resp: {resp}")
            except Exception:
                # some wrappers use privatePostAccountSetLeverage
                try:
                    resp = self.exchange.privatePostAccountSetLeverage(params)
                    logger.info(f"Set leverage alt {leverage} resp: {resp}")
                except Exception as e:
                    logger.warning(f"Could not set leverage via private endpoint: {e}")
        except Exception as e:
            logger.exception(f"set_leverage error: {e}")

    def fetch_ohlcv_df(self, symbol: str, timeframe: str = TIMEFRAME, limit: int = 200) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        return df

    def get_positions(self, symbol: str) -> Dict[str, float]:
        """
        Return dict: {'long': size_float, 'short': size_float}
        sizes are absolute contract amounts (may be raw contract units)
        """
        res = {'long': 0.0, 'short': 0.0}
        instId = self.market_map[symbol]['instId']
        # try unified fetch positions
        try:
            if hasattr(self.exchange, 'fetch_positions'):
                try:
                    raw = self.exchange.fetch_positions([symbol])
                    for p in raw:
                        info = p.get('info', {})
                        # adapt to structure: check posSide if exists
                        pos_side = info.get('posSide') or info.get('side') or p.get('side')
                        size = float(p.get('contracts') or p.get('positions') or p.get('amount') or 0)
                        if pos_side:
                            if pos_side.lower().startswith('long'):
                                res['long'] += abs(size)
                            elif pos_side.lower().startswith('short'):
                                res['short'] += abs(size)
                        else:
                            # fallback: infer by sign
                            try:
                                if float(size) > 0:
                                    res['long'] += abs(float(size))
                            except Exception:
                                pass
                    return res
                except Exception:
                    pass

            # fallback to raw private endpoint
            try:
                resp = None
                try:
                    resp = self.exchange.private_get_account_positions({'instId': instId})
                except Exception:
                    try:
                        resp = self.exchange.privateGetAccountPositions({'instId': instId})
                    except Exception:
                        resp = None
                if resp:
                    data = resp.get('data') or resp
                    if isinstance(data, list):
                        for item in data:
                            # item may have 'posSide' or 'side' and 'pos' or 'availPos'
                            pside = item.get('posSide') or item.get('side') or ''
                            qty = float(item.get('pos') or item.get('availPos') or item.get('position') or 0)
                            if isinstance(pside, str) and pside.lower().startswith('long'):
                                res['long'] += abs(qty)
                            elif isinstance(pside, str) and pside.lower().startswith('short'):
                                res['short'] += abs(qty)
                    else:
                        # single object
                        pside = data.get('posSide') or data.get('side') or ''
                        qty = float(data.get('pos') or data.get('availPos') or data.get('position') or 0)
                        if pside.lower().startswith('long'):
                            res['long'] += abs(qty)
                        elif pside.lower().startswith('short'):
                            res['short'] += abs(qty)
                return res
            except Exception as e:
                logger.exception(f"get_positions fallback error: {e}")
                return res
        except Exception as e:
            logger.exception(f"get_positions error: {e}")
            return res

    def create_market_order(self, symbol: str, side: str, pos_side: str, amount_usdt: float) -> Optional[Dict[str, Any]]:
        """
        side: 'buy' (open long) or 'sell' (open short)
        pos_side: 'long' or 'short' (for hedge)
        amount_usdt: funds to use (USDT)
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            price = float(ticker['last'])
            if price <= 0:
                logger.error("invalid price")
                return None
            leverage = DEFAULT_LEVERAGES.get(symbol.split('/')[0], DEFAULT_LEVERAGES['default'])
            # compute contract qty approximate: (amount_usdt * leverage) / price
            raw_qty = (amount_usdt * leverage) / price
            qty = self.safe_amount(symbol, raw_qty)
            if qty <= 0:
                logger.warning("qty <= 0 after safe_amount")
                return None
            instId = self.market_map[symbol]['instId']
            params = {'instId': instId, 'tdMode': TD_MODE, 'posSide': pos_side}
            logger.info(f"Placing MARKET order: {symbol} side={side} posSide={pos_side} qty={qty} params={params}")
            order = self.exchange.create_order(symbol, 'market', side, qty, None, params)
            logger.info(f"create_order resp: {order}")
            return order
        except Exception as e:
            logger.exception(f"create_market_order error: {e}")
            return None

    def close_side_position(self, symbol: str, pos_side: str) -> bool:
        """
        Close a single side by placing an opposite reduceOnly market order for that pos_side.
        pos_side: 'long' or 'short' (the side to close)
        """
        try:
            positions = self.get_positions(symbol)
            size = positions.get(pos_side, 0.0)
            if not size or size <= 0:
                logger.info(f"No {pos_side} position to close on {symbol}")
                return True
            # To close long: place 'sell' with reduceOnly and posSide='long'
            side = 'sell' if pos_side == 'long' else 'buy'
            instId = self.market_map[symbol]['instId']
            qty = self.safe_amount(symbol, size)
            params = {'instId': instId, 'tdMode': TD_MODE, 'posSide': pos_side, 'reduceOnly': True}
            logger.info(f"Closing {pos_side} on {symbol} via market {side} qty={qty} params={params}")
            order = self.exchange.create_order(symbol, 'market', side, qty, None, params)
            logger.info(f"close order resp: {order}")
            return True
        except Exception as e:
            logger.exception(f"close_side_position error: {e}")
            return False

# ---------------- Strategy & Loop ----------------
def check_signals_from_df(df: pd.DataFrame):
    close = df['close']
    macd, signal_line, hist = compute_macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    ma, bb_upper, bb_lower = compute_bollinger(close, BB_PERIOD, BB_STD)
    i = -1
    latest_macd = macd.iloc[i]
    latest_signal = signal_line.iloc[i]
    prev_macd = macd.iloc[i - 1]
    prev_signal = signal_line.iloc[i - 1]
    latest_close = close.iloc[i]
    prev_close = close.iloc[i - 1]
    latest_mid = ma.iloc[i]

    macd_goldencross = (prev_macd < prev_signal) and (latest_macd > latest_signal)
    macd_deathcross = (prev_macd > prev_signal) and (latest_macd < latest_signal)
    cross_mid_up = (prev_close < ma.iloc[i - 1]) and (latest_close > latest_mid)
    cross_mid_down = (prev_close > ma.iloc[i - 1]) and (latest_close < latest_mid)

    return {
        'long_entry': macd_goldencross and cross_mid_up,
        'long_exit': macd_deathcross or (latest_close < latest_mid),
        'short_entry': macd_deathcross and cross_mid_down,
        'short_exit': macd_goldencross or (latest_close > latest_mid),
        'latest_close': latest_close,
        'mid': latest_mid
    }

def main_loop(symbols):
    bot = OKXHedgeBot(symbols)
    logger.info("Starting hedge bot main loop.")
    while True:
        try:
            usdt_balance = bot.fetch_balance_usdt()
            logger.info(f"Free USDT balance: {usdt_balance}")
            n = len(symbols)
            # equal split of free balance to each symbol
            if usdt_balance <= 0:
                logger.warning("USDT balance zero or cannot fetch. Sleeping.")
                time.sleep(10)
                continue
            per_symbol_usdt = max(MIN_USDT_PER_SYMBOL, usdt_balance / n)
            logger.info(f"Allocating {per_symbol_usdt} USDT per symbol (n={n})")

            for sym in symbols:
                try:
                    # set leverage per symbol
                    base = sym.split('/')[0]
                    lev = DEFAULT_LEVERAGES.get(base, DEFAULT_LEVERAGES['default'])
                    bot.set_leverage_for_symbol(sym, lev)

                    df = bot.fetch_ohlcv_df(sym, TIMEFRAME, limit=200)
                    if len(df) < max(BB_PERIOD, MACD_SLOW) + 2:
                        logger.warning(f"not enough bars for {sym}")
                        continue
                    signals = check_signals_from_df(df)
                    positions = bot.get_positions(sym)
                    logger.info(f"{sym} signals={signals} positions={positions}")

                    # Long logic (independent)
                    if signals['long_entry']:
                        # open long even if short exists (hedge mode allows both)
                        logger.info(f"{sym}: long_entry -> attempt open long")
                        bot.create_market_order(sym, 'buy', 'long', per_symbol_usdt)
                    elif signals['long_exit']:
                        # close long side if exists
                        logger.info(f"{sym}: long_exit -> attempt close long")
                        bot.close_side_position(sym, 'long')

                    # Short logic (independent)
                    if signals['short_entry']:
                        logger.info(f"{sym}: short_entry -> attempt open short")
                        bot.create_market_order(sym, 'sell', 'short', per_symbol_usdt)
                    elif signals['short_exit']:
                        logger.info(f"{sym}: short_exit -> attempt close short")
                        bot.close_side_position(sym, 'short')

                    # small sleep between symbols to avoid rate limits
                    time.sleep(1)
                except Exception as e_sym:
                    logger.exception(f"Error processing {sym}: {e_sym}")
                    time.sleep(1)

            time.sleep(SLEEP_SECONDS)
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Exiting.")
            break
        except Exception as e:
            logger.exception(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == '__main__':
    missing = [k for k, v in [
        ('OKX_API_KEY', OKX_API_KEY),
        ('OKX_API_SECRET', OKX_API_SECRET),
        ('OKX_API_PASSPHRASE', OKX_API_PASSPHRASE),
    ] if not v]
    if missing:
        logger.error(f"缺少环境变量: {', '.join(missing)}。请在部署环境（如 Railway Variables）中配置后重启。")
        raise SystemExit(1)
    logger.info(f"Starting with symbols={DEFAULT_SYMBOLS}, SANDBOX={SANDBOX}")
    main_loop(DEFAULT_SYMBOLS)
