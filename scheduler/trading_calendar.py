# -*- coding: utf-8 -*-
"""
交易日历检测模块

薄封装层，核心逻辑委托给 utils/trade_date_utils.py。
提供三级降级：Tushare API → 周末排除。
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)


class TradingCalendar:
    """
    A股交易日历检测

    复用 utils/trade_date_utils 中已有的交易日判断和获取逻辑：
      - is_trading_day: Tushare trade_cal 接口（精确）
      - get_trading_days: 按日期区间批量获取交易日列表
      - 降级方案: 周末排除
    """

    @staticmethod
    def is_trading_day(d: Optional[date] = None) -> bool:
        """
        判断指定日期是否为 A 股交易日

        参数:
            d: 日期对象，默认当天
        返回:
            bool: 是否为交易日
        """
        from utils.trade_date_utils import is_trading_day as _is_trading_day
        target = d or date.today()
        date_str = target.strftime("%Y-%m-%d")
        return _is_trading_day(date_str)

    @staticmethod
    def get_next_trading_day(from_date: Optional[date] = None) -> date:
        """
        获取下一个交易日

        从指定日期起向后查找，跳过非交易日（周末/节假日）。

        参数:
            from_date: 起始日期，默认明天
        返回:
            date: 下一个交易日
        """
        current = (from_date or date.today()) + timedelta(days=1)
        # 最多查找 30 天防止无限循环
        for _ in range(30):
            if TradingCalendar.is_trading_day(current):
                return current
            current += timedelta(days=1)
        # 兜底返回明天
        logger.warning("未找到下一个交易日，回退到明天")
        return (from_date or date.today()) + timedelta(days=1)
