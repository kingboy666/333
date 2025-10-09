#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MACD策略实现 - RAILWALL平台版本
25倍杠杆，无限制交易，带挂单识别和状态同步
增加胜率统计和盈亏显示
"""
import time
import logging
import datetime
import os
import json
from typing import Dict, Any, List, Optional
import pytz

import ccxt
import pandas as pd
import numpy as np
import math

# 配置日志 - 使用中国时区和UTF-8编码
class ChinaTimeFormatter(logging.Formatter):
    """中国时区的日志格式化器"""
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=pytz.timezone('Asia/Shanghai'))
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.strftime('%Y-%m-%d %H:%M:%S')
        return s

# 配置日志 - 确保RAILWAY平台兼容
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
formatter = ChinaTimeFormatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.propagate = False  # 防止重复日志

class TradingStats:
    """交易统计类"""
    def __init__(self, stats_file: str = 'trading_stats.json'):
        self.stats_file = stats_file
        self.stats = {
            'total_trades': 0,
            'win_trades': 0,
            'loss_trades': 0,
            'total_pnl': 0.0,
            'total_win_pnl': 0.0,
            'total_loss_pnl': 0.0,
            'trades_history': []
        }
        self.load_stats()
    
    def load_stats(self):
        """加载统计数据"""
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, 'r') as f:
                    self.stats = json.load(f)
                logger.info(f"✅ 加载历史统计数据：总交易{self.stats['total_trades']}笔")
        except Exception as e:
            logger.warning(f"⚠️ 加载统计数据失败: {e}，使用新数据")
    
    def save_stats(self):
        """保存统计数据"""
        try:
            with open(self.stats_file, 'w') as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            logger.error(f"❌ 保存统计数据失败: {e}")
    
    def add_trade(self, symbol: str, side: str, pnl: float):
        """添加交易记录"""
        self.stats['total_trades'] += 1
        self.stats['total_pnl'] += pnl
        
        if pnl > 0:
            self.stats['win_trades'] += 1
            self.stats['total_win_pnl'] += pnl
        else:
            self.stats['loss_trades'] += 1
            self.stats['total_loss_pnl'] += pnl
        
        # 添加交易历史 - 使用北京时间
        china_tz = pytz.timezone('Asia/Shanghai')
        trade_record = {
            'timestamp': datetime.datetime.now(china_tz).strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'side': side,
            'pnl': round(pnl, 4)
        }
        self.stats['trades_history'].append(trade_record)
        
        # 只保留最近100条记录
        if len(self.stats['trades_history']) > 100:
            self.stats['trades_history'] = self.stats['trades_history'][-100:]
        
        self.save_stats()
    
    def get_win_rate(self) -> float:
        """计算胜率"""
        if self.stats['total_trades'] == 0:
            return 0.0
        return (self.stats['win_trades'] / self.stats['total_trades']) * 100
    
    def get_summary(self) -> str:
        """获取统计摘要"""
        win_rate = self.get_win_rate()
        return (f"📊 交易统计: 总计{self.stats['total_trades']}笔 | "
                f"胜{self.stats['win_trades']}笔 负{self.stats['loss_trades']}笔 | "
                f"胜率{win_rate:.1f}% | "
                f"总盈亏{self.stats['total_pnl']:.2f}U | "
                f"盈利{self.stats['total_win_pnl']:.2f}U 亏损{self.stats['total_loss_pnl']:.2f}U")

class MACDStrategy:
    """MACD策略类"""
    def __init__(self, api_key: str, secret_key: str, passphrase: str):
        """初始化策略"""
        # 交易所配置
        self.exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret_key,
            'password': passphrase,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',  # 设置默认交易类型为永续合约
                'types': ['swap'],      # 仅加载/使用 swap 市场，避免解析其他类型导致的空base/quote
            }
        })
        
        # OKX统一参数（强制使用SWAP场景）
        self.okx_params = {'instType': 'SWAP'}

        # 将统一交易对转为OKX instId，例如 FIL/USDT:USDT -> FIL-USDT-SWAP
        def _symbol_to_inst_id(sym: str) -> str:
            try:
                base = sym.split('/')[0]
                return f"{base}-USDT-SWAP"
            except Exception:
                return ''
        self.symbol_to_inst_id = _symbol_to_inst_id
        
        # 交易对配置 - 小币种
        self.symbols = [
            'ETH/USDT:USDT',
            'BTC/USDT:USDT',
            'SOL/USDT:USDT',
            'DOGE/USDT:USDT'
        ]
        
        # 时间周期 - 15分钟
        self.timeframe = '15m'
        
        # MACD参数
        self.fast_period = 10
        self.slow_period = 40
        self.signal_period = 15

        # 风险参数（按币种）
        self.risk_params: Dict[str, Dict[str, float]] = {
            'BTC/USDT:USDT': {'atr_period': 20, 'sl_mult': 2.2, 'tp_mult': 3.0, 'trigger_pct': 0.010, 'trail_pct': 0.006},
            'ETH/USDT:USDT': {'atr_period': 20, 'sl_mult': 2.0, 'tp_mult': 3.0, 'trigger_pct': 0.010, 'trail_pct': 0.006},
            'SOL/USDT:USDT': {'atr_period': 14, 'sl_mult': 1.8, 'tp_mult': 2.8, 'trigger_pct': 0.008, 'trail_pct': 0.005},
            'DOGE/USDT:USDT': {'atr_period': 14, 'sl_mult': 1.6, 'tp_mult': 2.2, 'trigger_pct': 0.007, 'trail_pct': 0.004},
        }

        # MACD反手规则（close时是否反手）
        self.reverse_on_close: Dict[str, bool] = {
            'BTC/USDT:USDT': False,
            'ETH/USDT:USDT': False,
            'SOL/USDT:USDT': True,
            'DOGE/USDT:USDT': False,
        }

        # ADX过滤阈值（按币种）；仅当 ADX ≥ 阈值 才允许进场
        self.adx_thresholds: Dict[str, int] = {
            'BTC/USDT:USDT': 25,
            'ETH/USDT:USDT': 25,
            'SOL/USDT:USDT': 23,
            'DOGE/USDT:USDT': 28,
        }
        # 统一ADX周期
        self.adx_period: int = 14

        # 动态止盈状态
        # 结构：{ symbol: {'active': bool, 'peak': float, 'trough': float} }
        self.trailing_state: Dict[str, Dict[str, Any]] = {}
        
        # 杠杆配置 - 分币种设置（按需逐币设置）
        self.symbol_leverage: Dict[str, int] = {
            'BTC/USDT:USDT': 25,
            'ETH/USDT:USDT': 30,
            'SOL/USDT:USDT': 30,
            'DOGE/USDT:USDT': 15,
        }
        
        # 仓位配置 - 使用100%资金
        self.position_percentage = 1.0
        
        # 持仓和挂单缓存
        self.positions_cache: Dict[str, Dict] = {}
        self.open_orders_cache: Dict[str, List] = {}
        self.last_sync_time: float = 0
        self.sync_interval: int = 60  # 60秒同步一次状态
        
        # 市场信息缓存
        self.markets_info: Dict[str, Dict] = {}
        
        # 交易统计
        self.stats = TradingStats()
        
        # 记录上次持仓状态，用于判断是否已平仓
        self.last_position_state: Dict[str, str] = {}  # symbol -> 'long'/'short'/'none'
        
        # 初始化交易所
        self._setup_exchange()
        
        # 加载市场信息
        self._load_markets()
        
        # 首次同步状态
        self.sync_all_status()
        
        # 处理启动前已有的持仓和挂单
        self.handle_existing_positions_and_orders()
    
    def _setup_exchange(self):
        """设置交易所配置"""
        try:
            # 检查连接
            self.exchange.check_required_credentials()
            # 强制设定 OKX API 版本，避免 ccxt 内部 URL 拼接出现 None + str
            try:
                self.exchange.version = 'v5'
            except Exception:
                pass
            # 统一默认类型与结算币种，减少内部推断
            try:
                opts = self.exchange.options or {}
                opts.update({'defaultType': 'swap', 'defaultSettle': 'USDT', 'version': 'v5'})
                self.exchange.options = opts
            except Exception:
                pass
            logger.info("✅ API连接验证成功")
            
            # 同步交易所时间
            self.sync_exchange_time()
            
            # 预加载市场数据（容错）：仅加载swap，失败则记录并继续，后续使用安全回退
            try:
                self.exchange.load_markets({'type': 'swap'})
                logger.info("✅ 预加载市场数据完成 (swap)")
            except Exception as e:
                logger.warning(f"⚠️ 预加载市场数据失败，将使用安全回退: {e}")
            
            # 按交易对设置杠杆（使用OKX原生接口，避免统一封装问题）
            for symbol in self.symbols:
                try:
                    lev = self.symbol_leverage.get(symbol, 20)
                    inst_id = self.symbol_to_inst_id(symbol)

                    # 分别设置多空两边的杠杆
                    try:
                        self.exchange.privatePostAccountSetLeverage({'instId': inst_id, 'lever': str(lev), 'mgnMode': 'cross', 'posSide': 'long'})
                    except Exception:
                        pass
                    try:
                        self.exchange.privatePostAccountSetLeverage({'instId': inst_id, 'lever': str(lev), 'mgnMode': 'cross', 'posSide': 'short'})
                    except Exception:
                        pass
                    logger.info(f"✅ 设置{symbol}杠杆为{lev}倍")
                except Exception as e:
                    logger.warning(f"⚠️ 设置{symbol}杠杆失败（可能已设置）: {e}")
            
            # 尝试设置合约模式（如果有持仓会失败，但不影响运行）
            try:
                self.exchange.set_position_mode(True)  # 双向持仓（多空分开）
                logger.info("✅ 设置为双向持仓模式（多空分开）")
            except Exception as e:
                logger.warning(f"⚠️ 设置持仓模式失败（当前可能有持仓，跳过设置）")
                logger.info("ℹ️ 程序将继续运行，使用当前持仓模式")
            
        except Exception as e:
            logger.error(f"❌ 交易所设置失败: {e}")
            raise
    
    def _load_markets(self):
        """加载市场信息（获取最小下单量等限制）"""
        try:
            logger.info("🔄 加载市场信息...")
            # 使用 OKX v5 原生接口获取合约规格，避免统一封装
            resp = self.exchange.publicGetPublicInstruments({'instType': 'SWAP'})
            data = resp.get('data') if isinstance(resp, dict) else resp
            # 建立 instId -> 规格 映射
            spec_map = {}
            for it in (data or []):
                if it.get('settleCcy') == 'USDT':  # 仅 USDT 结算
                    spec_map[it.get('instId')] = it
            for symbol in self.symbols:
                inst_id = self.symbol_to_inst_id(symbol)
                it = spec_map.get(inst_id, {})
                # 解析规格
                min_sz = float(it.get('minSz') or 0) or 0.000001
                lot_sz = float(it.get('lotSz') or 0) or None
                tick_sz = float(it.get('tickSz') or 0) or 0.0001
                amt_prec = len(str(lot_sz).split('.')[-1]) if lot_sz and '.' in str(lot_sz) else 8
                px_prec = len(str(tick_sz).split('.')[-1]) if '.' in str(tick_sz) else 4
                self.markets_info[symbol] = {
                    'min_amount': min_sz,
                    'min_cost': 0.0,
                    'amount_precision': amt_prec,
                    'price_precision': px_prec,
                    'lot_size': lot_sz,
                }
                logger.info(f"📊 {symbol} - 最小数量:{min_sz:.8f} 步进:{(lot_sz or 0):.8f} Tick:{tick_sz:.8f}")
            logger.info("✅ 市场信息加载完成")
        except Exception as e:
            logger.error(f"❌ 加载市场信息失败: {e}")
            # 小币种设置更宽松的默认值
            for symbol in self.symbols:
                self.markets_info[symbol] = {
                    'min_amount': 0.000001,
                    'min_cost': 0.1,
                    'amount_precision': 8,
                    'price_precision': 4,
                    'lot_size': None,
                }
    
    def sync_exchange_time(self):
        """同步交易所时间 - 使用中国时区"""
        try:
            server_time = self.exchange.fetch_time()
            local_time = int(time.time() * 1000)
            time_diff = server_time - local_time
            
            # 转换为中国时区
            china_tz = pytz.timezone('Asia/Shanghai')
            server_dt = datetime.datetime.fromtimestamp(server_time / 1000, tz=china_tz)
            local_dt = datetime.datetime.fromtimestamp(local_time / 1000, tz=china_tz)
            
            logger.info(f"🕐 交易所时间: {server_dt.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
            logger.info(f"🕐 本地时间: {local_dt.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
            logger.info(f"⏱️ 时间差: {time_diff}ms")
            
            if abs(time_diff) > 5000:
                logger.warning(f"⚠️ 时间差较大: {time_diff}ms，可能影响交易")
            
            return time_diff
            
        except Exception as e:
            logger.error(f"❌ 同步时间失败: {e}")
            return 0
    
    def get_open_orders(self, symbol: str) -> List[Dict]:
        """获取未成交订单（OKX原生接口，避免markets依赖）"""
        try:
            inst_id = self.symbol_to_inst_id(symbol)
            resp = self.exchange.privateGetTradeOrdersPending({'instType': 'SWAP', 'instId': inst_id})
            data = resp.get('data') if isinstance(resp, dict) else resp
            results = []
            for o in (data or []):
                results.append({
                    'id': o.get('ordId') or o.get('clOrdId'),
                    'side': 'buy' if o.get('side') == 'buy' else 'sell',
                    'amount': float(o.get('sz') or 0),
                    'price': float(o.get('px') or 0) if o.get('px') else None,
                })
            return results
        except Exception as e:
            logger.error(f"❌ 获取{symbol}挂单失败: {e}")
            return []
    
    def get_open_algo_orders(self, symbol: str) -> List[Dict]:
        """获取未触发的条件单（TP/SL）"""
        try:
            inst_id = self.symbol_to_inst_id(symbol)
            resp = self.exchange.privateGetTradeOrdersAlgoPending({'instType': 'SWAP', 'instId': inst_id})
            data = resp.get('data') if isinstance(resp, dict) else resp
            results = []
            for o in (data or []):
                results.append({
                    'id': o.get('algoId') or o.get('ordId') or o.get('clOrdId'),
                    'posSide': o.get('posSide'),
                    'tpTriggerPx': float(o.get('tpTriggerPx')) if o.get('tpTriggerPx') else None,
                    'slTriggerPx': float(o.get('slTriggerPx')) if o.get('slTriggerPx') else None,
                    'sz': float(o.get('sz') or 0),
                })
            return results
        except Exception as e:
            logger.error(f"❌ 获取{symbol}条件单失败: {e}")
            return []

    def place_tp_sl_orders(self, symbol: str, position: Dict, place_tp: bool = True, place_sl: bool = True) -> bool:
        """为当前持仓挂条件单TP/SL（市价触发，reduceOnly）"""
        try:
            if position.get('size', 0) <= 0:
                return False
            params = self.risk_params.get(symbol)
            if not params:
                return False

            atr = self.calculate_atr(symbol, period=int(params.get('atr_period', 14)))
            entry = float(position.get('entry_price', 0) or 0)
            if atr <= 0 or entry <= 0:
                return False

            side = position.get('side')
            size = float(position.get('size', 0) or 0)
            pos_side = 'long' if side == 'long' else 'short'
            sl_mult = float(params['sl_mult'])
            tp_mult = float(params['tp_mult'])

            if side == 'long':
                sl_px = max(0.0, entry - sl_mult * atr)
                tp_px = entry + tp_mult * atr
            else:
                sl_px = entry + sl_mult * atr
                tp_px = max(0.0, entry - tp_mult * atr)

            inst_id = self.symbol_to_inst_id(symbol)
            order_side = 'sell' if side == 'long' else 'buy'
            # 基础参数
            raw = {
                'instId': inst_id,
                'tdMode': 'cross',
                'posSide': pos_side,
                'side': order_side,
                'ordType': 'conditional',
                'sz': str(size),
            }
            # 只补缺失的一侧
            if place_tp:
                raw.update({
                    'tpTriggerPx': f"{tp_px:.8f}",
                    'tpOrdPx': '-1',
                    'tpTriggerPxType': 'last',
                })
            if place_sl:
                raw.update({
                    'slTriggerPx': f"{sl_px:.8f}",
                    'slOrdPx': '-1',
                    'slTriggerPxType': 'last',
                })
            # 若两侧都不需要补，则无需下单
            if not place_tp and not place_sl:
                return True

            resp = self.exchange.privatePostTradeOrderAlgo(raw)
            ok = isinstance(resp, dict)
            if ok:
                tp_msg = f"TP@{tp_px:.6f}" if place_tp else ""
                sl_msg = f"SL@{sl_px:.6f}" if place_sl else ""
                join = " " if (tp_msg and sl_msg) else ""
                logger.info(f"🛡️ 已为{symbol}挂出条件单 | {tp_msg}{join}{sl_msg} | size={size:.6f}")
            else:
                logger.warning(f"⚠️ 挂出{symbol} 条件单返回异常: {resp}")
            return ok
        except Exception as e:
            logger.error(f"❌ 挂{symbol} TP/SL条件单失败: {e}")
            return False

    def ensure_position_protection(self, symbol: str) -> None:
        """若存在持仓但未配置TP/SL条件单，则自动补挂"""
        try:
            position = self.get_position(symbol, force_refresh=True)
            if position.get('size', 0) <= 0:
                return

            # 归一化持仓方向
            pos_side = (position.get('side') or position.get('posSide') or '').lower()

            # 去抖动：刚挂完单后短时间内不重复挂，避免接口延迟导致重复
            if not hasattr(self, '_algo_guard'):
                self._algo_guard = {}
            key = (symbol, pos_side)
            now = time.time()
            last = self._algo_guard.get(key)
            if last and (now - last) < 30:
                return

            algo_orders = self.get_open_algo_orders(symbol)
            has_tp = False
            has_sl = False

            for o in algo_orders:
                try:
                    o_pos = (o.get('posSide') or '').lower()
                    tp = o.get('tpTriggerPx')
                    sl = o.get('slTriggerPx')

                    def _ok(v):
                        if v in (None, '', '0', 0):
                            return False
                        try:
                            return float(v) > 0
                        except Exception:
                            return True  # 有值但无法解析，也视为已设置

                    # posSide 匹配或未提供时都计入，避免因为posSide不一致漏检
                    if o_pos in ('', pos_side):
                        if _ok(tp):
                            has_tp = True
                        if _ok(sl):
                            has_sl = True
                except Exception:
                    continue

            if has_tp and has_sl:
                logger.info(f"🧷 {symbol} 持仓已存在TP/SL条件单（posSide={pos_side}）")
                return

            placed = self.place_tp_sl_orders(
                symbol,
                position,
                place_tp=(not has_tp),
                place_sl=(not has_sl),
            )
            if placed:
                self._algo_guard[key] = now
        except Exception as e:
            logger.error(f"❌ 确保{symbol}持仓保护失败: {e}")

    def cancel_all_orders(self, symbol: str) -> bool:
        """取消所有未成交订单"""
        try:
            orders = self.get_open_orders(symbol)
            if not orders:
                return True
            
            for order in orders:
                try:
                    self.exchange.cancel_order(order['id'], symbol)
                    logger.info(f"✅ 取消订单: {symbol} {order['id']}")
                except Exception as e:
                    logger.error(f"❌ 取消订单失败: {order['id']} - {e}")
            
            return True
        except Exception as e:
            logger.error(f"❌ 批量取消订单失败: {e}")
            return False
    
    def sync_all_status(self):
        """同步所有状态（持仓和挂单）"""
        try:
            logger.info("🔄 开始同步状态...")
            
            # 同步时间
            self.sync_exchange_time()
            
            # 同步所有交易对的持仓和挂单
            has_positions = False
            has_orders = False
            
            for symbol in self.symbols:
                # 同步持仓
                position = self.get_position(symbol, force_refresh=True)
                self.positions_cache[symbol] = position
                
                # 记录持仓状态
                if position['size'] > 0:
                    self.last_position_state[symbol] = position['side']
                    has_positions = True
                else:
                    self.last_position_state[symbol] = 'none'
                
                # 同步挂单
                orders = self.get_open_orders(symbol)
                self.open_orders_cache[symbol] = orders
                
                # 输出状态
                if position['size'] > 0:
                    logger.info(f"📊 {symbol} 持仓: {position['side']} {position['size']:.6f} @{position['entry_price']:.2f} PNL:{position['unrealized_pnl']:.2f}U 杠杆:{position['leverage']}x")
                
                if orders:
                    has_orders = True
                    logger.info(f"📋 {symbol} 挂单数量: {len(orders)}")
                    for order in orders:
                        logger.info(f"   └─ {order['side']} {order['amount']:.6f} @{order.get('price', 'market')}")
            
            if not has_positions:
                logger.info("ℹ️ 当前无持仓")
            
            if not has_orders:
                logger.info("ℹ️ 当前无挂单")
            
            self.last_sync_time = time.time()
            logger.info("✅ 状态同步完成")
            
        except Exception as e:
            logger.error(f"❌ 同步状态失败: {e}")
    
    def handle_existing_positions_and_orders(self):
        """处理程序启动时已有的持仓和挂单"""
        logger.info("=" * 70)
        logger.info("🔍 检查启动前的持仓和挂单状态...")
        logger.info("=" * 70)
        
        has_positions = False
        has_orders = False
        
        # 检查余额
        balance = self.get_account_balance()
        logger.info(f"💰 当前可用余额: {balance:.4f} USDT")
        logger.info(f"💡 小币种交易：即使只有0.1U也可以下单")
        
        for symbol in self.symbols:
            # 检查持仓
            position = self.get_position(symbol, force_refresh=True)
            if position['size'] > 0:
                has_positions = True
                logger.warning(f"⚠️ 检测到{symbol}已有持仓: {position['side']} {position['size']:.6f} @{position['entry_price']:.4f} PNL:{position['unrealized_pnl']:.2f}U")
                # 记录已有持仓状态
                self.last_position_state[symbol] = position['side']
                # 确保该持仓已挂保护性条件单
                try:
                    self.ensure_position_protection(symbol)
                except Exception as _e:
                    logger.error(f"❌ 为{symbol}补挂TP/SL失败: {_e}")
            
            # 检查挂单
            orders = self.get_open_orders(symbol)
            if orders:
                has_orders = True
                logger.warning(f"⚠️ 检测到{symbol}有{len(orders)}个未成交订单")
                for order in orders:
                    logger.info(f"   └─ {order['side']} {order['amount']:.6f} @{order.get('price', 'market')} ID:{order['id']}")
            # 检查条件单（TP/SL）
            try:
                algo_orders = self.get_open_algo_orders(symbol)
                if algo_orders:
                    has_orders = True
                    logger.warning(f"⚠️ 检测到{symbol}有{len(algo_orders)}个条件单（TP/SL）未触发")
                    for o in algo_orders:
                        tips = []
                        if o.get('tpTriggerPx') is not None:
                            tips.append(f"TP@{o['tpTriggerPx']:.6f}")
                        if o.get('slTriggerPx') is not None:
                            tips.append(f"SL@{o['slTriggerPx']:.6f}")
                        logger.info(f"   └─ posSide={o.get('posSide')} size={o.get('sz', 0):.6f} {' '.join(tips)}")
            except Exception as _e:
                logger.error(f"❌ 读取{symbol}条件单失败: {_e}")
        
        if has_positions or has_orders:
            logger.info("=" * 70)
            logger.info("❓ 程序启动时检测到已有持仓或挂单")
            logger.info("💡 策略说明:")
            logger.info("   1. 已有持仓: 程序会根据MACD信号管理，出现反向信号时平仓")
            logger.info("   2. 已有挂单: 程序会在下次交易前自动取消")
            logger.info("   3. 程序会继续运行并根据信号执行交易")
            logger.info("=" * 70)
            logger.info("⚠️ 如果需要立即平仓所有持仓，请手动操作或重启程序前先手动平仓")
            logger.info("=" * 70)
        else:
            logger.info("✅ 启动前无持仓和挂单，可以正常运行")
            logger.info("=" * 70)
    
    def display_current_positions(self):
        """显示当前所有持仓状态"""
        logger.info("")
        logger.info("=" * 70)
        logger.info("📊 当前持仓状态")
        logger.info("=" * 70)
        
        has_positions = False
        total_pnl = 0.0
        
        for symbol in self.symbols:
            position = self.get_position(symbol, force_refresh=False)
            if position['size'] > 0:
                has_positions = True
                pnl = position['unrealized_pnl']
                total_pnl += pnl
                pnl_emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "➖"
                logger.info(f"{pnl_emoji} {symbol}: {position['side'].upper()} | 数量:{position['size']:.6f} | 入场价:{position['entry_price']:.2f} | 盈亏:{pnl:.2f}U | 杠杆:{position['leverage']}x")
        
        if has_positions:
            total_emoji = "💰" if total_pnl > 0 else "💸" if total_pnl < 0 else "➖"
            logger.info("-" * 70)
            logger.info(f"{total_emoji} 总浮动盈亏: {total_pnl:.2f} USDT")
        else:
            logger.info("ℹ️ 当前无持仓")
        
        logger.info("=" * 70)
        logger.info("")
    
    def check_sync_needed(self):
        """检查是否需要同步状态"""
        current_time = time.time()
        if current_time - self.last_sync_time >= self.sync_interval:
            self.sync_all_status()
    
    def get_account_balance(self) -> float:
        """获取账户余额（OKX原生接口）"""
        try:
            resp = self.exchange.privateGetAccountBalance({})
            data = resp.get('data') if isinstance(resp, dict) else resp
            # data 结构: [{ details: [{ccy:'USDT', cashBal:'...', availBal:'...'}], ... }]
            avail = 0.0
            for acc in (data or []):
                for d in (acc.get('details') or []):
                    if d.get('ccy') == 'USDT':
                        # 优先 availBal，其次 cashBal
                        v = d.get('availBal') or d.get('cashBal') or '0'
                        try:
                            avail = float(v)
                        except Exception:
                            avail = 0.0
                        break
            return avail
        except Exception as e:
            logger.error(f"❌ 获取账户余额失败: {e}")
            return 0.0
    
    def get_klines(self, symbol: str, limit: int = 100) -> List[Dict]:
        """获取K线数据 - 15分钟周期（OKX v5 原生接口）"""
        try:
            inst_id = self.symbol_to_inst_id(symbol)
            # OKX v5: /api/v5/market/candles?instId=...&bar=15m&limit=...
            params = {'instId': inst_id, 'bar': self.timeframe, 'limit': str(limit)}
            resp = self.exchange.publicGetMarketCandles(params)
            rows = resp.get('data') if isinstance(resp, dict) else resp
            result: List[Dict] = []
            for r in (rows or []):
                # OKX返回: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                ts = int(r[0])
                o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4]); v = float(r[5])
                result.append({
                    'timestamp': pd.to_datetime(ts, unit='ms'),
                    'open': o, 'high': h, 'low': l, 'close': c, 'volume': v
                })
            # OKX通常返回从新到旧，按时间升序
            result.sort(key=lambda x: x['timestamp'])
            return result
        except Exception as e:
            logger.error(f"❌ 获取{symbol}K线数据失败: {e}")
            return []
    
    def get_position(self, symbol: str, force_refresh: bool = False) -> Dict:
        """获取当前持仓（带缓存）"""
        try:
            # 如果不强制刷新且缓存存在，返回缓存
            if not force_refresh and symbol in self.positions_cache:
                return self.positions_cache[symbol]
            
            # 从交易所获取最新持仓
            # 使用OKX原生接口获取持仓，避免markets依赖
            inst_id = self.symbol_to_inst_id(symbol)
            resp = self.exchange.privateGetAccountPositions({'instType': 'SWAP', 'instId': inst_id})
            data = resp.get('data') if isinstance(resp, dict) else resp
            for p in (data or []):
                if p.get('instId') == inst_id and float(p.get('pos', 0) or 0) != 0:
                    size = abs(float(p.get('pos', 0) or 0))
                    side = 'long' if p.get('posSide') == 'long' else 'short'
                    entry_price = float(p.get('avgPx', 0) or 0)
                    leverage = float(p.get('lever', 0) or 0)
                    unreal = float(p.get('upl', 0) or 0)
                    pos_data = {
                        'size': size,
                        'side': side,
                        'entry_price': entry_price,
                        'unrealized_pnl': unreal,
                        'leverage': leverage,
                    }
                    self.positions_cache[symbol] = pos_data
                    return pos_data
            
            # 无持仓
            pos_data = {'size': 0, 'side': 'none', 'entry_price': 0, 'unrealized_pnl': 0, 'leverage': 0}
            self.positions_cache[symbol] = pos_data
            return pos_data
            
        except Exception as e:
            logger.error(f"❌ 获取{symbol}持仓失败: {e}")
            # 返回缓存或默认值
            if symbol in self.positions_cache:
                return self.positions_cache[symbol]
            return {'size': 0, 'side': 'none', 'entry_price': 0, 'unrealized_pnl': 0, 'leverage': 0}
    
    def has_open_orders(self, symbol: str) -> bool:
        """检查是否有未成交订单"""
        try:
            orders = self.get_open_orders(symbol)
            has_orders = len(orders) > 0
            if has_orders:
                logger.info(f"⚠️ {symbol} 存在{len(orders)}个未成交订单")
            return has_orders
        except Exception as e:
            logger.error(f"❌ 检查挂单失败: {e}")
            return False
    
    def calculate_order_amount(self, symbol: str, active_count: Optional[int] = None) -> float:
        """计算下单金额（增强版：支持固定目标金额/放大因子/上下限/按信号集中分配）"""
        try:
            # 1) 固定目标名义金额（最高优先）
            target_str = os.environ.get('TARGET_NOTIONAL_USDT', '').strip()
            if target_str:
                try:
                    target = max(0.0, float(target_str))
                    logger.info(f"💵 使用固定目标名义金额: {target:.4f}U")
                    return target
                except Exception:
                    logger.warning(f"⚠️ TARGET_NOTIONAL_USDT 无效: {target_str}")

            # 2) 基于余额分配（默认平均分）
            balance = self.get_account_balance()
            if balance <= 0:
                logger.warning(f"⚠️ 余额不足，无法为 {symbol} 分配资金 (余额:{balance:.4f}U)")
                return 0.0

            alloc_mode = (os.environ.get('ALLOC_MODE', 'all') or 'all').strip().lower()
            num_symbols = max(1, len(self.symbols))
            base_divisor = num_symbols

            # 仅给有信号的币分配（需要调用处统计 active_count 并传入）
            if alloc_mode == 'signals' and active_count and active_count > 0:
                base_divisor = active_count

            allocated_amount = balance / max(1, base_divisor)

            # 3) 放大因子
            factor_str = os.environ.get('ORDER_NOTIONAL_FACTOR', '50').strip()
            try:
                factor = max(1.0, float(factor_str or '1'))
            except Exception:
                factor = 1.0
            allocated_amount *= factor

            # 4) 下限/上限
            def _to_float(env_name: str, default: float) -> float:
                try:
                    s = os.environ.get(env_name, '').strip()
                    return float(s) if s else default
                except Exception:
                    return default

            min_floor = max(0.0, _to_float('MIN_PER_SYMBOL_USDT', 0.0))
            max_cap = max(0.0, _to_float('MAX_PER_SYMBOL_USDT', 0.0))

            if min_floor > 0 and allocated_amount < min_floor:
                allocated_amount = min_floor
            if max_cap > 0 and allocated_amount > max_cap:
                allocated_amount = max_cap

            logger.info(f"💵 资金分配: 模式={alloc_mode}, 总余额={balance:.4f}U, 分母={base_divisor}, 因子={factor:.2f}, 本币目标={allocated_amount:.4f}U")
            if allocated_amount <= 0:
                logger.warning(f"⚠️ {symbol}最终分配金额为0，跳过")
                return 0.0

            return allocated_amount

        except Exception as e:
            logger.error(f"❌ 计算{symbol}下单金额失败: {e}")
            return 0.0
    
    def create_order(self, symbol: str, side: str, amount: float) -> bool:
        """创建订单 - 小币种版本，支持小额交易（OKX原生下单，避免精度与symbol转换问题）"""
        try:
            # 检查是否有挂单
            if self.has_open_orders(symbol):
                logger.warning(f"⚠️ {symbol}存在未成交订单，先取消")
                self.cancel_all_orders(symbol)
                time.sleep(1)  # 等待订单取消

            if amount <= 0:
                logger.warning(f"⚠️ {symbol}下单金额为0，跳过")
                return False

            # 获取市场信息
            market_info = self.markets_info.get(symbol, {})
            min_amount = float(market_info.get('min_amount', 0.001) or 0.001)
            amount_precision = int(market_info.get('amount_precision', 8) or 8)
            lot_sz = market_info.get('lot_size')  # 可能为 None

            # 获取当前价格（使用 OKX v5 原生接口，避免 ccxt 统一接口的 None + 'str' 问题）
            inst_id = self.symbol_to_inst_id(symbol)
            try:
                tkr = self.exchange.publicGetMarketTicker({'instId': inst_id})
                # OKX v5 返回结构 { code, data: [{ last: '...', ... }], msg }
                if isinstance(tkr, dict):
                    d = tkr.get('data') or []
                    if isinstance(d, list) and d:
                        current_price = float(d[0].get('last') or d[0].get('lastPx') or 0.0)
                    else:
                        current_price = 0.0
                else:
                    current_price = 0.0
            except Exception as _e:
                logger.error(f"❌ 获取{symbol}最新价失败({inst_id}): {_e}")
                current_price = 0.0

            if not current_price or current_price <= 0:
                logger.error(f"❌ 无法获取{symbol}有效价格，跳过下单")
                return False

            # 计算合约数量（基于金额/价格）
            contract_size = amount / current_price

            # 先确保不低于最小数量
            if contract_size < min_amount:
                contract_size = min_amount

            # 先按步进向上对齐，再按小数位四舍五入（尽量用满分配金额）
            step = None
            if lot_sz:
                try:
                    step = float(lot_sz)
                    if step and step > 0:
                        contract_size = math.ceil(contract_size / step) * step
                except Exception:
                    step = None
            contract_size = round(contract_size, amount_precision)

            # 防止截断后为0或仍小于最小数量
            if contract_size <= 0 or contract_size < min_amount:
                contract_size = max(min_amount, 10 ** (-amount_precision))
                if step and step > 0:
                    try:
                        contract_size = math.ceil(contract_size / step) * step
                    except Exception:
                        pass
                contract_size = round(contract_size, amount_precision)

            # 若按当前价格计算的成本仍低于分配金额，则按步进/精度向上补量，尽量使 size*price ≥ amount
            try:
                used_usdt = contract_size * current_price
                if used_usdt + 1e-12 < amount:
                    # 计算还需增加的数量
                    need_qty = (amount - used_usdt) / current_price
                    incr_step = step if (step and step > 0) else (10 ** (-amount_precision))
                    # 向上取整到合法步进
                    add_qty = math.ceil(need_qty / incr_step) * incr_step
                    contract_size = round(contract_size + add_qty, amount_precision)
                    # 再次确保不低于最小数量
                    if contract_size < min_amount:
                        contract_size = min_amount
                        if step and step > 0:
                            contract_size = math.ceil(contract_size / step) * step
                        contract_size = round(contract_size, amount_precision)
            except Exception:
                pass

            if contract_size <= 0:
                logger.warning(f"⚠️ {symbol}最终数量无效: {contract_size}")
                return False

            logger.info(f"📝 准备下单: {symbol} {side} 金额:{amount:.4f}U 价格:{current_price:.4f} 数量:{contract_size:.8f}")
            # 成本对齐信息（用于核对是否用满分配金额）
            try:
                est_cost = contract_size * current_price
                logger.info(f"🧮 下单成本对齐: 分配金额={amount:.4f}U | 预计成本={est_cost:.4f}U | 数量={contract_size:.8f} | minSz={min_amount} | lotSz={lot_sz}")
            except Exception:
                pass

            pos_side = 'long' if side == 'buy' else 'short'
            order_id = None
            last_err = None

            # 打印当前 ccxt 版本配置，便于排查
            try:
                ex_ver = getattr(self.exchange, 'version', None)
                opt_ver = (self.exchange.options or {}).get('version') if getattr(self.exchange, 'options', None) else None
                logger.debug(f"🔧 CCXT version: {ex_ver}, options.version: {opt_ver}")
            except Exception:
                pass

            import traceback

            # 可选：仅用原生接口（通过环境变量控制）
            native_only = False
            try:
                native_only = (os.environ.get('USE_OKX_NATIVE_ONLY', '').strip().lower() in ('1', 'true', 'yes'))
            except Exception:
                native_only = False

            # 尝试1：统一接口 create_order（若未启用仅原生）
            if not native_only:
                try:
                    params = {'tdMode': 'cross', 'posSide': pos_side}
                    resp = self.exchange.create_order(symbol, 'market', side, contract_size, None, params)
                    if isinstance(resp, dict):
                        order_id = resp.get('id') or resp.get('orderId') or resp.get('ordId') or resp.get('clOrdId')
                    elif isinstance(resp, list) and resp and isinstance(resp[0], dict):
                        order_id = resp[0].get('id') or resp[0].get('orderId') or resp[0].get('ordId') or resp[0].get('clOrdId')
                    if order_id:
                        logger.info(f"✅ 成功创建{symbol} {side}订单，数量:{contract_size:.8f}，订单ID:{order_id}")
                    else:
                        logger.warning(f"⚠️ create_order 返回未包含订单ID，响应: {resp}")
                except Exception as e1:
                    last_err = e1
                    logger.error(f"❌ create_order 异常: {e1}")
                    logger.debug(traceback.format_exc())

            # 尝试2：create_market_order（若尚未拿到ID且未启用仅原生）
            if not order_id and not native_only:
                try:
                    params = {'tdMode': 'cross', 'posSide': pos_side}
                    resp = self.exchange.create_market_order(symbol, side, contract_size, params)
                    if isinstance(resp, dict):
                        order_id = resp.get('id') or resp.get('orderId') or resp.get('ordId') or resp.get('clOrdId')
                    elif isinstance(resp, list) and resp and isinstance(resp[0], dict):
                        order_id = resp[0].get('id') or resp[0].get('orderId') or resp[0].get('ordId') or resp[0].get('clOrdId')
                    if order_id:
                        logger.info(f"✅ 成功创建{symbol} {side}订单（market API），数量:{contract_size:.8f}，订单ID:{order_id}")
                    else:
                        logger.warning(f"⚠️ create_market_order 返回未包含订单ID，响应: {resp}")
                except Exception as e2:
                    last_err = e2
                    logger.error(f"❌ create_market_order 异常: {e2}")
                    logger.debug(traceback.format_exc())

            # 尝试3：OKX 原生接口（最后兜底）
            if not order_id:
                try:
                    inst_id = self.symbol_to_inst_id(symbol)
                    raw_params = {
                        'instId': inst_id,
                        'tdMode': 'cross',
                        'side': side,
                        'posSide': pos_side,
                        'ordType': 'market',
                        'sz': str(contract_size)
                    }
                    resp = self.exchange.privatePostTradeOrder(raw_params)
                    # 兼容 OKX v5 返回结构
                    if isinstance(resp, dict):
                        data = resp.get('data') or []
                        if isinstance(data, list) and data:
                            order_id = data[0].get('ordId') or data[0].get('clOrdId') or data[0].get('id')
                        else:
                            order_id = resp.get('ordId') or resp.get('clOrdId') or resp.get('id')
                    if order_id:
                        logger.info(f"✅ 成功创建{symbol} {side}订单（OKX原生兜底），数量:{contract_size:.8f}，订单ID:{order_id}")
                    else:
                        logger.error(f"❌ OKX原生下单无订单ID，响应: {resp}")
                except Exception as e3:
                    last_err = e3
                    logger.error(f"❌ OKX原生下单异常: {e3}")
                    logger.debug(traceback.format_exc())

            if order_id:
                time.sleep(2)
                # 刷新持仓并挂出保护性条件单（TP/SL）
                self.get_position(symbol, force_refresh=True)
                try:
                    self.ensure_position_protection(symbol)
                except Exception as _e:
                    logger.error(f"❌ 挂出{symbol}保护性条件单失败: {_e}")
                return True

            # 若三次都失败，抛出最后错误提示
            if last_err:
                logger.error(f"❌ 创建{symbol} {side}订单失败：{last_err}")
            return False

        except Exception as e:
            logger.error(f"❌ 创建{symbol} {side}订单异常: {e}")
            import traceback as _tb
            logger.debug(_tb.format_exc())
            return False
    
    def close_position(self, symbol: str, open_reverse: bool = False) -> bool:
        """平仓；如 open_reverse=True，平仓后立即反向开仓"""
        try:
            # 先取消所有挂单
            if self.has_open_orders(symbol):
                logger.info(f"🔄 平仓前先取消{symbol}的挂单")
                self.cancel_all_orders(symbol)
                time.sleep(1)
            
            # 刷新持仓
            position = self.get_position(symbol, force_refresh=True)
            
            if position['size'] == 0:
                logger.info(f"ℹ️ {symbol}无持仓，无需平仓")
                return True
            
            # 记录平仓前的盈亏
            pnl = position.get('unrealized_pnl', 0)
            position_side = position.get('side', 'unknown')
            
            # 获取合约数量
            size = float(position.get('size', 0) or 0)
            
            # 反向平仓：多头平仓用sell，空头平仓用buy
            side = 'sell' if position.get('side') == 'long' else 'buy'
            
            logger.info(f"📝 准备平仓: {symbol} {side} 数量:{size:.6f} 预计盈亏:{pnl:.2f}U")

            import traceback as _tb
            order_id = None
            last_err = None

            # 尝试1：ccxt 统一接口 create_order + reduceOnly
            try:
                params = {'reduceOnly': True, 'posSide': position_side, 'tdMode': 'cross'}
                resp = self.exchange.create_order(symbol, 'market', side, size, None, params)
                if isinstance(resp, dict):
                    order_id = resp.get('id') or resp.get('orderId') or resp.get('ordId') or resp.get('clOrdId')
                elif isinstance(resp, list) and resp and isinstance(resp[0], dict):
                    order_id = resp[0].get('id') or resp[0].get('orderId') or resp[0].get('ordId') or resp[0].get('clOrdId')
            except Exception as e1:
                last_err = e1
                logger.error(f"❌ 平仓 create_order 异常: {e1}")
                logger.debug(_tb.format_exc())

            # 尝试2：ccxt create_market_order + reduceOnly
            if not order_id:
                try:
                    params = {'reduceOnly': True, 'posSide': position_side, 'tdMode': 'cross'}
                    resp = self.exchange.create_market_order(symbol, side, size, params)
                    if isinstance(resp, dict):
                        order_id = resp.get('id') or resp.get('orderId') or resp.get('ordId') or resp.get('clOrdId')
                    elif isinstance(resp, list) and resp and isinstance(resp[0], dict):
                        order_id = resp[0].get('id') or resp[0].get('orderId') or resp[0].get('ordId') or resp[0].get('clOrdId')
                except Exception as e2:
                    last_err = e2
                    logger.error(f"❌ 平仓 create_market_order 异常: {e2}")
                    logger.debug(_tb.format_exc())

            # 尝试3：OKX 原生接口兜底
            if not order_id:
                try:
                    inst_id = self.symbol_to_inst_id(symbol)
                    raw_params = {
                        'instId': inst_id,
                        'tdMode': 'cross',
                        'side': side,
                        'posSide': position_side,
                        'reduceOnly': True,
                        'ordType': 'market',
                        'sz': str(size)
                    }
                    resp = self.exchange.privatePostTradeOrder(raw_params)
                    if isinstance(resp, dict):
                        data = resp.get('data') or []
                        if isinstance(data, list) and data:
                            order_id = data[0].get('ordId') or data[0].get('clOrdId') or data[0].get('id')
                        else:
                            order_id = resp.get('ordId') or resp.get('clOrdId') or resp.get('id')
                except Exception as e3:
                    last_err = e3
                    logger.error(f"❌ 平仓 OKX 原生接口异常: {e3}")
                    logger.debug(_tb.format_exc())

            if order_id:
                logger.info(f"✅ 成功平仓{symbol}，方向: {side}，数量: {size:.6f}，盈亏: {pnl:.2f}U")
                # 记录交易统计
                self.stats.add_trade(symbol, position_side, pnl)
                time.sleep(2)
                self.get_position(symbol, force_refresh=True)
                self.last_position_state[symbol] = 'none'

                if open_reverse:
                    reverse_side = 'sell' if position_side == 'long' else 'buy'
                    amount = self.calculate_order_amount(symbol)
                    if amount > 0:
                        if self.create_order(symbol, reverse_side, amount):
                            logger.info(f"🔁 平仓后已反向开仓 {symbol} -> {reverse_side}")
                return True

            logger.error(f"❌ 平仓{symbol}失败")
            if last_err:
                logger.error(f"❌ 平仓最后错误：{last_err}")
            return False
                
        except Exception as e:
            logger.error(f"❌ 平仓{symbol}失败: {e}")
            return False
    
    def calculate_macd(self, prices: List[float]) -> Dict[str, float]:
        """计算MACD指标"""
        # 转换为numpy数组
        close_array = np.array(prices)
        
        # 计算EMA
        ema_fast = pd.Series(close_array).ewm(span=self.fast_period, adjust=False).mean().values
        ema_slow = pd.Series(close_array).ewm(span=self.slow_period, adjust=False).mean().values
        
        # 计算MACD线
        macd_line = ema_fast - ema_slow
        
        # 计算信号线
        signal_line = pd.Series(macd_line).ewm(span=self.signal_period, adjust=False).mean().values
        
        # 计算柱状图
        histogram = macd_line - signal_line
        
        # 返回最新的MACD值
        return {
            'macd': macd_line[-1],
            'signal': signal_line[-1],
            'histogram': histogram[-1],
            'macd_line': macd_line,
            'signal_line': signal_line
        }

    def get_current_price(self, symbol: str) -> float:
        """获取当前最新成交价（OKX v5 原生接口）"""
        try:
            inst_id = self.symbol_to_inst_id(symbol)
            tkr = self.exchange.publicGetMarketTicker({'instId': inst_id})
            if isinstance(tkr, dict):
                d = (tkr.get('data') or [])
                if isinstance(d, list) and d:
                    return float(d[0].get('last') or d[0].get('lastPx') or 0.0)
            return 0.0
        except Exception as e:
            logger.error(f"❌ 获取{symbol}最新价失败: {e}")
            return 0.0

    def calculate_atr(self, symbol: str, period: int = 14, limit: int = 200) -> float:
        """计算ATR（True Range的EMA版本），默认拉取200根15m K线"""
        try:
            klines = self.get_klines(symbol, limit)
            if len(klines) < period + 1:
                return 0.0
            highs = np.array([k['high'] for k in klines])
            lows = np.array([k['low'] for k in klines])
            closes = np.array([k['close'] for k in klines])
            prev_closes = np.concatenate(([closes[0]], closes[:-1]))

            tr = np.maximum.reduce([
                highs - lows,
                np.abs(highs - prev_closes),
                np.abs(lows - prev_closes)
            ])
            atr_series = pd.Series(tr).ewm(span=period, adjust=False).mean().values
            return float(atr_series[-1])
        except Exception as e:
            logger.error(f"❌ 计算{symbol} ATR失败: {e}")
            return 0.0

    def calculate_adx(self, symbol: str, period: int = 14, limit: int = 200) -> float:
        """计算ADX（Wilder's ADX，使用RMA实现），返回最新值"""
        try:
            klines = self.get_klines(symbol, limit)
            if len(klines) < period + 2:
                return 0.0

            highs = np.array([k['high'] for k in klines], dtype=float)
            lows = np.array([k['low'] for k in klines], dtype=float)
            closes = np.array([k['close'] for k in klines], dtype=float)

            # 方向动量
            up_move = highs[1:] - highs[:-1]
            down_move = lows[:-1] - lows[1:]

            plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

            # True Range
            prev_close = closes[:-1]
            tr_components = np.vstack([
                highs[1:] - lows[1:],
                np.abs(highs[1:] - prev_close),
                np.abs(lows[1:] - prev_close)
            ])
            tr = np.max(tr_components, axis=0)

            # Wilder's RMA (alpha = 1/period)
            alpha = 1.0 / period
            tr_rma = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean().values
            plus_dm_rma = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False).mean().values
            minus_dm_rma = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False).mean().values

            # DI+
            plus_di = np.where(tr_rma > 0, 100.0 * (plus_dm_rma / tr_rma), 0.0)
            minus_di = np.where(tr_rma > 0, 100.0 * (minus_dm_rma / tr_rma), 0.0)

            # DX 与 ADX
            dx = np.where(
                (plus_di + minus_di) > 0,
                100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di),
                0.0
            )
            adx = pd.Series(dx).ewm(alpha=alpha, adjust=False).mean().values

            # 最新ADX对应到序列末尾（注意长度：去掉了第一根）
            latest_adx = float(adx[-1]) if len(adx) > 0 else 0.0
            return latest_adx
        except Exception as e:
            logger.error(f"❌ 计算{symbol} ADX失败: {e}")
            return 0.0

    def manage_risk(self, symbol: str, position: Dict) -> bool:
        """风险管理：ATR止损/止盈 + 动态触发/回撤追踪。返回True表示已平仓。"""
        try:
            if position.get('size', 0) <= 0:
                return False

            params = self.risk_params.get(symbol, None)
            if not params:
                return False

            atr = self.calculate_atr(symbol, period=int(params.get('atr_period', 14)))
            price = self.get_current_price(symbol)
            if atr <= 0 or price <= 0:
                return False

            entry = float(position.get('entry_price', 0) or 0)
            if entry <= 0:
                return False

            side = position.get('side')
            sl_mult = float(params['sl_mult'])
            tp_mult = float(params['tp_mult'])
            trigger_pct = float(params['trigger_pct'])
            trail_pct = float(params['trail_pct'])

            # 初始化跟踪状态
            ts = self.trailing_state.get(symbol)
            if not ts:
                ts = {'active': False, 'peak': None, 'trough': None}
                self.trailing_state[symbol] = ts

            closed = False
            reason = ""

            if side == 'long':
                stop_loss = entry - sl_mult * atr
                take_profit = entry + tp_mult * atr

                # 触发/更新峰值
                ts['peak'] = price if ts['peak'] is None else max(ts['peak'], price)

                # 固定止损/止盈
                if price <= stop_loss:
                    reason = f"📉 触发止损（ATR×{sl_mult:.2f}）"
                    closed = True
                elif price >= take_profit:
                    reason = f"🎯 触发止盈（ATR×{tp_mult:.2f}）"
                    closed = True
                else:
                    # 动态触发与回撤
                    profit_pct = (price - entry) / entry
                    if profit_pct >= trigger_pct:
                        ts['active'] = True
                    if ts['active'] and ts['peak']:
                        drawdown_pct = (ts['peak'] - price) / ts['peak']
                        if drawdown_pct >= trail_pct:
                            reason = f"🔧 动态止盈回撤 {trail_pct*100:.1f}% 触发"
                            closed = True

            else:  # short
                stop_loss = entry + sl_mult * atr
                take_profit = entry - tp_mult * atr

                # 触发/更新低点
                ts['trough'] = price if ts['trough'] is None else min(ts['trough'], price)

                if price >= stop_loss:
                    reason = f"📉 空头止损（ATR×{sl_mult:.2f}）"
                    closed = True
                elif price <= take_profit:
                    reason = f"🎯 空头止盈（ATR×{tp_mult:.2f}）"
                    closed = True
                else:
                    profit_pct = (entry - price) / entry
                    if profit_pct >= trigger_pct:
                        ts['active'] = True
                    if ts['active'] and ts['trough'] is not None:
                        rebound_pct = (price - ts['trough']) / ts['trough']
                        if rebound_pct >= trail_pct:
                            reason = f"🔧 动态止盈回撤 {trail_pct*100:.1f}% 触发"
                            closed = True

            if closed:
                logger.info(f"🛡️ 风险管理触发 {symbol} -> {reason} | 当前价:{price:.4f} | 入场:{entry:.4f} | ATR:{atr:.6f}")
                # 风险管理只平仓，不反手
                if self.close_position(symbol, open_reverse=False):
                    # 重置该symbol的跟踪状态
                    self.trailing_state[symbol] = {'active': False, 'peak': None, 'trough': None}
                    return True

            return False

        except Exception as e:
            logger.error(f"❌ {symbol} 风险管理异常: {e}")
            return False
    
    def analyze_symbol(self, symbol: str) -> Dict[str, str]:
        """分析单个交易对"""
        try:
            # 获取K线数据
            klines = self.get_klines(symbol, 100)
            if not klines:
                return {'signal': 'hold', 'reason': '数据获取失败'}
            
            # 提取收盘价（包含最新正在形成的K线）
            closes = [kline['close'] for kline in klines]

            if len(closes) < 2:
                return {'signal': 'hold', 'reason': '数据不足'}

            # 使用实时K线：当前与前一根（不等待收盘）
            macd_current = self.calculate_macd(closes)
            macd_prev = self.calculate_macd(closes[:-1])
            
            # 获取持仓（强制刷新，确保信号判断基于最新持仓）
            position = self.get_position(symbol, force_refresh=True)
            
            # 使用实时K线进行交叉与柱状图颜色变化判断
            prev_macd = macd_prev['macd']
            prev_signal = macd_prev['signal']
            prev_hist = macd_prev['histogram']
            current_macd = macd_current['macd']
            current_signal = macd_current['signal']
            current_hist = macd_current['histogram']

            # 计算ADX并应用过滤（仅影响进场）
            adx_val = self.calculate_adx(symbol, period=self.adx_period)
            adx_th = int(self.adx_thresholds.get(symbol, 25))
            logger.debug(f"📊 {symbol} MACD(实时) - 当前: MACD={current_macd:.6f}, Signal={current_signal:.6f}, Hist={current_hist:.6f} | ADX={adx_val:.2f} 阈值={adx_th}")

            # 生成交易信号
            if position['size'] == 0:  # 无持仓：先检查ADX过滤
                if adx_val < adx_th:
                    return {'signal': 'hold', 'reason': f'ADX({adx_val:.2f})低于阈值{adx_th}，过滤震荡不进场'}
                # 金叉信号：快线上穿慢线 或 柱状图由绿转红（负到正）
                if (prev_macd <= prev_signal and current_macd > current_signal) or (prev_hist <= 0 and current_hist > 0):
                    return {'signal': 'buy', 'reason': f'MACD金叉且ADX({adx_val:.2f})≥{adx_th}'}
                # 死叉信号：快线下穿慢线 或 柱状图由红转绿（正到负）
                elif (prev_macd >= prev_signal and current_macd < current_signal) or (prev_hist >= 0 and current_hist < 0):
                    return {'signal': 'sell', 'reason': f'MACD死叉且ADX({adx_val:.2f})≥{adx_th}'}
                else:
                    return {'signal': 'hold', 'reason': '等待交叉信号'}

            else:  # 有持仓：平仓逻辑仍以MACD为主（风险管理已在执行流程先行处理）
                current_position_side = position['side']
                # 检查持仓方向是否与上次记录一致，如果一致说明没有平仓过
                last_side = self.last_position_state.get(symbol, 'none')

                if current_position_side == 'long':
                    # 多头平仓：快线下穿慢线 或 柱状图转负
                    if (prev_macd >= prev_signal and current_macd < current_signal) or (current_hist < 0):
                        return {'signal': 'close', 'reason': '多头平仓（死叉）'}
                    else:
                        return {'signal': 'hold', 'reason': '持有多头'}

                else:  # short
                    # 空头平仓：快线上穿慢线 或 柱状图转正
                    if (prev_macd <= prev_signal and current_macd > current_signal) or (current_hist > 0):
                        return {'signal': 'close', 'reason': '空头平仓（金叉）'}
                    else:
                        return {'signal': 'hold', 'reason': '持有空头'}
                        
        except Exception as e:
            logger.error(f"❌ 分析{symbol}失败: {e}")
            return {'signal': 'hold', 'reason': f'分析异常: {e}'}
    
    def execute_strategy(self):
        """执行策略"""
        logger.info("=" * 70)
        logger.info("🚀 开始执行MACD策略 (分币种杠杆，15分钟周期)")
        logger.info("=" * 70)
        
        try:
            # 检查是否需要同步状态
            self.check_sync_needed()
            
            # 显示当前余额
            balance = self.get_account_balance()
            logger.info(f"💰 当前账户余额: {balance:.2f} USDT")
            
            # 显示交易统计
            logger.info(self.stats.get_summary())
            
            # 显示当前持仓状态
            self.display_current_positions()
            
            # 在每轮开始时为已有持仓补挂/补全 TP/SL 条件单（启动前或外部形成的持仓也覆盖）
            for symbol in self.symbols:
                try:
                    self.ensure_position_protection(symbol)
                except Exception as _e:
                    logger.error(f"❌ 为{symbol}补挂TP/SL失败: {_e}")
            
            logger.info("🔍 分析交易信号...")
            logger.info("-" * 70)
            
            # 分析所有交易对
            signals = {}
            for symbol in self.symbols:
                signals[symbol] = self.analyze_symbol(symbol)
                position = self.get_position(symbol, force_refresh=False)
                open_orders = self.get_open_orders(symbol)
                
                status_line = f"📊 {symbol}: 信号={signals[symbol]['signal']}, 原因={signals[symbol]['reason']}"
                if open_orders:
                    status_line += f", 挂单={len(open_orders)}个"
                
                logger.info(status_line)
            
            logger.info("-" * 70)
            logger.info("⚡ 执行交易操作...")
            logger.info("")
            
            # 执行交易
            for symbol, signal_info in signals.items():
                signal = signal_info['signal']
                reason = signal_info['reason']
                
                # 获取当前持仓（强制刷新，确保动作基于最新状态）
                current_position = self.get_position(symbol, force_refresh=True)

                # 先进行风险管理（ATR止损/止盈 + 动态追踪）
                try:
                    if self.manage_risk(symbol, current_position):
                        logger.info(f"✅ {symbol} 已按风险规则平仓，跳过当轮信号处理")
                        continue
                except Exception as _e:
                    logger.error(f"❌ {symbol} 风险管理处理失败: {_e}")
                
                if signal == 'buy':
                    # 检查是否已经是多头持仓，如果是则不重复开仓
                    if current_position['size'] > 0 and current_position['side'] == 'long':
                        logger.info(f"ℹ️ {symbol}已有多头持仓，跳过重复开仓")
                        continue
                    
                    # 做多：金叉信号
                    amount = self.calculate_order_amount(symbol)
                    if amount > 0:
                        if self.create_order(symbol, 'buy', amount):
                            logger.info(f"🚀 开多{symbol}成功 - {reason}")
                            self.last_position_state[symbol] = 'long'
                
                elif signal == 'sell':
                    # 检查是否已经是空头持仓，如果是则不重复开仓
                    if current_position['size'] > 0 and current_position['side'] == 'short':
                        logger.info(f"ℹ️ {symbol}已有空头持仓，跳过重复开仓")
                        continue
                    
                    # 做空：死叉信号
                    amount = self.calculate_order_amount(symbol)
                    if amount > 0:
                        if self.create_order(symbol, 'sell', amount):
                            logger.info(f"📉 开空{symbol}成功 - {reason}")
                            self.last_position_state[symbol] = 'short'
                
                elif signal == 'close':
                    # 按币种规则决定是否反手
                    open_reverse = self.reverse_on_close.get(symbol, False)
                    if self.close_position(symbol, open_reverse=open_reverse):
                        act = "平仓并反手开仓" if open_reverse else "平仓"
                        logger.info(f"✅ {act} {symbol} 成功 - {reason}")
            
            logger.info("=" * 70)
                        
        except Exception as e:
            logger.error(f"❌ 执行策略失败: {e}")
    
    def run_continuous(self, interval: int = 60):
        """连续运行策略（改为北京时间整点刷新）"""
        logger.info("=" * 70)
        logger.info("🚀 MACD策略启动 - RAILWAY平台版 (小币种)")
        logger.info("=" * 70)
        logger.info(f"📈 MACD参数: 快线={self.fast_period}, 慢线={self.slow_period}, 信号线={self.signal_period}")
        logger.info(f"📊 K线周期: {self.timeframe} (15分钟)")
        lev_desc = ', '.join([f"{s.split('/')[0]}={self.symbol_leverage.get(s, 20)}x" for s in self.symbols])
        logger.info(f"💪 杠杆倍数: {lev_desc}")
        logger.info("⏰ 刷新方式: 实时巡检（每interval秒执行一次，可用环境变量 SCAN_INTERVAL 调整，默认1秒）")
        logger.info(f"🔄 状态同步: 每{self.sync_interval}秒")
        logger.info(f"📊 监控币种: {', '.join(self.symbols)}")
        logger.info(f"💡 小币种特性: 支持0.1U起的小额交易")
        logger.info(self.stats.get_summary())
        logger.info("=" * 70)

        china_tz = pytz.timezone('Asia/Shanghai')

        while True:
            try:
                # 实时巡检模式：每 interval 秒执行一次
                start_ts = time.time()

                # 按需同步状态（内部有节流）
                self.check_sync_needed()

                # 执行策略（含拉取行情、分析与下单）
                self.execute_strategy()

                # 计算本轮耗时与休眠
                elapsed = time.time() - start_ts
                sleep_sec = max(1, int(interval - elapsed)) if interval > 0 else 1
                logger.info(f"⏳ 休眠 {sleep_sec} 秒后继续实时巡检...")
                time.sleep(sleep_sec)

            except KeyboardInterrupt:
                logger.info("⛔ 用户中断，策略停止")
                break
            except Exception as e:
                logger.error(f"❌ 策略运行异常: {e}")
                logger.info("🔄 60秒后重试...")
                time.sleep(60)

