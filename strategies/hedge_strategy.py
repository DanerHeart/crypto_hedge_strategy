# -*- coding: utf-8 -*-
"""
多空双开对冲策略
- 同时开多单和空单
- 当一方亏损超过1%时，亏损方止损，盈利方进入移动止盈
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from binance.client import Client

from strategies.base_strategy import BaseStrategy
from utils.exchange_utils import (
    to_binance_symbol, get_mark_price, get_order_book,
    cancel_all_orders, place_market_order, place_limit_order,
    check_order_filled, cancel_order, fetch_and_store_all_instruments,
    instrument_info_dict
)

logger = logging.getLogger(__name__)


class HedgeStrategy(BaseStrategy):
    """多空双开对冲策略"""
    
    def __init__(self, client: Client, config: dict):
        """
        初始化策略
        Args:
            client: 币安客户端
            config: 配置字典
        """
        self.client = client
        self.config = config
        self.leverage_value = config.get('leverage', 10)
        
        # 确保交易对信息已加载
        fetch_and_store_all_instruments(client)
    
    def get_strategy_name(self) -> str:
        """返回策略名称"""
        return "HEDGE"
    
    def fetch_positions(self):
        """获取所有持仓"""
        try:
            positions = self.client.futures_position_information()
            return positions
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []
    
    def count_current_orders(self):
        """统计当前挂单数量（多单和空单）"""
        long_count = 0
        short_count = 0
        try:
            # 获取所有未成交的挂单
            all_orders = self.client.futures_get_open_orders()
            for order in all_orders:
                side = order.get('side', '').upper()
                if side == 'BUY':
                    long_count += 1
                elif side == 'SELL':
                    short_count += 1
        except Exception as e:
            logger.error(f"统计挂单数量失败: {e}")
        return long_count, short_count
    
    def has_hedge_position(self, symbol: str) -> bool:
        """
        检查是否已有对冲持仓或单边持仓（对冲策略相关）
        - 如果同时有多单和空单，返回True（对冲持仓）
        - 如果只有单边持仓（一方已止损，另一方在移动止盈），也返回True（避免重复下单）
        Args:
            symbol: 交易对
        Returns:
            bool: 是否已有对冲持仓或单边持仓
        """
        positions = self.fetch_positions()
        has_long = False
        has_short = False
        
        for pos in positions:
            if pos['symbol'] == symbol:
                position_amt = float(pos['positionAmt'])
                if position_amt > 0:
                    has_long = True
                elif position_amt < 0:
                    has_short = True
        
        # 如果同时有多空，是对冲持仓
        if has_long and has_short:
            return True
        
        # 如果只有单边持仓，说明一方已止损，另一方在移动止盈中，也应该跳过下单
        if has_long or has_short:
            logger.info(f"{symbol} [对冲策略] 检测到单边持仓（一方已止损，另一方在移动止盈中），跳过下单")
            return True
        
        return False
    
    def process_pair(self, instId: str, pair_config: dict) -> dict:
        """
        处理单个交易对：使用限价单并行下多单和空单，降低滑点
        Args:
            instId: 交易对ID
            pair_config: 交易对配置
        Returns:
            dict: 包含订单信息的字典，格式为 {'symbol': 'BTCUSDT', 'order_ids': [long_id, short_id], 'strategy': 'hedge'} 或 None
        """
        try:
            symbol = to_binance_symbol(instId)
            
            # 检查是否已有对冲持仓
            if self.has_hedge_position(symbol):
                logger.info(f"{symbol} [对冲策略] 已有对冲持仓（同时有多单和空单），跳过下单")
                return None

            # 取消该交易对的所有挂单
            cancel_all_orders(self.client, symbol)

            long_amount_usdt = pair_config.get('long_amount_usdt', 20)
            short_amount_usdt = pair_config.get('short_amount_usdt', 20)
            
            # 获取订单簿（买一卖一价格）
            try:
                order_book = get_order_book(self.client, symbol, limit=5)
                bid_price = order_book['bid_price']  # 买一价（最高买价）
                ask_price = order_book['ask_price']  # 卖一价（最低卖价）
                mark_price = (bid_price + ask_price) / 2  # 中间价
                
                logger.info(f"{symbol} [对冲策略] 订单簿：买一价={bid_price:.6f}，卖一价={ask_price:.6f}，中间价={mark_price:.6f}")
            except Exception as e:
                logger.warning(f"{symbol} [对冲策略] 获取订单簿失败，使用标记价格: {e}")
                mark_price = get_mark_price(self.client, symbol)
                bid_price = mark_price * 0.9995  # 估算买一价
                ask_price = mark_price * 1.0005  # 估算卖一价

            # 计算限价单价格
            # 多单：以卖一价 + 1个tick挂单（确保成交）
            # 空单：以买一价 - 1个tick挂单（确保成交）
            # 获取tick_size
            if symbol in instrument_info_dict:
                tick_size = float(instrument_info_dict[symbol].get('tickSz', '0.01'))
            else:
                tick_size = 0.01  # 默认值
            
            # 多单限价：卖一价 + 1个tick（略高于卖一价，确保成交）
            long_limit_price = ask_price + tick_size
            # 空单限价：买一价 - 1个tick（略低于买一价，确保成交）
            short_limit_price = bid_price - tick_size
            
            logger.info(f"{symbol} [对冲策略] 准备使用限价单并行下单：多单限价={long_limit_price:.6f}，空单限价={short_limit_price:.6f}")

            # 并行下限价单
            long_order = None
            short_order = None
            long_order_id = None
            short_order_id = None
            
            def place_long_order():
                """下多单限价单"""
                nonlocal long_order
                try:
                    long_order = place_limit_order(
                        self.client, symbol, long_amount_usdt, 'buy',
                        self.leverage_value, long_limit_price, position_side='LONG'
                    )
                    return long_order
                except Exception as e:
                    logger.error(f"{symbol} [对冲策略] 多单限价单下单异常: {e}")
                    return None
            
            def place_short_order():
                """下空单限价单"""
                nonlocal short_order
                try:
                    short_order = place_limit_order(
                        self.client, symbol, short_amount_usdt, 'sell',
                        self.leverage_value, short_limit_price, position_side='SHORT'
                    )
                    return short_order
                except Exception as e:
                    logger.error(f"{symbol} [对冲策略] 空单限价单下单异常: {e}")
                    return None
            
            # 并行执行两个订单
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {
                    executor.submit(place_long_order): 'long',
                    executor.submit(place_short_order): 'short'
                }
                
                for future in as_completed(futures):
                    side = futures[future]
                    try:
                        order = future.result()
                        if order and order.get('orderId'):
                            if side == 'long':
                                long_order_id = order['orderId']
                                logger.info(f"{symbol} [对冲策略] 多单限价单下单成功，订单ID: {long_order_id}")
                            else:
                                short_order_id = order['orderId']
                                logger.info(f"{symbol} [对冲策略] 空单限价单下单成功，订单ID: {short_order_id}")
                    except Exception as e:
                        logger.error(f"{symbol} [对冲策略] {side}单限价单下单失败: {e}")
            
            # 检查订单是否都成功
            if not long_order_id and not short_order_id:
                logger.warning(f"{symbol} [对冲策略] 多空单限价单均下单失败")
                return None
            
            # 等待限价单成交（最多等待5秒）
            timeout = 5.0
            start_time = time.time()
            long_filled = False
            short_filled = False
            
            while time.time() - start_time < timeout:
                if long_order_id and not long_filled:
                    long_filled = check_order_filled(self.client, symbol, long_order_id)
                    if long_filled:
                        logger.info(f"{symbol} [对冲策略] 多单限价单已成交")
                
                if short_order_id and not short_filled:
                    short_filled = check_order_filled(self.client, symbol, short_order_id)
                    if short_filled:
                        logger.info(f"{symbol} [对冲策略] 空单限价单已成交")
                
                if (long_order_id and long_filled) and (short_order_id and short_filled):
                    # 两个订单都已成交
                    logger.info(f"{symbol} [对冲策略] 多空单限价单均已成交")
                    break
                
                time.sleep(0.2)  # 每200ms检查一次
            
            # 处理未成交的订单：转市价单
            order_ids = []
            if long_order_id:
                if long_filled:
                    order_ids.append(long_order_id)
                else:
                    logger.warning(f"{symbol} [对冲策略] 多单限价单未成交，取消并转市价单")
                    cancel_order(self.client, symbol, long_order_id)
                    # 转市价单
                    market_order = place_market_order(
                        self.client, symbol, long_amount_usdt, 'buy',
                        self.leverage_value, mark_price
                    )
                    if market_order and market_order.get('orderId'):
                        order_ids.append(market_order['orderId'])
                        logger.info(f"{symbol} [对冲策略] 多单已转市价单，订单ID: {market_order['orderId']}")
            
            if short_order_id:
                if short_filled:
                    order_ids.append(short_order_id)
                else:
                    logger.warning(f"{symbol} [对冲策略] 空单限价单未成交，取消并转市价单")
                    cancel_order(self.client, symbol, short_order_id)
                    # 转市价单
                    market_order = place_market_order(
                        self.client, symbol, short_amount_usdt, 'sell',
                        self.leverage_value, mark_price
                    )
                    if market_order and market_order.get('orderId'):
                        order_ids.append(market_order['orderId'])
                        logger.info(f"{symbol} [对冲策略] 空单已转市价单，订单ID: {market_order['orderId']}")
            
            if order_ids:
                logger.info(f"{symbol} [对冲策略] 多空单处理完成，订单IDs: {order_ids}")
                return {
                    'symbol': symbol,
                    'order_ids': order_ids,
                    'strategy': 'hedge',
                    'long_order_id': long_order_id if long_filled else None,
                    'short_order_id': short_order_id if short_filled else None
                }
            else:
                logger.warning(f"{symbol} [对冲策略] 多空单均处理失败")
                return None

        except Exception as e:
            error_message = f'Error processing {instId} in hedge strategy: {e}'
            logger.error(error_message)
            return None

