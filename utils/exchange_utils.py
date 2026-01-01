# -*- coding: utf-8 -*-
"""
交易所相关工具函数
"""
import logging
import time
import hmac
import hashlib
import urllib.parse
import requests
from decimal import Decimal, ROUND_DOWN, getcontext
from binance.client import Client

# 设置Decimal精度
getcontext().prec = 10

logger = logging.getLogger(__name__)

# 全局变量：交易对信息字典
instrument_info_dict = {}


def to_binance_symbol(instId: str) -> str:
    """将类似 OKX 的 'CTC-USDT-SWAP' 转为 'CTCUSDT'"""
    parts = instId.split('-')
    if len(parts) >= 2:
        return (parts[0] + parts[1]).upper()
    return instId.replace('-', '').replace('_', '').upper()


def fetch_and_store_all_instruments(client: Client):
    """获取并存储所有交易对信息"""
    try:
        info = client.futures_exchange_info()
        instrument_info_dict.clear()
        for s in info.get('symbols', []):
            symbol = s['symbol']
            tick_size = None
            step_size = None
            for f in s.get('filters', []):
                if f.get('filterType') == 'PRICE_FILTER':
                    tick_size = f.get('tickSize')
                if f.get('filterType') == 'LOT_SIZE':
                    step_size = f.get('stepSize')
            instrument_info_dict[symbol] = {
                'symbol': symbol,
                'tickSz': tick_size,
                'stepSz': step_size
            }
        logger.info(f"Fetched {len(instrument_info_dict)} binance futures symbols")
    except Exception as e:
        logger.error(f"Error fetching exchange info: {e}")
        raise


def get_mark_price(client: Client, symbol: str):
    """获取标记价格"""
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = ticker.get('price')
        if price is None:
            raise ValueError(f"无法获取 {symbol} 的标记价格")
        return float(price)
    except Exception as e:
        logger.error(f"获取标记价格失败 {symbol}: {e}")
        raise


def get_order_book(client: Client, symbol: str, limit: int = 5):
    """
    获取订单簿（买一卖一价格）
    Args:
        client: 币安客户端
        symbol: 交易对
        limit: 返回的深度数量，默认5
    Returns:
        dict: {'bid_price': float, 'ask_price': float, 'bid_qty': float, 'ask_qty': float}
    """
    try:
        order_book = client.futures_order_book(symbol=symbol, limit=limit)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        if not bids or not asks:
            raise ValueError(f"无法获取 {symbol} 的订单簿")
        
        # 买一价（最高买价）
        bid_price = float(bids[0][0])
        bid_qty = float(bids[0][1])
        
        # 卖一价（最低卖价）
        ask_price = float(asks[0][0])
        ask_qty = float(asks[0][1])
        
        return {
            'bid_price': bid_price,
            'ask_price': ask_price,
            'bid_qty': bid_qty,
            'ask_qty': ask_qty
        }
    except Exception as e:
        logger.error(f"获取订单簿失败 {symbol}: {e}")
        raise


def get_account_balance(client: Client) -> float:
    """
    获取账户USDT余额
    Args:
        client: 币安客户端
    Returns:
        float: USDT余额
    """
    try:
        account = client.futures_account()
        assets = account.get('assets', [])
        for asset in assets:
            if asset.get('asset') == 'USDT':
                balance = float(asset.get('walletBalance', 0))
                logger.debug(f"账户USDT余额: {balance}")
                return balance
        logger.warning("未找到USDT资产")
        return 0.0
    except Exception as e:
        logger.error(f"获取账户余额失败: {e}")
        return 0.0


def round_price_to_tick(price, tick_size):
    """价格精度调整"""
    if tick_size is None:
        return str(price)
    p = Decimal(str(price))
    t = Decimal(str(tick_size))
    q = (p / t).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    adjusted = (q * t).quantize(t)
    return format(adjusted, 'f')


