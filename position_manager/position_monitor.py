# -*- coding: utf-8 -*-
"""
止盈止损监控独立运行入口
可以单独运行此文件来启动止盈止损监控
"""
import sys
import os

# 添加父目录到路径，以便导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_loader import load_config, get_binance_client
from utils.logger_setup import setup_logger
from position_manager.stop_loss_manager import StopLossManager


def main():
    """主函数：独立运行止盈止损监控"""
    # 加载配置
    config = load_config()
    
    # 设置日志
    logger = setup_logger()
    
    # 创建币安客户端
    client = get_binance_client(config)
    
    # 创建止盈止损管理器
    manager = StopLossManager(client, config)
    
    # 启动监控
    try:
        manager.start_monitoring(monitor_interval=1.5)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止监控...")
        manager.stop()
        logger.info("监控已停止")


if __name__ == '__main__':
    main()

