# -*- coding: utf-8 -*-
"""
日志配置模块
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler


class ColoredFormatter(logging.Formatter):
    """自定义Formatter，在文件输出时移除ANSI颜色代码"""
    
    def format(self, record):
        # 获取原始消息
        message = super().format(record)
        # 移除ANSI转义序列（用于文件输出）
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', message)


def setup_logger(log_file_path=None, logger_name=None):
    """
    设置日志
    Args:
        log_file_path: 日志文件路径，如果为None则使用默认路径
        logger_name: logger名称，如果为None则使用root logger
    Returns:
        logging.Logger: 配置好的logger对象
    """
    if log_file_path is None:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        log_file_path = os.path.join(base_dir, "log", "integrated_bot.log")
    
    # 确保日志目录存在
    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    
    # 清除已有的handler，避免重复
    logger.handlers.clear()
    
    # 文件handler
    file_handler = TimedRotatingFileHandler(
        log_file_path, when='midnight', interval=1, backupCount=7, encoding='utf-8'
    )
    file_handler.suffix = "%Y-%m-%d"
    file_formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # 控制台handler
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    return logger