def round_quantity_to_step(quantity, step_size):
    """数量精度调整"""
    if step_size is None:
        return float(quantity)
    q = Decimal(str(quantity))
    s = Decimal(str(step_size))
    q_adj = (q / s).quantize(Decimal('1.'), rounding=ROUND_DOWN) * s
    return format(q_adj.normalize(), 'f')


def get_historical_klines(client: Client, symbol: str, bar='1m', limit=241):
    """获取历史K线数据"""
    resp = client.futures_klines(symbol=symbol, interval=bar, limit=limit)
    if not resp:
        raise ValueError('No klines')
    return resp


def cancel_all_orders(client: Client, symbol: str):
    """取消所有挂单"""
    try:
        open_orders = client.futures_get_open_orders(symbol=symbol)
        for o in open_orders:
            client.futures_cancel_order(symbol=symbol, orderId=o.get('orderId'))
        logger.info(f"{symbol} 挂单取消成功")
    except Exception as e:
        logger.error(f"Cancel orders error for {symbol}: {e}")


def cancel_stop_order(client: Client, symbol: str, order_id: int):
    """取消止损订单"""
    try:
        client.futures_cancel_order(symbol=symbol, orderId=order_id)
        logger.info(f"{symbol} 止损订单 {order_id} 取消成功")
        return True
    except Exception as e:
        logger.error(f"取消止损订单失败 {symbol} {order_id}: {e}")
        return False


