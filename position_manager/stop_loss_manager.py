# -*- coding: utf-8 -*-
"""
止盈止损管理器
"""
import time
import logging
import threading
from datetime import datetime
from binance.client import Client

from utils.exchange_utils import (
    get_mark_price, cancel_stop_order, create_stop_loss_order
)
from utils.notification import send_dingtalk_notification

logger = logging.getLogger(__name__)


class StopLossManager:
    """止盈止损管理器"""
    
    def __init__(self, client: Client, config: dict, notification_func=None):
        """
        初始化止盈止损管理器
        Args:
            client: 币安客户端
            config: 配置字典
            notification_func: 通知函数，如果为None则使用默认的钉钉通知
        """
        self.client = client
        self.config = config
        self.stop_loss_config = config.get('stop_loss', {
            'stop_loss_pct': 2.0,
            'low_trail_stop_loss_pct': 0.3,
            'trail_stop_loss_pct': 0.2,
            'higher_trail_stop_loss_pct': 0.25,
            'low_trail_profit_threshold': 0.4,
            'first_trail_profit_threshold': 1.0,
            'second_trail_profit_threshold': 3.0,
        })
        self.leverage_value = config.get('leverage', 10)
        self.dingtalk_webhook = config.get('dingtalk_webhook', '')
        
        # 使用传入的通知函数，如果没有则使用默认的钉钉通知
        if notification_func:
            self.notification_func = notification_func
        else:
            self.notification_func = lambda title, content: send_dingtalk_notification(
                self.dingtalk_webhook, title, content
            )
        
        # 状态变量
        self.highest_profits = {}  # 记录每个持仓的最高盈利值
        self.current_tiers = {}  # 记录当前档位
        self.monitored_positions = set()  # 已监控的持仓
        self.exchange_stop_orders = {}  # 交易所止损订单 {symbol: order_id}
        self.running = True
        self.total_profit_count = 0  # 总止盈次数
        self.total_loss_count = 0  # 总止损次数
        self.total_loss_usdt = 0.0  # 总损失（USDT）
        self.max_loss_usdt = 10.0  # 最大允许损失（USDT），超过此值停止程序
        
        # 回调函数（可选）
        self.on_position_closed = None  # 平仓回调函数
    
    def fetch_positions(self):
        """获取所有持仓"""
        try:
            positions = self.client.futures_position_information()
            return positions
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
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side_str,
                type='MARKET',
                quantity=abs(amount),
                reduceOnly=True
            )
            
            # 计算平仓收益（USDT）
            if side == 'long':
                profit_usdt = (close_price - entry_price) * abs(amount)
            else:  # short
                profit_usdt = (entry_price - close_price) * abs(amount)
            
            # 更新止盈/止损计数
            if is_profit:
                self.total_profit_count += 1
                logger.info(f"已止盈平仓：{symbol}，数量：{amount}，方向：{side}，总止盈次数：{self.total_profit_count}")
            else:
                self.total_loss_count += 1
                logger.info(f"已止损平仓：{symbol}，数量：{amount}，方向：{side}，总止损次数：{self.total_loss_count}")
            
            logger.info(f"Closed position for {symbol} with size {amount}, side: {side}, profit: {profit_usdt:.2f} USDT")
            
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
            
            diff = self.total_loss_count - self.total_profit_count
            logger.info(f"当前统计：止盈次数={self.total_profit_count}，止损次数={self.total_loss_count}，差值（止损-止盈）={diff}，净损失={self.total_loss_usdt:.2f} USDT")
            
            # 检查是否满足停止条件：净损失超过10U
            if self.total_loss_usdt >= self.max_loss_usdt:
                logger.warning(f"净损失({self.total_loss_usdt:.2f} USDT) >= {self.max_loss_usdt} USDT，满足停止条件，程序将停止")
                self.running = False
                return True
            
            # 发送钉钉平仓通知
            profit_type = "止盈" if is_profit else "止损"
            profit_symbol = "📈" if profit_usdt >= 0 else "📉"
            side_cn = "多" if side == 'long' else "空"
            trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            markdown_content = f"""# ✅ 已平仓 [{profit_type}] Today

**交易对**: {symbol}  
**方向**: {side_cn}  
**数量**: {abs(amount)}  
**开仓价格**: ${entry_price:.6f}  
**平仓价格**: ${close_price:.6f}  

**平仓收益**: {profit_symbol} {profit_usdt:.2f} USDT  

**统计信息**：
- 总止盈次数: {self.total_profit_count}
- 总止损次数: {self.total_loss_count}
- 差值（止损-止盈）: {diff}
- 净损失: {self.total_loss_usdt:.2f} USDT

**触发时间**: {trigger_time}

---
*自动交易机器人*"""
            
            self.notification_func(f"平仓通知 - {symbol} Today", markdown_content)
            
            # 取消止损订单（如果存在）
            if symbol in self.exchange_stop_orders:
                stop_order_id = self.exchange_stop_orders.pop(symbol)
                cancel_stop_order(self.client, symbol, stop_order_id)
            
            # 清除监控记录
            self.monitored_positions.discard(symbol)
            self.highest_profits.pop(symbol, None)
            self.current_tiers.pop(symbol, None)
            
            # 调用回调函数（如果有）
            if self.on_position_closed:
                self.on_position_closed(symbol, amount, side, profit_usdt, is_profit)
            
            return True
        except Exception as e:
            logger.error(f"Error closing position for {symbol}: {e}")
            return False
    
    def monitor_positions(self):
        """监控持仓并执行止盈止损"""
        positions = self.fetch_positions()
        for position in positions:
            symbol = position['symbol']
            position_amt = float(position['positionAmt'])
            
            if position_amt == 0:
                # 如果持仓为0，清除监控记录和止损订单
                if symbol in self.monitored_positions:
                    self.monitored_positions.discard(symbol)
                    self.highest_profits.pop(symbol, None)
                    self.current_tiers.pop(symbol, None)
                # 取消止损订单（如果存在）
                if symbol in self.exchange_stop_orders:
                    stop_order_id = self.exchange_stop_orders.pop(symbol)
                    cancel_stop_order(self.client, symbol, stop_order_id)
                continue
            
            entry_price = float(position['entryPrice'])
            mark_price = get_mark_price(self.client, symbol)
            
            # 判断方向
            if position_amt > 0:
                side = 'long'
            elif position_amt < 0:
                side = 'short'
            else:
                continue
            
            # 首次检测到持仓，初始化记录
            if symbol not in self.monitored_positions:
                self.monitored_positions.add(symbol)
                self.highest_profits[symbol] = 0
                self.current_tiers[symbol] = "无"
                logger.info(f"首次检测到仓位：{symbol}，仓位数量：{position_amt}，开仓价格：{entry_price}，方向：{side}")
                
                # 设置交易所止损订单（基础止损-2%）
                stop_order_id = create_stop_loss_order(
                    self.client, symbol, entry_price, position_amt, side,
                    self.stop_loss_config['stop_loss_pct']
                )
                if stop_order_id:
                    self.exchange_stop_orders[symbol] = stop_order_id
                    logger.info(f"{symbol} 已设置交易所止损订单（-{self.stop_loss_config['stop_loss_pct']}%），订单ID：{stop_order_id}")
                else:
                    logger.warning(f"{symbol} 设置交易所止损订单失败，将使用程序监控止损")
                
                # 发送钉钉仓位开启通知
                side_cn = "多" if side == 'long' else "空"
                trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                markdown_content = f"""# 📈 仓位已开启 Today

**交易对**: {symbol}  
**方向**: {side_cn}  
**数量**: {abs(position_amt)}  
**开仓价格**: ${entry_price:.6f}  
**当前价格**: ${mark_price:.6f}  
**杠杆**: {self.leverage_value}x  
**止损比例**: -{self.stop_loss_config['stop_loss_pct']:.1f}%  

**触发时间**: {trigger_time}

---
*自动交易机器人*"""
                
                self.notification_func(f"仓位开启 - {symbol} Today", markdown_content)
            
            # 计算浮动盈亏百分比
            if side == 'long':
                profit_pct = (mark_price - entry_price) / entry_price * 100
            else:  # short
                profit_pct = (entry_price - mark_price) / entry_price * 100
            
            # 更新最高盈利值
            highest_profit = self.highest_profits.get(symbol, 0)
            if profit_pct > highest_profit:
                highest_profit = profit_pct
                self.highest_profits[symbol] = highest_profit
            
            # 更新当前档位
            previous_tier = self.current_tiers.get(symbol, "无")
            current_tier = "无"
            if highest_profit >= self.stop_loss_config['second_trail_profit_threshold']:
                current_tier = "第二档移动止盈"
            elif highest_profit >= self.stop_loss_config['first_trail_profit_threshold']:
                current_tier = "第一档移动止盈"
            elif highest_profit >= self.stop_loss_config['low_trail_profit_threshold']:
                current_tier = "低档保护止盈"
            
            # 如果从"无"档位进入移动止盈档位，取消交易所止损订单，改用程序监控
            if previous_tier == "无" and current_tier != "无":
                if symbol in self.exchange_stop_orders:
                    stop_order_id = self.exchange_stop_orders.pop(symbol)
                    cancel_stop_order(self.client, symbol, stop_order_id)
                    logger.info(f"{symbol} 进入移动止盈档位（{current_tier}），已取消交易所止损订单，改用程序监控")
            
            self.current_tiers[symbol] = current_tier
            
            # 为浮动盈亏和最高盈亏添加颜色
            profit_color = '\033[92m' if profit_pct >= 0 else '\033[91m'
            highest_color = '\033[92m' if highest_profit >= 0 else '\033[91m'
            reset_color = '\033[0m'
            
            profit_pct_str = f"{profit_color}{profit_pct:.2f}%{reset_color}"
            highest_profit_str = f"{highest_color}{highest_profit:.2f}%{reset_color}"
            
            logger.info(
                f"监控 {symbol}，仓位：{position_amt}，方向：{side}，开仓价格：{entry_price}，当前价格：{mark_price}，"
                f"浮动盈亏：{profit_pct_str}，最高盈亏：{highest_profit_str}，当前档位：{current_tier}"
            )
            
            # 根据档位执行止盈或止损策略
            if current_tier == "低档保护止盈":
                # 回撤0.2%：从最高盈利回撤0.2%时触发
                if profit_pct >= 0 and highest_profit - profit_pct >= self.stop_loss_config['low_trail_stop_loss_pct']:
                    logger.info(f"{symbol} 触发低档保护止盈，最高盈利：{highest_profit:.2f}%，当前盈亏：{profit_pct:.2f}%，回撤：{highest_profit - profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, position_amt, side, entry_price, mark_price, is_profit=True)
                    if not self.running:
                        break
                    continue
            
            elif current_tier == "第一档移动止盈":
                if profit_pct >= 0:
                    trail_stop_loss = highest_profit * (1 - self.stop_loss_config['trail_stop_loss_pct'])
                    if profit_pct <= trail_stop_loss:
                        logger.info(
                            f"{symbol} 达到利润回撤阈值，当前档位：第一档移动止盈，最高盈亏：{highest_profit:.2f}%，"
                            f"当前盈亏：{profit_pct:.2f}%，执行平仓"
                        )
                        self.close_position(symbol, position_amt, side, entry_price, mark_price, is_profit=True)
                        if not self.running:
                            break
                        continue
            
            elif current_tier == "第二档移动止盈":
                if profit_pct >= 0:
                    trail_stop_loss = highest_profit * (1 - self.stop_loss_config['higher_trail_stop_loss_pct'])
                    if profit_pct <= trail_stop_loss:
                        logger.info(
                            f"{symbol} 达到利润回撤阈值，当前档位：第二档移动止盈，最高盈亏：{highest_profit:.2f}%，"
                            f"当前盈亏：{profit_pct:.2f}%，执行平仓"
                        )
                        self.close_position(symbol, position_amt, side, entry_price, mark_price, is_profit=True)
                        if not self.running:
                            break
                        continue
            
            # 基础止损逻辑
            if profit_pct <= -self.stop_loss_config['stop_loss_pct']:
                logger.info(f"{symbol} 触发止损，当前盈亏：{profit_pct:.2f}%，执行平仓")
                self.close_position(symbol, position_amt, side, entry_price, mark_price, is_profit=False)
                if not self.running:
                    break
    
    def start_monitoring(self, monitor_interval=1.5):
        """
        启动监控循环（可独立运行）
        Args:
            monitor_interval: 监控间隔（秒）
        """
        logger.info("启动止盈止损监控...")
        while self.running:
            try:
                if not self.running:
                    break
                self.monitor_positions()
                if not self.running:
                    break
                time.sleep(monitor_interval)
            except Exception as e:
                logger.error(f"持仓监控循环异常: {e}")
                if not self.running:
                    break
                time.sleep(monitor_interval)
    
    def stop(self):
        """停止监控"""
        self.running = False

