# -*- coding: utf-8 -*-
"""
数学计算工具函数
"""
import logging
from decimal import Decimal
import pandas as pd

logger = logging.getLogger(__name__)


def calculate_atr(klines, period=60):
    """计算ATR（平均真实波幅）"""
    if not isinstance(klines, list) or len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        try:
            high = Decimal(str(klines[i][2]))
            low = Decimal(str(klines[i][3]))
            prev_close = Decimal(str(klines[i-1][4]))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(float(tr))
        except (IndexError, ValueError) as e:
            logger.error(f"计算ATR时处理K线数据出错: {e}")
            return None
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    return atr if atr > 0 else None


def calculate_ema_pandas(data, period):
    """计算EMA"""
    if not data or len(data) < period:
        raise ValueError(f"数据长度不足，无法计算EMA。需要至少 {period} 个数据点，当前只有 {len(data) if data else 0} 个")
    df = pd.Series(data)
    ema = df.ewm(span=period, adjust=False).mean()
    return ema.iloc[-1]


def calculate_average_amplitude(klines, period=60):
    """计算平均振幅"""
    if not isinstance(klines, list) or len(klines) < period:
        return None
    amplitudes = []
    start_idx = max(0, len(klines) - period)
    for i in range(start_idx, len(klines)):
        try:
            high = float(klines[i][2])
            low = float(klines[i][3])
            close = float(klines[i][4])
            if close == 0:
                continue
            amplitude = ((high - low) / close) * 100
            amplitudes.append(amplitude)
        except (IndexError, ValueError) as e:
            logger.error(f"计算平均振幅时处理K线数据出错: {e}")
            continue
    if len(amplitudes) == 0:
        return None
    average_amplitude = sum(amplitudes) / len(amplitudes)
    return average_amplitude