def create_stop_loss_order(client: Client, symbol: str, entry_price: float, 
                           position_amount: float, side: str, stop_loss_pct: float,
                           position_side: str = None):
    """
    创建交易所止损订单（使用python-binance的futures_create_algo_order）
    
    重要：USDT-M合约的止损单必须使用 futures_create_algo_order，不能用 futures_create_order
    
    Args:
        client: 币安客户端
        symbol: 交易对
        entry_price: 开仓价格
        position_amount: 持仓数量（用于计算止损价格，实际使用closePosition自动平仓）
        side: 方向 ('long' 或 'short')
        stop_loss_pct: 止损百分比（正数，如1.2表示1.2%）
        position_side: 持仓方向 ('LONG' 或 'SHORT')，用于双向持仓模式，如果为None则使用单向模式
    Returns:
        订单ID（clientAlgoId），失败返回None
    
    最佳实践（USDT-M合约Algo Order）：
    - 使用 futures_create_algo_order（必须）
    - 使用 algoType='STOP_MARKET'（止损市价单，这是USDT-M Algo Order的正确值）
    - 使用 closePosition=True（自动平仓，必须提供，不能缺少）
    - 使用 workingType='MARK_PRICE'（使用标记价格触发，防止插针，推荐）
    - 使用 timeInForce='GTC'（一直有效直到取消）
    - 不能带 price 参数（市价止损不需要price）
    
    重要：USDT-M合约Algo Order的合法algoType是'STOP_MARKET'，不是'STOP'
    """
    try:
        if symbol not in instrument_info_dict:
            logger.error(f"Instrument {symbol} not found")
            return None
        
        tick_size = instrument_info_dict[symbol].get('tickSz')
        
        # 计算止损价格
        if side == 'long':
            stop_price = entry_price * (1 - stop_loss_pct / 100)
            stop_side = 'SELL'  # 多单止损 = 卖出
            default_position_side = 'LONG'
        else:  # short
            stop_price = entry_price * (1 + stop_loss_pct / 100)
            stop_side = 'BUY'  # 空单止损 = 买入
            default_position_side = 'SHORT'
        
        # 如果没有指定position_side，使用默认值
        if position_side is None:
            position_side = default_position_side
        
        # 调整价格精度
        adjusted_stop_price = round_price_to_tick(stop_price, tick_size)
        
        # 使用 futures_create_algo_order 创建止损单（正确的方法）
        # 重要：USDT-M合约Algo Order的合法algoType是'STOP_MARKET'，不是'STOP'
        # 必须同时满足以下条件，否则会返回-4500错误：
        # 1. algoType='STOP_MARKET'
        # 2. stopPrice（触发价格）
        # 3. closePosition=True 或 quantity（二选一，必须提供）
        # 4. 不能带 price 参数（市价止损）
        # 5. 推荐加上 workingType='MARK_PRICE'（使用标记价格触发）
        algo_order_params = {
            'symbol': symbol,
            'side': stop_side,
            'algoType': 'STOP_MARKET',  # 算法订单类型：止损市价单（USDT-M Algo Order的正确值）
            'stopPrice': float(adjusted_stop_price),  # 触发价格（必须是float类型）
            'closePosition': True,  # 自动平仓（必须提供，布尔值True）
            'workingType': 'MARK_PRICE',  # 使用标记价格触发，防止插针（推荐）
            'timeInForce': 'GTC'  # 一直有效直到取消（字符串'GTC'）
        }
        
        # 确保algoType是字符串类型，且值完全正确
        assert isinstance(algo_order_params['algoType'], str), "algoType必须是字符串类型"
        assert algo_order_params['algoType'] == 'STOP_MARKET', f"algoType必须是'STOP_MARKET'（USDT-M Algo Order），当前值：{algo_order_params['algoType']}"
        assert algo_order_params.get('closePosition') is True, "closePosition必须为True（USDT-M Algo Order要求）"
        
        # 双向持仓模式下添加positionSide
        if position_side:
            algo_order_params['positionSide'] = position_side
        
        # 调试：打印参数（用于排查-4500错误）
        logger.debug(
            f"{symbol} 创建止损单参数: "
            f"algoType={algo_order_params['algoType']}, "
            f"stopPrice={algo_order_params['stopPrice']}, "
            f"closePosition={algo_order_params['closePosition']}, "
            f"workingType={algo_order_params.get('workingType', 'None')}, "
            f"positionSide={algo_order_params.get('positionSide', 'None')}"
        )
        
        try:
            # 使用 futures_create_algo_order（关键！）
            algo_order = client.futures_create_algo_order(**algo_order_params)
            order_id = algo_order.get('clientAlgoId') or algo_order.get('orderId')
            
            if order_id:
                logger.info(
                    f"{symbol} 止损订单创建成功（futures_create_algo_order），"
                    f"订单ID：{order_id}，止损价格：{adjusted_stop_price}，"
                    f"止损比例：{stop_loss_pct}%，持仓方向：{position_side or '单向'}"
                )
                return order_id
            else:
                logger.warning(f"{symbol} 算法订单创建成功但未返回订单ID: {algo_order}")
                return None
                
        except Exception as e:
            error_str = str(e)
            
            # 如果是-4500错误（Invalid algoType），记录详细信息
            if '-4500' in error_str or 'Invalid algoType' in error_str:
                logger.error(
                    f"{symbol} 止损订单创建失败：Invalid algoType (-4500)。"
                    f"当前参数：algoType='{algo_order_params['algoType']}' (类型: {type(algo_order_params['algoType']).__name__})，"
                    f"请确保algoType的值是字符串'STOP_MARKET'（大小写完全一致）。"
                    f"完整错误：{e}"
                )
                return None
            
            # 如果双向持仓模式失败，尝试使用单向模式（不使用positionSide）
            if position_side and ('positionSide' in error_str or 'hedge' in error_str.lower()):
                logger.warning(f"{symbol} 双向持仓止损单创建失败，尝试使用单向模式: {e}")
                try:
                    algo_order_params.pop('positionSide', None)
                    logger.debug(f"{symbol} 单向模式参数: {algo_order_params}")
                    algo_order = client.futures_create_algo_order(**algo_order_params)
                    order_id = algo_order.get('clientAlgoId') or algo_order.get('orderId')
                    
                    if order_id:
                        logger.info(
                            f"{symbol} 止损订单创建成功（单向模式），订单ID：{order_id}，"
                            f"止损价格：{adjusted_stop_price}，止损比例：{stop_loss_pct}%"
                        )
                        return order_id
                    else:
                        logger.warning(f"{symbol} 单向模式算法订单创建成功但未返回订单ID: {algo_order}")
                        return None
                except Exception as e2:
                    error_str2 = str(e2)
                    if '-4500' in error_str2 or 'Invalid algoType' in error_str2:
                        logger.error(
                            f"{symbol} 单向模式也失败：Invalid algoType (-4500)。"
                            f"参数：algoType='{algo_order_params['algoType']}'，"
                            f"错误：{e2}"
                        )
                    else:
                        logger.error(f"{symbol} 单向模式止损单创建也失败: {e2}")
                    return None
            else:
                logger.error(f"{symbol} 止损订单创建失败: {e}")
                return None
                
    except Exception as e:
        logger.error(f"创建止损订单失败 {symbol}: {e}")
        return None


