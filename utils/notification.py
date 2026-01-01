# -*- coding: utf-8 -*-
"""
通知模块
"""
import requests
import logging

logger = logging.getLogger(__name__)


def send_dingtalk_notification(webhook_url, title, content):
    """
    发送钉钉通知（支持Markdown格式）
    Args:
        webhook_url: 钉钉webhook地址
        title: 消息标题
        content: Markdown格式的消息内容
    """
    if not webhook_url:
        return
    
    headers = {'Content-Type': 'application/json'}
    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": content
        }
    }
    try:
        response = requests.post(webhook_url, headers=headers, json=data, timeout=10)
        result = response.json()
        if result.get('errcode') == 0:
            logger.info("钉钉通知发送成功")
        else:
            logger.error(f"钉钉通知发送失败: {result.get('errmsg')}")
    except Exception as e:
        logger.error(f"钉钉通知异常: {e}")