def main():
    """主函数"""
    logger.info("=" * 70)
    logger.info("🎯 MACD策略程序启动中...")
    logger.info("=" * 70)
    
    # 从环境变量获取API配置
    okx_api_key = os.environ.get('OKX_API_KEY', '')
    okx_secret_key = os.environ.get('OKX_SECRET_KEY', '')
    okx_passphrase = os.environ.get('OKX_PASSPHRASE', '')
    
    # 检查环境变量是否设置
    missing_vars = []
    if not okx_api_key:
        missing_vars.append('OKX_API_KEY')
    if not okx_secret_key:
        missing_vars.append('OKX_SECRET_KEY')
    if not okx_passphrase:
        missing_vars.append('OKX_PASSPHRASE')
    
    if missing_vars:
        logger.error(f"❌ 缺少环境变量: {', '.join(missing_vars)}")
        logger.error("💡 请在RAILWALL平台上设置这些环境变量")
        return
    
    logger.info("✅ 环境变量检查通过")
    
    # 创建策略实例
    try:
        strategy = MACDStrategy(
            api_key=okx_api_key,
            secret_key=okx_secret_key,
            passphrase=okx_passphrase
        )
        
        logger.info("✅ 策略初始化成功")
        
        # 运行策略（扫描间隔可通过环境变量 SCAN_INTERVAL 覆盖，单位秒，默认1s）
        try:
            scan_interval_env = os.environ.get('SCAN_INTERVAL', '').strip()
            scan_interval = int(scan_interval_env) if scan_interval_env else 1
            if scan_interval <= 0:
                scan_interval = 1
        except Exception:
            scan_interval = 1
        logger.info(f"🛠 扫描间隔设置: {scan_interval} 秒（可用环境变量 SCAN_INTERVAL 覆盖）")
        strategy.run_continuous(interval=scan_interval)
        
    except Exception as e:
        logger.error(f"❌ 策略初始化或运行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    main()
