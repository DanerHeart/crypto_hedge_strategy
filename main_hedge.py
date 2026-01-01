# -*- coding: utf-8 -*-
"""
对冲策略交易机器人 - 主程序
- 多空同时双开
- 当一方亏损超过1%时，亏损方止损，盈利方进入移动止盈
"""
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.config_loader import load_config, get_binance_client
from utils.logger_setup import setup_logger
from utils.exchange_utils import fetch_and_store_all_instruments, check_order_filled
from position_manager.hedge_stop_loss_manager import HedgeStopLossManager
from strategies.hedge_strategy import HedgeStrategy

logger = logging.getLogger(__name__)


class HedgeTradingBot:
    """对冲策略交易机器人主类"""
    
    def __init__(self, config_path=None):
        """
        初始化交易机器人
        Args:
            config_path: 配置文件路径，如果为None则使用默认路径
        """
        # 加载配置
        self.config = load_config(config_path)
        
        # 创建币安客户端
        self.client = get_binance_client(self.config)
        
        # 创建对冲策略止盈止损管理器
        self.stop_loss_manager = HedgeStopLossManager(self.client, self.config)
        
        # 创建对冲策略实例
        self.strategy = HedgeStrategy(self.client, self.config)
        
        # 交易对配置
        self.trading_pairs_config = self.config.get('tradingPairs', {})
        self.monitor_interval = self.config.get('monitor_interval', 60)
        
        # 状态变量
        self.running = True
        self.pending_orders = {}  # 待检查的订单 {symbol: [order_id1, order_id2, ...]}
        self.order_lock = threading.Lock()  # 挂单锁，防止并发竞态条件
    
    def check_pending_orders(self):
        """
        检查待成交订单
        注意：市价单通常会立即成交，此检查主要用于确认订单状态
        """
        if not self.pending_orders:
            return
        
        for symbol, order_info in list(self.pending_orders.items()):
            remaining_order_ids = []
            all_filled = True
            
            # 处理对冲策略返回的多个订单ID
            order_ids = order_info if isinstance(order_info, list) else [order_info]
            
            for order_id in order_ids:
                if check_order_filled(self.client, symbol, order_id):
                    logger.info(f"[对冲策略] {symbol} 订单 {order_id} 已成交")
                else:
                    remaining_order_ids.append(order_id)
                    all_filled = False
            
            # 更新待检查订单列表
            if remaining_order_ids:
                self.pending_orders[symbol] = remaining_order_ids
            else:
                # 所有订单都已成交或取消，移除该交易对
                self.pending_orders.pop(symbol, None)
                logger.info(f"[对冲策略] {symbol} 所有订单已成交，开始监控对冲持仓")
                
    
    def run_order_placement_loop(self):
        """挂单循环"""
        fetch_and_store_all_instruments(self.client)
        inst_ids = list(self.trading_pairs_config.keys())
        batch_size = 5
        
        while self.running:
            try:
                if not self.running:
                    break
                for i in range(0, len(inst_ids), batch_size):
                    if not self.running:
                        break
                    batch = inst_ids[i:i + batch_size]
                    with ThreadPoolExecutor(max_workers=batch_size) as executor:
                        futures = [executor.submit(self._process_pair_with_lock, instId, self.trading_pairs_config[instId]) 
                                  for instId in batch]
                        for future in as_completed(futures):
                            if not self.running:
                                break
                            future.result()
                if not self.running:
                    break
                time.sleep(self.monitor_interval)
            except Exception as e:
                logger.error(f"[对冲策略] 挂单循环异常: {e}")
                if not self.running:
                    break
                time.sleep(self.monitor_interval)
    
    def _process_pair_with_lock(self, instId: str, pair_config: dict):
        """
        使用锁保护的处理交易对函数
        Args:
            instId: 交易对ID
            pair_config: 交易对配置
        """
        with self.order_lock:
            result = self.strategy.process_pair(instId, pair_config)
            if result and result.get('order_ids'):
                symbol = result['symbol']
                order_ids = result['order_ids']
                # 存储所有订单ID
                self.pending_orders[symbol] = order_ids
                logger.info(f"[对冲策略] {symbol} 已挂多空订单，订单IDs: {order_ids}")
    
    def run_position_monitor_loop(self):
        """持仓监控循环"""
        while self.running:
            try:
                if not self.running:
                    break
                
                self.check_pending_orders()  # 检查待成交订单
                if not self.running:
                    break
                self.stop_loss_manager.monitor_positions()  # 监控对冲持仓
                if not self.running:
                    break
                
                # 如果止盈止损管理器停止了，也停止主程序
                if not self.stop_loss_manager.running:
                    self.running = False
                    break
                
                time.sleep(0.15)  # 每300毫秒检查一次持仓
            except Exception as e:
                logger.error(f"[对冲策略] 持仓监控循环异常: {e}")
                if not self.running:
                    break
                time.sleep(0.15)
    
    def start(self):
        """启动机器人"""
        logger.info("启动对冲策略交易机器人...")
        
        # 启动挂单线程
        order_thread = threading.Thread(target=self.run_order_placement_loop, daemon=True)
        order_thread.start()
        
        # 启动持仓监控线程
        monitor_thread = threading.Thread(target=self.run_position_monitor_loop, daemon=True)
        monitor_thread.start()
        
        logger.info("所有线程已启动，对冲策略机器人运行中...")
        
        # 主线程保持运行
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭机器人...")
            self.stop()
            logger.info("机器人已停止")
    
    def stop(self):
        """停止机器人"""
        self.running = False
        self.stop_loss_manager.stop()


if __name__ == '__main__':
    # 设置日志
    setup_logger()
    
    # 创建并启动对冲策略机器人
    bot = HedgeTradingBot()
    bot.start()

