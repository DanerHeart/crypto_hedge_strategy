# -*- coding: utf-8 -*-
"""
对冲策略止盈止损管理器
- 监控同一交易对的多空持仓
- 当一方亏损超过1%时，亏损方止损，盈利方进入移动止盈
"""
import time
import logging
import threading
from datetime import datetime
from binance.client import Client

from utils.exchange_utils import (
    get_mark_price,
    get_account_balance
)
from utils.notification import send_dingtalk_notification

logger = logging.getLogger(__name__)

# ANSI颜色代码
class Colors:
    GREEN = '\033[92m'  # 绿色（盈利）
    RED = '\033[91m'    # 红色（亏损）
    YELLOW = '\033[93m' # 黄色（警告）
    RESET = '\033[0m'   # 重置颜色

def colorize_profit(value: float, is_percent: bool = False) -> str:
    """
    为盈亏数字添加颜色
    Args:
        value: 盈亏值
        is_percent: 是否为百分比
    Returns:
        带颜色的字符串
    """
    if is_percent:
        if value > 0:
            return f"{Colors.GREEN}{value:.2f}%{Colors.RESET}"
        elif value < 0:
            return f"{Colors.RED}{value:.2f}%{Colors.RESET}"
        else:
            return f"{value:.2f}%"
    else:
        if value > 0:
            return f"{Colors.GREEN}{value:.2f}{Colors.RESET}"
        elif value < 0:
            return f"{Colors.RED}{value:.2f}{Colors.RESET}"
        else:
            return f"{value:.2f}"


