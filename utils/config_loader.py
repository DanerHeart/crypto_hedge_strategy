# -*- coding: utf-8 -*-
"""
配置加载模块
"""
import json
import os
from binance.client import Client


def load_config(config_path=None):
    """
    加载配置文件
    Args:
        config_path: 配置文件路径，如果为None则使用默认路径
    Returns:
        dict: 配置字典
    """
    if config_path is None:
        # 获取utils目录的父目录（项目根目录）
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, 'config.json')
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    return config


def get_binance_client(config):
    """
    创建并返回币安客户端
    Args:
        config: 配置字典
    Returns:
        Client: 币安客户端对象
    """
    binance_config = config.get('binance', {})
    api_key = binance_config.get('apiKey', '')
    api_secret = binance_config.get('secret', '')
    use_testnet = binance_config.get('testnet', False)
    
    client = Client(api_key, api_secret)
    if use_testnet:
        client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
    
    return client