def set_leverage(client: Client, symbol: str, leverage: int):
    """设置杠杆"""
    try:
        client.futures_change_leverage(symbol=symbol, leverage=int(leverage))
        logger.info(f"Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        logger.error(f"Error setting leverage for {symbol}: {e}")


def place_order(client: Client, symbol: str, price: float, amount_usdt: float, 
                side: str, leverage: int):
    """
    下单（限价单）
    Args:
        client: 币安客户端
        symbol: 交易对
        price: 价格
        amount_usdt: 金额（USDT）
        side: 方向 ('buy' 或 'sell')
        leverage: 杠杆倍数
    Returns:
        订单对象，失败返回None
    """
    if symbol not in instrument_info_dict:
        logger.error(f"Instrument {symbol} not found")
        return None
    
    tick_size = instrument_info_dict[symbol].get('tickSz')
    step_size = instrument_info_dict[symbol].get('stepSz')

    adjusted_price = Decimal(round_price_to_tick(price, tick_size))

    # 计算仓位数量：quantity = (amount_usdt * leverage) / price
    qty = (Decimal(str(amount_usdt)) * Decimal(str(leverage))) / adjusted_price
    qty_str = round_quantity_to_step(qty, step_size)

    if float(qty_str) <= 0:
        logger.info(f"{symbol} 计算出的合约数量太小，无法下单。")
        return None

    # 检查名义价值，确保 >= 100 USDT
    notional_value = float(qty_str) * float(adjusted_price)
    min_notional = 100.0
    
    if notional_value < min_notional:
        # 计算需要的最小数量
        min_qty = Decimal(str(min_notional)) / adjusted_price
        qty_str = round_quantity_to_step(min_qty, step_size)
        notional_value = float(qty_str) * float(adjusted_price)
        
        if notional_value < min_notional:
            logger.warning(f"{symbol} 即使调整后名义价值仍不足100 USDT: {notional_value:.2f}，跳过下单")
            return None
        else:
            logger.info(f"{symbol} 名义价值不足，已自动调整数量: {qty_str}，名义价值: {notional_value:.2f} USDT")

    try:
        set_leverage(client, symbol, leverage)
        side_str = 'BUY' if side == 'buy' else 'SELL'
        order = client.futures_create_order(
            symbol=symbol,
            side=side_str,
            type='LIMIT',
            timeInForce='GTC',
            quantity=qty_str,
            price=str(adjusted_price),
            reduceOnly=False
        )
        logger.info(f"Order placed: {order}")
        return order
    except Exception as e:
        logger.error(f"Place order error for {symbol}: {e}")
        return None


def place_limit_order(client: Client, symbol: str, amount_usdt: float,
                      side: str, leverage: int, limit_price: float,
                      position_side: str = None):
    """
    下限价单（支持双向持仓模式）
    Args:
        client: 币安客户端
        symbol: 交易对
        amount_usdt: 金额（USDT）
        side: 方向 ('buy' 或 'sell')
        leverage: 杠杆倍数
        limit_price: 限价
        position_side: 持仓方向 ('LONG' 或 'SHORT')，用于双向持仓模式
    Returns:
        订单对象，失败返回None
    """
    if symbol not in instrument_info_dict:
        logger.error(f"Instrument {symbol} not found")
        return None
    
    tick_size = instrument_info_dict[symbol].get('tickSz')
    step_size = instrument_info_dict[symbol].get('stepSz')
    
    # 调整价格精度
    adjusted_price = Decimal(round_price_to_tick(limit_price, tick_size))
    
    # 计算仓位数量：quantity = (amount_usdt * leverage) / limit_price
    qty = (Decimal(str(amount_usdt)) * Decimal(str(leverage))) / adjusted_price
    qty_str = round_quantity_to_step(qty, step_size)
    
    if float(qty_str) <= 0:
        logger.info(f"{symbol} 计算出的合约数量太小，无法下单。")
        return None
    
    # 检查名义价值，确保 >= 100 USDT
    notional_value = float(qty_str) * float(adjusted_price)
    min_notional = 100.0
    
    if notional_value < min_notional:
        # 计算需要的最小数量
        min_qty = Decimal(str(min_notional)) / adjusted_price
        qty_str = round_quantity_to_step(min_qty, step_size)
        notional_value = float(qty_str) * float(adjusted_price)
        
        if notional_value < min_notional:
            logger.warning(f"{symbol} 即使调整后名义价值仍不足100 USDT: {notional_value:.2f}，跳过下单")
            return None
        else:
            logger.info(f"{symbol} 名义价值不足，已自动调整数量: {qty_str}，名义价值: {notional_value:.2f} USDT")
    
    try:
        set_leverage(client, symbol, leverage)
        side_str = 'BUY' if side == 'buy' else 'SELL'
        
        # 构建订单参数
        order_params = {
            'symbol': symbol,
            'side': side_str,
            'type': 'LIMIT',
            'timeInForce': 'GTC',
            'quantity': qty_str,
            'price': str(adjusted_price)
        }
        
        # 如果指定了position_side，添加到订单参数中（双向持仓模式）
        # 注意：双向持仓模式下，开仓时不能使用reduceOnly参数
        if position_side:
            order_params['positionSide'] = position_side
        else:
            # 单向模式下，开仓时也不使用reduceOnly（默认为False）
            pass
        
        try:
            order = client.futures_create_order(**order_params)
        except Exception as e:
            # 如果双向持仓模式失败，尝试使用单向模式
            if position_side and ('positionSide' in str(e) or 'hedge' in str(e).lower() or 'reduceonly' in str(e).lower()):
                logger.warning(f"{symbol} 双向持仓限价单创建失败，尝试使用单向模式: {e}")
                order_params.pop('positionSide', None)
                # 单向模式下，开仓时也不使用reduceOnly参数
                order = client.futures_create_order(**order_params)
            else:
                raise
        
        logger.info(f"{symbol} 限价单创建成功，订单ID：{order.get('orderId')}，价格：{adjusted_price}，数量：{qty_str}，持仓方向：{position_side or '单向'}")
        return order
    except Exception as e:
        logger.error(f"创建限价单失败 {symbol}: {e}")
        return None


def place_market_order(client: Client, symbol: str, amount_usdt: float, 
                       side: str, leverage: int, mark_price: float = None):
    """
    下市价单
    Args:
        client: 币安客户端
        symbol: 交易对
        amount_usdt: 金额（USDT）
        side: 方向 ('buy' 或 'sell')
        leverage: 杠杆倍数
        mark_price: 标记价格（可选，如果不提供则自动获取）
    Returns:
        订单对象，失败返回None
    """
    if symbol not in instrument_info_dict:
        logger.error(f"Instrument {symbol} not found")
        return None
    
    step_size = instrument_info_dict[symbol].get('stepSz')
    
    # 获取当前标记价格用于计算数量（如果未提供）
    if mark_price is None:
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            mark_price = float(ticker.get('price', 0))
            if mark_price == 0:
                logger.error(f"无法获取 {symbol} 的标记价格")
                return None
        except Exception as e:
            logger.error(f"获取标记价格失败 {symbol}: {e}")
            return None

    # 计算仓位数量：quantity = (amount_usdt * leverage) / mark_price
    qty = (Decimal(str(amount_usdt)) * Decimal(str(leverage))) / Decimal(str(mark_price))
    qty_str = round_quantity_to_step(qty, step_size)

    if float(qty_str) <= 0:
        logger.info(f"{symbol} 计算出的合约数量太小，无法下单。")
        return None

    # 检查名义价值，确保 >= 100 USDT
    notional_value = float(qty_str) * mark_price
    min_notional = 100.0
    
    if notional_value < min_notional:
        # 计算需要的最小数量
        min_qty = Decimal(str(min_notional)) / Decimal(str(mark_price))
        qty_str = round_quantity_to_step(min_qty, step_size)
        notional_value = float(qty_str) * mark_price
        
        if notional_value < min_notional:
            logger.warning(f"{symbol} 即使调整后名义价值仍不足100 USDT: {notional_value:.2f}，跳过下单")
            return None
        else:
            logger.info(f"{symbol} 名义价值不足，已自动调整数量: {qty_str}，名义价值: {notional_value:.2f} USDT")

    try:
        set_leverage(client, symbol, leverage)
        side_str = 'BUY' if side == 'buy' else 'SELL'
        # 使用双向持仓模式：多单使用LONG，空单使用SHORT，这样才能同时持有多单和空单
        # 注意：双向持仓模式下，开仓时不能使用reduceOnly参数
        position_side = 'LONG' if side == 'buy' else 'SHORT'
        order = client.futures_create_order(
            symbol=symbol,
            side=side_str,
            type='MARKET',
            quantity=qty_str,
            positionSide=position_side  # 双向持仓模式，不需要reduceOnly参数
        )
        logger.info(f"Market order placed: {side_str} {symbol} quantity={qty_str} positionSide={position_side} at price={mark_price:.6f}, notional={notional_value:.2f} USDT")
        return order
    except Exception as e:
        logger.error(f"Place market order error for {symbol}: {e}")
        # 如果双向持仓模式失败，可能是账户未开启双向持仓，尝试使用单向模式
        if 'positionSide' in str(e) or 'hedge' in str(e).lower() or 'reduceonly' in str(e).lower():
            logger.warning(f"{symbol} 双向持仓模式失败，尝试使用单向模式（可能导致多空抵消）")
            try:
                order = client.futures_create_order(
                    symbol=symbol,
                    side=side_str,
                    type='MARKET',
                    quantity=qty_str,
                    reduceOnly=False
                )
                logger.info(f"Market order placed (单向模式): {side_str} {symbol} quantity={qty_str} at price={mark_price:.6f}, notional={notional_value:.2f} USDT")
                return order
            except Exception as e2:
                logger.error(f"Place market order error (单向模式) for {symbol}: {e2}")
        return None


def check_order_filled(client: Client, symbol: str, order_id: int):
    """检查订单是否成交"""
    try:
        order = client.futures_get_order(symbol=symbol, orderId=order_id)
        status = order.get('status')
        return status == 'FILLED'
    except Exception as e:
        logger.error(f"检查订单状态失败 {symbol}: {e}")
        return False


def cancel_order(client: Client, symbol: str, order_id: int):
    """
    取消订单
    Args:
        client: 币安客户端
        symbol: 交易对
        order_id: 订单ID
    Returns:
        bool: 是否成功
    """
    try:
        client.futures_cancel_order(symbol=symbol, orderId=order_id)
        logger.info(f"{symbol} 订单 {order_id} 取消成功")
        return True
    except Exception as e:
        logger.error(f"取消订单失败 {symbol} {order_id}: {e}")
        return False

