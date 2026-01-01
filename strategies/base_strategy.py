# -*- coding: utf-8 -*-
"""
策略基类
"""
from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """策略基类"""
    
    @abstractmethod
    def process_pair(self, instId: str, pair_config: dict):
        """
        处理单个交易对
        Args:
            instId: 交易对ID（如 'BTC-USDT-SWAP'）
            pair_config: 交易对配置
        Returns:
            dict: 包含订单信息的字典，格式为 {'symbol': 'BTCUSDT', 'order_id': 123} 或 None
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """
        返回策略名称
        Returns:
            str: 策略名称
        """
        pass