class HedgeStopLossManager:
    """对冲策略止盈止损管理器"""
    
    def __init__(self, client: Client, config: dict, notification_func=None):
        """
        初始化对冲策略止盈止损管理器
        Args:
            client: 币安客户端
            config: 配置字典
            notification_func: 通知函数（已废弃，不再使用）
        """
        self.client = client
        self.config = config
        self.stop_loss_config = config.get('stop_loss', {
            'stop_loss_pct': 1.0,  # 对冲策略的止损百分比（1%）
            'lowest_trail_profit_threshold': 0.7,
            'lowest_trail_stop_loss_pct': 0.2,
            'low_trail_profit_threshold': 1.0,
            'low_trail_stop_loss_pct': 0.3,
            'low_mid_trail_profit_threshold': 1.3,
            'low_mid_trail_stop_loss_pct': 0.3,
            'low_mid2_trail_profit_threshold': 1.5,
            'low_mid2_trail_stop_loss_pct': 0.3,
            'first_trail_profit_threshold': 2.0,
            'trail_stop_loss_pct': 0.3,
            'first_mid_trail_profit_threshold': 2.5,
            'first_mid_trail_stop_loss_pct': 0.3,
            'second_trail_profit_threshold': 3.0,
            'higher_trail_stop_loss_pct': 0.25,
            'second_mid_trail_profit_threshold': 4.0,
            'second_mid_trail_stop_loss_pct': 0.25,
            'third_trail_profit_threshold': 5.0,
            'third_trail_stop_loss_pct': 0.2,
            'third_mid_trail_profit_threshold': 7.5,
            'third_mid_trail_stop_loss_pct': 0.2,
            'fourth_trail_profit_threshold': 10.0,
            'fourth_trail_stop_loss_pct': 0.2,
        })
        self.leverage_value = config.get('leverage', 10)
        
        # 从配置中读取累计总收益的停止条件
        # max_total_profit_loss_usdt: 累计总收益的最大亏损阈值（负数表示亏损），如果累计总收益 <= 此值，停止机器人
        # min_total_profit_usdt: 累计总收益的目标盈利阈值（正数），如果累计总收益 >= 此值，停止机器人（可选）
        self.max_total_profit_loss_usdt = config.get('max_total_profit_loss_usdt', None)  # 默认不设置
        self.min_total_profit_usdt = config.get('min_total_profit_usdt', None)  # 默认不设置
        
        # 钉钉通知配置
        self.dingtalk_webhook = config.get('dingtalk_webhook', '')
        self.enable_dingtalk_notification = config.get('enable_dingtalk_notification', True)  # 默认启用钉钉通知
        
        # 状态变量
        self.hedge_positions = {}  # 记录对冲持仓 {symbol: {'long': {...}, 'short': {...}}}
        self.highest_profits = {}  # 记录每个持仓的最高盈利值 {symbol: {'long': float, 'short': float}}
        self.current_tiers = {}  # 记录当前档位 {symbol: {'long': str, 'short': str}}
        self.exchange_stop_orders = {}  # 交易所止损订单 {symbol: {'long': order_id, 'short': order_id}}
        self.position_open_times = {}  # 记录持仓开仓时间 {symbol: {'long': timestamp, 'short': timestamp}}
        self.min_monitor_delay = 5.0  # 开仓后最小监控延迟（秒），避免立即平仓
        self.running = True
        self.total_loss_usdt = 0.0  # 净损失（USDT），盈利可以抵消亏损
        self.total_profit_usdt = 0.0  # 累计总收益（USDT），开启机器人后的总利润
        
        # 记录每对交易的收益，用于在一对交易完成后发送通知
        # {symbol: {'long': profit_usdt, 'short': profit_usdt, 'long_entry': price, 'short_entry': price, ...}}
        self.pair_profits = {}
        
        # 记录机器人启动时的初始余额，用于计算累计总收益
        self.initial_balance = get_account_balance(self.client)
        logger.info(f"[对冲策略] 机器人启动时初始余额：{self.initial_balance:.2f} USDT")
        
        # 日志输出控制：每5秒输出一次监控日志
        self.last_log_time = {}  # {symbol: timestamp} 记录每个交易对上次输出日志的时间
        self.log_interval = 5.0  # 日志输出间隔（秒）
        
        # 回调函数（可选）
        self.on_position_closed = None  # 平仓回调函数
    
    def fetch_positions(self):
        """获取所有持仓"""
        try:
            positions = self.client.futures_position_information()
            # 过滤出非零持仓
            non_zero_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
            
            # 调试：打印BTCUSDT和LIGHTUSDT的持仓信息（包括数量为0的）
            debug_symbols = ['BTCUSDT', 'LIGHTUSDT']
            for p in positions:
                if p.get('symbol') in debug_symbols:
                    symbol = p.get('symbol')
                    position_amt = float(p.get('positionAmt', 0))
                    entry_price = float(p.get('entryPrice', 0))
                    logger.debug(f"[对冲策略] {symbol} 持仓详情：数量={position_amt}, 开仓价={entry_price}")
            
            return non_zero_positions
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []
    
    def close_position(self, symbol: str, amount: float, side: str, 
                      entry_price: float, close_price: float, is_profit: bool = False):
        """
        平仓
        Args:
            symbol: 交易对
            amount: 数量
            side: 方向 ('long' 或 'short')
            entry_price: 开仓价格
            close_price: 平仓价格
            is_profit: 是否为止盈（True=止盈，False=止损）
        Returns:
            bool: 是否成功
        """
        try:
            side_str = 'SELL' if side == 'long' else 'BUY'
            # 使用双向持仓模式平仓
            # 注意：双向持仓模式下，平仓时只需要指定positionSide，不需要reduceOnly参数
            position_side = 'LONG' if side == 'long' else 'SHORT'
            try:
                order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side_str,
                    type='MARKET',
                    quantity=abs(amount),
                    positionSide=position_side  # 双向持仓模式，不需要reduceOnly参数
                )
            except Exception as e:
                # 如果双向持仓模式失败，尝试使用单向模式
                if 'positionSide' in str(e) or 'hedge' in str(e).lower() or 'reduceonly' in str(e).lower():
                    logger.warning(f"{symbol} 双向持仓平仓失败，尝试使用单向模式")
                    order = self.client.futures_create_order(
                        symbol=symbol,
                        side=side_str,
                        type='MARKET',
                        quantity=abs(amount),
                        reduceOnly=True
                    )
                else:
                    raise
            
            # 等待一小段时间，确保平仓订单已完全执行
            time.sleep(0.5)
            
            # 使用价格差计算单个持仓的盈亏（用于显示和百分比）
            if side == 'long':
                profit_usdt = (close_price - entry_price) * abs(amount)
            else:  # short
                profit_usdt = (entry_price - close_price) * abs(amount)
            
            # 计算盈亏百分比
            profit_pct = (profit_usdt / (entry_price * abs(amount))) * 100 if entry_price > 0 else 0
            
            # 记录平仓信息（带颜色）
            profit_type = "止盈" if is_profit else "止损"
            side_cn = "多" if side == 'long' else "空"
            profit_usdt_colored = colorize_profit(profit_usdt)
            profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
            
            logger.info(f"[对冲策略] 已{profit_type}平仓：{symbol} {side_cn}单，数量：{amount}，"
                       f"收益：{profit_usdt_colored} USDT ({profit_pct_colored})")
            
            # 计算净损失（盈利可以抵消亏损）
            # profit_usdt < 0 表示亏损，profit_usdt > 0 表示盈利
            if profit_usdt < 0:
                # 亏损：增加净损失
                self.total_loss_usdt += abs(profit_usdt)
            else:
                # 盈利：减少净损失（盈利抵消亏损）
                self.total_loss_usdt -= profit_usdt
                if self.total_loss_usdt < 0:
                    self.total_loss_usdt = 0  # 净损失不能为负，如果盈利超过亏损，净损失为0
            
            # 注意：不在这里输出净损失，等一对对冲都结束后再输出
            
            # 记录当前平仓的收益
            if symbol not in self.pair_profits:
                self.pair_profits[symbol] = {}
            
            # 记录当前方向的收益和相关信息
            self.pair_profits[symbol][side] = {
                'profit_usdt': profit_usdt,
                'profit_pct': profit_pct,
                'entry_price': entry_price,
                'close_price': close_price,
                'amount': abs(amount),
                'is_profit': is_profit,
                'close_time': datetime.now()
            }
            
            # 检查是否一对交易都已完成（多空双方都已平仓）
            if 'long' in self.pair_profits[symbol] and 'short' in self.pair_profits[symbol]:
                # 计算一对交易的总收益（使用价格差，用于显示）
                long_profit = self.pair_profits[symbol]['long']['profit_usdt']
                short_profit = self.pair_profits[symbol]['short']['profit_usdt']
                pair_total_profit = long_profit + short_profit  # 本次对冲的总收益（价格差计算）
                
                # 查询当前余额，计算累计总收益（基于账户余额差值）
                current_balance = get_account_balance(self.client)
                self.total_profit_usdt = current_balance - self.initial_balance  # 累计总收益 = 当前余额 - 初始余额
                
                # 计算总收益率（基于总投入）
                long_entry = self.pair_profits[symbol]['long']['entry_price']
                short_entry = self.pair_profits[symbol]['short']['entry_price']
                long_amount = self.pair_profits[symbol]['long']['amount']
                short_amount = self.pair_profits[symbol]['short']['amount']
                total_investment = (long_entry * long_amount) + (short_entry * short_amount)
                total_profit_pct = (pair_total_profit / total_investment) * 100 if total_investment > 0 else 0
                
                # 输出本次对冲的总收益和累计总收益（带颜色，突出显示）
                pair_profit_colored = colorize_profit(pair_total_profit)
                total_profit_colored = colorize_profit(self.total_profit_usdt)
                
                logger.info("=" * 70)
                logger.info(f"[对冲策略] ✅ 一对对冲完成：{symbol}")
                logger.info(f"[对冲策略] 📊 本次对冲总收益：{pair_profit_colored} USDT ({total_profit_pct:.2f}%)")
                logger.info(f"[对冲策略] 💰 累计总收益（余额差值）：{total_profit_colored} USDT")
                logger.info(f"[对冲策略] 📈 当前账户余额：{current_balance:.2f} USDT | 初始余额：{self.initial_balance:.2f} USDT")
                
                # 显示累计总收益的停止条件
                stop_conditions = []
                if self.max_total_profit_loss_usdt is not None:
                    max_loss_colored_profit = colorize_profit(self.max_total_profit_loss_usdt)
                    stop_conditions.append(f"累计亏损阈值：{max_loss_colored_profit} USDT")
                if self.min_total_profit_usdt is not None:
                    min_profit_colored = colorize_profit(self.min_total_profit_usdt)
                    stop_conditions.append(f"累计盈利目标：{min_profit_colored} USDT")
                
                if stop_conditions:
                    logger.info(f"[对冲策略] 🎯 停止条件：{', '.join(stop_conditions)}")
                
                logger.info("=" * 70)
                
                # 检查是否满足停止条件：累计总收益亏损超过阈值
                if self.max_total_profit_loss_usdt is not None and self.total_profit_usdt <= self.max_total_profit_loss_usdt:
                    logger.warning(f"[对冲策略] 累计总收益({self.total_profit_usdt:.2f} USDT) <= {self.max_total_profit_loss_usdt:.2f} USDT（最大允许亏损），满足停止条件，程序将停止")
                    self.running = False
                    return True
                
                # 检查是否满足停止条件3：累计总收益达到目标盈利阈值
                if self.min_total_profit_usdt is not None and self.total_profit_usdt >= self.min_total_profit_usdt:
                    logger.info(f"[对冲策略] ✅ 累计总收益({self.total_profit_usdt:.2f} USDT) >= {self.min_total_profit_usdt:.2f} USDT（目标盈利），满足停止条件，程序将停止")
                    self.running = False
                    return True
                
                # 发送钉钉通知
                self._send_pair_completion_notification(
                    symbol=symbol,
                    long_profit=long_profit,
                    short_profit=short_profit,
                    total_profit=pair_total_profit,
                    total_profit_pct=total_profit_pct,
                    long_info=self.pair_profits[symbol]['long'],
                    short_info=self.pair_profits[symbol]['short']
                )
                
                # 清除该交易对的收益记录
                self.pair_profits.pop(symbol, None)
            
            # 清除监控记录
            if symbol in self.hedge_positions:
                self.hedge_positions[symbol].pop(side, None)
                if not self.hedge_positions[symbol]:  # 如果两个方向都平仓了，清除整个记录
                    self.hedge_positions.pop(symbol, None)
                    self.highest_profits.pop(symbol, None)
                    self.current_tiers.pop(symbol, None)
                    if symbol in self.exchange_stop_orders:
                        self.exchange_stop_orders.pop(symbol, None)
                    if symbol in self.position_open_times:
                        self.position_open_times.pop(symbol, None)
            
            # 调用回调函数（如果有）
            if self.on_position_closed:
                self.on_position_closed(symbol, amount, side, profit_usdt, is_profit)
            
            return True
        except Exception as e:
            logger.error(f"Error closing position for {symbol} {side}: {e}")
            return False
    
    def monitor_positions(self):
        """监控对冲持仓并执行止盈止损"""
        positions = self.fetch_positions()
        logger.debug(f"[对冲策略] 获取到 {len(positions)} 个持仓信息")
        
        # 调试：打印所有持仓信息（包括数量为0的）
        if len(positions) > 0:
            for pos in positions[:5]:  # 只打印前5个，避免日志过多
                symbol = pos.get('symbol', 'UNKNOWN')
                position_amt = float(pos.get('positionAmt', 0))
                entry_price = float(pos.get('entryPrice', 0))
                logger.debug(f"[对冲策略] 持仓详情：{symbol}，数量：{position_amt}，开仓价格：{entry_price}")
        
        # 按交易对分组持仓
        positions_by_symbol = {}
        for position in positions:
            symbol = position['symbol']
            position_amt = float(position['positionAmt'])
            entry_price = float(position.get('entryPrice', 0))
            logger.debug(f"[对冲策略] 检查持仓：{symbol}，数量：{position_amt}，开仓价格：{entry_price}")
            if position_amt == 0:
                # 如果持仓为0，检查是否有记录需要清理
                if symbol in self.hedge_positions:
                    # 检查是否还有单边持仓需要监控
                    remaining_sides = []
                    if symbol in self.hedge_positions:
                        for side in ['long', 'short']:
                            if side in self.hedge_positions[symbol]:
                                remaining_sides.append(side)
                    
                    # 如果没有剩余持仓，清除所有记录
                    if not remaining_sides:
                        self.hedge_positions.pop(symbol, None)
                        self.highest_profits.pop(symbol, None)
                        self.current_tiers.pop(symbol, None)
                        if symbol in self.exchange_stop_orders:
                            self.exchange_stop_orders.pop(symbol, None)
                        if symbol in self.position_open_times:
                            self.position_open_times.pop(symbol, None)
                continue
            
            if symbol not in positions_by_symbol:
                positions_by_symbol[symbol] = {}
            
            if position_amt > 0:
                positions_by_symbol[symbol]['long'] = position
                logger.debug(f"[对冲策略] 检测到 {symbol} 多单持仓：{position_amt}")
            elif position_amt < 0:
                positions_by_symbol[symbol]['short'] = position
                logger.debug(f"[对冲策略] 检测到 {symbol} 空单持仓：{position_amt}")
        
        # 处理所有持仓（包括对冲持仓和单边持仓）
        for symbol, pos_dict in positions_by_symbol.items():
            # 检查是否是对冲持仓（同时有多空）
            is_hedge = 'long' in pos_dict and 'short' in pos_dict
            
            if is_hedge:
                # 处理对冲持仓
                logger.debug(f"[对冲策略] {symbol} 检测到对冲持仓（同时有多空），开始监控")
                self._monitor_hedge_position(symbol, pos_dict)
            else:
                # 处理单边持仓（一方已止损，另一方继续监控）
                if 'long' in pos_dict:
                    logger.debug(f"[对冲策略] {symbol} 检测到单边持仓（只有多单）")
                elif 'short' in pos_dict:
                    logger.debug(f"[对冲策略] {symbol} 检测到单边持仓（只有空单）")
                
                # 注意：如果只有单边持仓且不在hedge_positions中，说明可能是新开的单边持仓，不应该监控
                # 只有在对冲持仓记录中存在的单边持仓才应该监控（说明另一方已止损）
                if symbol in self.hedge_positions:
                    logger.debug(f"[对冲策略] {symbol} 单边持仓在对冲记录中，开始监控")
                    self._monitor_single_position(symbol, pos_dict)
                else:
                    # 如果不在对冲持仓记录中，说明可能是新开的单边持仓，不应该监控
                    logger.debug(f"[对冲策略] {symbol} 检测到单边持仓但不在对冲记录中，跳过监控（可能是新开的单边持仓）")
            
    def _monitor_hedge_position(self, symbol: str, pos_dict: dict):
        """
        监控对冲持仓（同时有多空）
        Args:
            symbol: 交易对
            pos_dict: 持仓字典 {'long': position, 'short': position}
        """
        long_pos = pos_dict['long']
        short_pos = pos_dict['short']
        
        long_amt = float(long_pos['positionAmt'])
        short_amt = float(short_pos['positionAmt'])
        
        if long_amt == 0 or short_amt == 0:
            return
        
        long_entry = float(long_pos['entryPrice'])
        short_entry = float(short_pos['entryPrice'])
        mark_price = get_mark_price(self.client, symbol)
        
        # 初始化对冲持仓记录
        current_time = time.time()
        is_new_hedge = symbol not in self.hedge_positions
        if is_new_hedge:
            self.hedge_positions[symbol] = {
                'long': {'entry_price': long_entry, 'amount': long_amt},
                'short': {'entry_price': short_entry, 'amount': short_amt}
            }
            self.highest_profits[symbol] = {'long': 0, 'short': 0}
            self.current_tiers[symbol] = {'long': "无", 'short': "无"}
            # 记录开仓时间
            self.position_open_times[symbol] = {
                'long': current_time,
                'short': current_time
            }
            
            logger.info(f"[对冲策略] 首次检测到对冲仓位：{symbol}，多单：{long_amt}@{long_entry}，空单：{short_amt}@{short_entry}")
            logger.info(f"[对冲策略] {symbol} 使用程序监控止损（监控间隔：300ms，止损阈值：-{self.stop_loss_config['stop_loss_pct']}%）")
            
            # 首次检测到持仓，等待一段时间后再开始监控止损，避免立即平仓
            return
        
        # 检查开仓时间，如果开仓时间太短，跳过止损检查（避免立即平仓）
        if symbol in self.position_open_times:
            long_open_time = self.position_open_times[symbol].get('long', current_time)
            short_open_time = self.position_open_times[symbol].get('short', current_time)
            long_time_since_open = current_time - long_open_time
            short_time_since_open = current_time - short_open_time
            
            # 如果开仓时间不足最小延迟时间，跳过止损检查
            if long_time_since_open < self.min_monitor_delay or short_time_since_open < self.min_monitor_delay:
                logger.debug(
                    f"[对冲策略] {symbol} 开仓时间过短（多单：{long_time_since_open:.1f}秒，空单：{short_time_since_open:.1f}秒），"
                    f"跳过止损检查（最小延迟：{self.min_monitor_delay}秒）"
                )
                return
        
        # 计算多空盈亏百分比
        long_profit_pct = (mark_price - long_entry) / long_entry * 100
        short_profit_pct = (short_entry - mark_price) / short_entry * 100
        
        # 更新最高盈利值
        if long_profit_pct > self.highest_profits[symbol]['long']:
            self.highest_profits[symbol]['long'] = long_profit_pct
        if short_profit_pct > self.highest_profits[symbol]['short']:
            self.highest_profits[symbol]['short'] = short_profit_pct
        
        highest_long = self.highest_profits[symbol]['long']
        highest_short = self.highest_profits[symbol]['short']
        
        # 使用颜色标识盈亏
        long_profit_pct_colored = colorize_profit(long_profit_pct, is_percent=True)
        short_profit_pct_colored = colorize_profit(short_profit_pct, is_percent=True)
        highest_long_colored = colorize_profit(highest_long, is_percent=True)
        highest_short_colored = colorize_profit(highest_short, is_percent=True)
        
        # 每5秒输出一次监控日志
        current_time = time.time()
        last_log_time = self.last_log_time.get(symbol, 0)
        if current_time - last_log_time >= self.log_interval:
            logger.info(
                f"[对冲策略] 监控 {symbol}，多单盈亏：{long_profit_pct_colored}（最高：{highest_long_colored}），"
                f"空单盈亏：{short_profit_pct_colored}（最高：{highest_short_colored}）"
            )
            self.last_log_time[symbol] = current_time
        
        # 对冲策略核心逻辑：当一方亏损超过1%时，亏损方止损，盈利方进入移动止盈
        # 使用 < 而不是 <=，确保亏损超过阈值才触发止损
        long_loss = long_profit_pct < -self.stop_loss_config['stop_loss_pct']
        short_loss = short_profit_pct < -self.stop_loss_config['stop_loss_pct']
        
        if long_loss and not short_loss:
            # 多单亏损超过1%，空单盈利，多单止损，空单进入移动止盈
            logger.info(f"[对冲策略] {symbol} 多单亏损超过{self.stop_loss_config['stop_loss_pct']}%，执行多单止损，空单进入移动止盈")
            self.close_position(symbol, long_amt, 'long', long_entry, mark_price, is_profit=False)
            
            # 检查是否已触发停止条件
            if not self.running:
                return
            
            # 空单进入移动止盈模式
            if symbol in self.hedge_positions and 'short' in self.hedge_positions[symbol]:
                self._enable_trailing_stop(symbol, 'short', short_entry, short_amt, short_profit_pct, highest_short)
            
            return
        
        elif short_loss and not long_loss:
            # 空单亏损超过1%，多单盈利，空单止损，多单进入移动止盈
            logger.info(f"[对冲策略] {symbol} 空单亏损超过{self.stop_loss_config['stop_loss_pct']}%，执行空单止损，多单进入移动止盈")
            self.close_position(symbol, short_amt, 'short', short_entry, mark_price, is_profit=False)
            
            # 检查是否已触发停止条件
            if not self.running:
                return
            
            # 多单进入移动止盈模式
            if symbol in self.hedge_positions and 'long' in self.hedge_positions[symbol]:
                self._enable_trailing_stop(symbol, 'long', long_entry, long_amt, long_profit_pct, highest_long)
            
            return
        
        elif long_loss and short_loss:
            # 双方都亏损，都止损（这种情况应该很少见）
            logger.warning(f"[对冲策略] {symbol} 多空双方都亏损超过{self.stop_loss_config['stop_loss_pct']}%，执行双方止损")
            self.close_position(symbol, long_amt, 'long', long_entry, mark_price, is_profit=False)
            if not self.running:
                return
            self.close_position(symbol, short_amt, 'short', short_entry, mark_price, is_profit=False)
            if not self.running:
                return
            return
        
        # 如果双方都盈利，检查是否进入移动止盈
        if long_profit_pct > 0 and short_profit_pct > 0:
            # 双方都盈利，检查是否达到移动止盈阈值
            self._check_trailing_stop(symbol, 'long', long_entry, long_amt, long_profit_pct, highest_long, mark_price)
            if not self.running:
                return
            self._check_trailing_stop(symbol, 'short', short_entry, short_amt, short_profit_pct, highest_short, mark_price)
            if not self.running:
                return
    
    def _monitor_single_position(self, symbol: str, pos_dict: dict):
        """
        监控单边持仓（一方已止损，另一方继续监控移动止盈）
        Args:
            symbol: 交易对
            pos_dict: 持仓字典 {'long': position} 或 {'short': position}
        """
        # 检查是否在监控列表中（说明之前是对冲持仓，一方已止损）
        if symbol not in self.hedge_positions:
            return
        
        # 检查开仓时间，如果开仓时间太短，跳过止损检查（避免立即平仓）
        current_time = time.time()
        if symbol in self.position_open_times:
            # 获取当前持仓的开仓时间
            if 'long' in pos_dict:
                side_open_time = self.position_open_times[symbol].get('long', current_time)
            elif 'short' in pos_dict:
                side_open_time = self.position_open_times[symbol].get('short', current_time)
            else:
                return
            
            time_since_open = current_time - side_open_time
            
            # 如果开仓时间不足最小延迟时间，跳过止损检查
            if time_since_open < self.min_monitor_delay:
                logger.debug(
                    f"[对冲策略] {symbol} 单边持仓开仓时间过短（{time_since_open:.1f}秒），"
                    f"跳过止损检查（最小延迟：{self.min_monitor_delay}秒）"
                )
                return
        
        mark_price = get_mark_price(self.client, symbol)
        
        # 处理多单
        if 'long' in pos_dict:
            long_pos = pos_dict['long']
            long_amt = float(long_pos['positionAmt'])
            if long_amt > 0 and symbol in self.hedge_positions and 'long' in self.hedge_positions[symbol]:
                # 使用当前持仓的entryPrice，而不是记录的entry_price（因为持仓数量变化时entryPrice会调整）
                long_entry = float(long_pos['entryPrice'])
                long_profit_pct = (mark_price - long_entry) / long_entry * 100
                
                # 更新最高盈利值
                if symbol in self.highest_profits and 'long' in self.highest_profits[symbol]:
                    if long_profit_pct > self.highest_profits[symbol]['long']:
                        self.highest_profits[symbol]['long'] = long_profit_pct
                    highest_long = self.highest_profits[symbol]['long']
                    
                    # 使用颜色标识盈亏
                    long_profit_pct_colored = colorize_profit(long_profit_pct, is_percent=True)
                    highest_long_colored = colorize_profit(highest_long, is_percent=True)
                    
                    # 每5秒输出一次监控日志
                    current_time = time.time()
                    last_log_time = self.last_log_time.get(f"{symbol}_long", 0)
                    if current_time - last_log_time >= self.log_interval:
                        logger.info(
                            f"[对冲策略] 监控单边持仓 {symbol} 多单，盈亏：{long_profit_pct_colored}（最高：{highest_long_colored}）"
                        )
                        self.last_log_time[f"{symbol}_long"] = current_time
                    
                    # 检查止损：如果亏损超过1%，执行止损
                    if long_profit_pct < -self.stop_loss_config['stop_loss_pct']:
                        logger.info(f"[对冲策略] {symbol} 单边持仓多单亏损超过{self.stop_loss_config['stop_loss_pct']}%，执行止损")
                        self.close_position(symbol, long_amt, 'long', long_entry, mark_price, is_profit=False)
                        if not self.running:
                            return
                        return
                    
                    # 检查移动止盈（只有在没有触发止损的情况下）
                    self._check_trailing_stop(symbol, 'long', long_entry, long_amt, long_profit_pct, highest_long, mark_price)
                    if not self.running:
                        return
        
        # 处理空单
        if 'short' in pos_dict:
            short_pos = pos_dict['short']
            short_amt = float(short_pos['positionAmt'])
            if short_amt < 0 and symbol in self.hedge_positions and 'short' in self.hedge_positions[symbol]:
                # 使用当前持仓的entryPrice，而不是记录的entry_price（因为持仓数量变化时entryPrice会调整）
                short_entry = float(short_pos['entryPrice'])
                short_profit_pct = (short_entry - mark_price) / short_entry * 100
                
                # 更新最高盈利值
                if symbol in self.highest_profits and 'short' in self.highest_profits[symbol]:
                    if short_profit_pct > self.highest_profits[symbol]['short']:
                        self.highest_profits[symbol]['short'] = short_profit_pct
                    highest_short = self.highest_profits[symbol]['short']
                    
                    # 使用颜色标识盈亏
                    short_profit_pct_colored = colorize_profit(short_profit_pct, is_percent=True)
                    highest_short_colored = colorize_profit(highest_short, is_percent=True)
                    
                    # 每5秒输出一次监控日志
                    current_time = time.time()
                    last_log_time = self.last_log_time.get(f"{symbol}_short", 0)
                    if current_time - last_log_time >= self.log_interval:
                        logger.info(
                            f"[对冲策略] 监控单边持仓 {symbol} 空单，盈亏：{short_profit_pct_colored}（最高：{highest_short_colored}）"
                        )
                        self.last_log_time[f"{symbol}_short"] = current_time
                    
                    # 检查止损：如果亏损超过1%，执行止损
                    if short_profit_pct < -self.stop_loss_config['stop_loss_pct']:
                        logger.info(f"[对冲策略] {symbol} 单边持仓空单亏损超过{self.stop_loss_config['stop_loss_pct']}%，执行止损")
                        self.close_position(symbol, short_amt, 'short', short_entry, mark_price, is_profit=False)
                        if not self.running:
                            return
                        return
                    
                    # 检查移动止盈（只有在没有触发止损的情况下）
                    self._check_trailing_stop(symbol, 'short', short_entry, abs(short_amt), short_profit_pct, highest_short, mark_price)
                    if not self.running:
                        return
    
    def _enable_trailing_stop(self, symbol: str, side: str, entry_price: float, 
                             amount: float, profit_pct: float, highest_profit: float):
        """
        启用移动止盈（当一方止损后，另一方进入移动止盈）
        Args:
            symbol: 交易对
            side: 方向 ('long' 或 'short')
            entry_price: 开仓价格
            amount: 数量
            profit_pct: 当前盈亏百分比
            highest_profit: 最高盈亏百分比
        """
        # 更新档位（统一命名：第1-11档移动止盈）
        current_tier = "无"
        if highest_profit >= self.stop_loss_config.get('fourth_trail_profit_threshold', 10.0):
            current_tier = "第11档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('third_mid_trail_profit_threshold', 7.5):
            current_tier = "第10档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('third_trail_profit_threshold', 5.0):
            current_tier = "第9档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('second_mid_trail_profit_threshold', 4.0):
            current_tier = "第8档移动止盈"
        elif highest_profit >= self.stop_loss_config['second_trail_profit_threshold']:
            current_tier = "第7档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('first_mid_trail_profit_threshold', 2.5):
            current_tier = "第6档移动止盈"
        elif highest_profit >= self.stop_loss_config['first_trail_profit_threshold']:
            current_tier = "第5档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('low_mid2_trail_profit_threshold', 1.5):
            current_tier = "第4档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('low_mid_trail_profit_threshold', 1.3):
            current_tier = "第3档移动止盈"
        elif highest_profit >= self.stop_loss_config['low_trail_profit_threshold']:
            current_tier = "第2档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('lowest_trail_profit_threshold', 0.7):
            current_tier = "第1档移动止盈"
        
        if symbol in self.current_tiers:
            self.current_tiers[symbol][side] = current_tier
        
        highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
        logger.info(f"[对冲策略] {symbol} {side} 进入移动止盈模式，当前档位：{current_tier}，最高盈亏：{highest_profit_colored}")
    
    def _check_trailing_stop(self, symbol: str, side: str, entry_price: float, 
                            amount: float, profit_pct: float, highest_profit: float, mark_price: float):
        """
        检查移动止盈条件
        Args:
            symbol: 交易对
            side: 方向 ('long' 或 'short')
            entry_price: 开仓价格
            amount: 数量
            profit_pct: 当前盈亏百分比
            highest_profit: 最高盈亏百分比
            mark_price: 当前标记价格
        """
        if symbol not in self.current_tiers or side not in self.current_tiers[symbol]:
            return
        
        current_tier = self.current_tiers[symbol][side]
        
        # 第1档移动止盈：阈值0.7%，比例回撤20%
        if current_tier == "第1档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 20%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('lowest_trail_stop_loss_pct', 0.2))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第1档移动止盈（比例回撤20%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第2档移动止盈：阈值1.2%，固定回撤0.2%
        elif current_tier == "第2档移动止盈":
            # 固定回撤：从最高盈利回撤固定0.2%时触发
            if profit_pct >= 0 and highest_profit - profit_pct >= self.stop_loss_config['low_trail_stop_loss_pct']:
                highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                pullback = highest_profit - profit_pct
                pullback_colored = colorize_profit(pullback, is_percent=True)
                logger.info(f"[对冲策略] {symbol} {side} 触发第2档移动止盈（固定回撤0.2%），最高盈利：{highest_profit_colored}，当前盈亏：{profit_pct_colored}，回撤：{pullback_colored}，执行平仓")
                self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                if not self.running:
                    return
                return
        
        # 第3档移动止盈：阈值1.3%，固定回撤0.3%
        elif current_tier == "第3档移动止盈":
            # 固定回撤：从最高盈利回撤固定0.3%时触发
            if profit_pct >= 0 and highest_profit - profit_pct >= self.stop_loss_config.get('low_mid_trail_stop_loss_pct', 0.3):
                highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                pullback = highest_profit - profit_pct
                pullback_colored = colorize_profit(pullback, is_percent=True)
                logger.info(f"[对冲策略] {symbol} {side} 触发第3档移动止盈（固定回撤0.3%），最高盈利：{highest_profit_colored}，当前盈亏：{profit_pct_colored}，回撤：{pullback_colored}，执行平仓")
                self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                if not self.running:
                    return
                return
        
        # 第4档移动止盈：阈值1.5%，固定回撤0.3%
        elif current_tier == "第4档移动止盈":
            # 固定回撤：从最高盈利回撤固定0.3%时触发
            if profit_pct >= 0 and highest_profit - profit_pct >= self.stop_loss_config.get('low_mid2_trail_stop_loss_pct', 0.3):
                highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                pullback = highest_profit - profit_pct
                pullback_colored = colorize_profit(pullback, is_percent=True)
                logger.info(f"[对冲策略] {symbol} {side} 触发第4档移动止盈（固定回撤0.3%），最高盈利：{highest_profit_colored}，当前盈亏：{profit_pct_colored}，回撤：{pullback_colored}，执行平仓")
                self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                if not self.running:
                    return
                return
        
        # 第5档移动止盈：阈值2.0%，比例回撤30%
        elif current_tier == "第5档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 30%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config['trail_stop_loss_pct'])
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第4档移动止盈（比例回撤30%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第6档移动止盈：阈值2.5%，比例回撤30%
        elif current_tier == "第6档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 30%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('first_mid_trail_stop_loss_pct', 0.3))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第5档移动止盈（比例回撤30%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第7档移动止盈：阈值3.0%，比例回撤25%
        elif current_tier == "第7档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 25%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config['higher_trail_stop_loss_pct'])
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第6档移动止盈（比例回撤25%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第8档移动止盈：阈值4.0%，比例回撤25%
        elif current_tier == "第8档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 25%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('second_mid_trail_stop_loss_pct', 0.25))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第7档移动止盈（比例回撤25%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第9档移动止盈：阈值5.0%，比例回撤20%
        elif current_tier == "第9档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 20%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('third_trail_stop_loss_pct', 0.2))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第8档移动止盈（比例回撤20%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第10档移动止盈：阈值7.5%，比例回撤20%
        elif current_tier == "第10档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 20%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('third_mid_trail_stop_loss_pct', 0.2))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第9档移动止盈（比例回撤20%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 第11档移动止盈：阈值10.0%，比例回撤20%
        elif current_tier == "第11档移动止盈":
            # 比例回撤：当前盈亏 ≤ 最高盈利 × (1 - 20%) 时触发
            if profit_pct >= 0:
                trail_stop_loss = highest_profit * (1 - self.stop_loss_config.get('fourth_trail_stop_loss_pct', 0.2))
                if profit_pct <= trail_stop_loss:
                    highest_profit_colored = colorize_profit(highest_profit, is_percent=True)
                    profit_pct_colored = colorize_profit(profit_pct, is_percent=True)
                    logger.info(
                        f"[对冲策略] {symbol} {side} 触发第10档移动止盈（比例回撤20%），最高盈亏：{highest_profit_colored}，"
                        f"当前盈亏：{profit_pct_colored}，执行平仓"
                    )
                    self.close_position(symbol, amount, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        return
                    return
        
        # 更新档位（统一命名：第1-11档移动止盈）
        new_tier = "无"
        if highest_profit >= self.stop_loss_config.get('fourth_trail_profit_threshold', 10.0):
            new_tier = "第11档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('third_mid_trail_profit_threshold', 7.5):
            new_tier = "第10档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('third_trail_profit_threshold', 5.0):
            new_tier = "第9档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('second_mid_trail_profit_threshold', 4.0):
            new_tier = "第8档移动止盈"
        elif highest_profit >= self.stop_loss_config['second_trail_profit_threshold']:
            new_tier = "第7档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('first_mid_trail_profit_threshold', 2.5):
            new_tier = "第6档移动止盈"
        elif highest_profit >= self.stop_loss_config['first_trail_profit_threshold']:
            new_tier = "第5档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('low_mid2_trail_profit_threshold', 1.5):
            new_tier = "第4档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('low_mid_trail_profit_threshold', 1.3):
            new_tier = "第3档移动止盈"
        elif highest_profit >= self.stop_loss_config['low_trail_profit_threshold']:
            new_tier = "第2档移动止盈"
        elif highest_profit >= self.stop_loss_config.get('lowest_trail_profit_threshold', 0.7):
            new_tier = "第1档移动止盈"
        
        if new_tier != current_tier:
            self.current_tiers[symbol][side] = new_tier
            logger.info(f"[对冲策略] {symbol} {side} 档位更新：{current_tier} -> {new_tier}")
    
    def start_monitoring(self, monitor_interval=0.3):
        """
        启动监控循环（可独立运行）
        Args:
            monitor_interval: 监控间隔（秒）
        """
        logger.info("启动对冲策略止盈止损监控...")
        while self.running:
            try:
                if not self.running:
                    break
                self.monitor_positions()
                if not self.running:
                    break
                time.sleep(monitor_interval)
            except Exception as e:
                logger.error(f"对冲策略持仓监控循环异常: {e}")
                if not self.running:
                    break
                time.sleep(monitor_interval)
    
    def stop(self):
        """停止监控"""
        self.running = False
    
    def _send_pair_completion_notification(self, symbol: str, long_profit: float, short_profit: float,
                                         total_profit: float, total_profit_pct: float,
                                         long_info: dict, short_info: dict):
        """
        发送一对交易完成的通知
        Args:
            symbol: 交易对
            long_profit: 多单收益（USDT）
            short_profit: 空单收益（USDT）
            total_profit: 总收益（USDT）
            total_profit_pct: 总收益率（%）
            long_info: 多单详细信息
            short_info: 空单详细信息
        """
        # 检查是否启用钉钉通知
        if not self.enable_dingtalk_notification:
            logger.debug(f"[对冲策略] 钉钉通知已禁用，跳过发送交易完成通知")
            return
        
        if not self.dingtalk_webhook:
            logger.warning(f"[对冲策略] 钉钉Webhook未配置，无法发送通知")
            return
        
        # 格式化收益显示
        profit_symbol = "📈" if total_profit >= 0 else "📉"
        long_profit_symbol = "📈" if long_profit >= 0 else "📉"
        short_profit_symbol = "📈" if short_profit >= 0 else "📉"
        
        # 格式化时间
        long_close_time = long_info['close_time'].strftime("%Y-%m-%d %H:%M:%S")
        short_close_time = short_info['close_time'].strftime("%Y-%m-%d %H:%M:%S")
        
        markdown_content = f"""# ✅ 一对交易完成 [对冲策略] bull

**交易对**: {symbol}

---

## 📊 多单信息
- **开仓价格**: ${long_info['entry_price']:.6f}
- **平仓价格**: ${long_info['close_price']:.6f}
- **数量**: {long_info['amount']}
- **收益**: {long_profit_symbol} {long_profit:.2f} USDT ({long_info['profit_pct']:.2f}%)
- **平仓时间**: {long_close_time}

---

## 📊 空单信息
- **开仓价格**: ${short_info['entry_price']:.6f}
- **平仓价格**: ${short_info['close_price']:.6f}
- **数量**: {short_info['amount']}
- **收益**: {short_profit_symbol} {short_profit:.2f} USDT ({short_info['profit_pct']:.2f}%)
- **平仓时间**: {short_close_time}

---

## 💰 本次对冲总收益
**本次对冲总收益**: {profit_symbol} {total_profit:.2f} USDT ({total_profit_pct:.2f}%)

## 📈 累计总收益
**累计总收益**: {profit_symbol} {self.total_profit_usdt:.2f} USDT

**当前净损失**: {self.total_loss_usdt:.2f} USDT

---
*自动交易机器人 - 对冲策略*"""
        
        send_dingtalk_notification(
            self.dingtalk_webhook,
            f"一对交易完成 - {symbol} [对冲策略] bull",
            markdown_content
        )
