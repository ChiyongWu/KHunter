#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易计划生成器模块
负责根据狩猎场筛选结果生成交易计划
"""

import logging
from typing import Dict, List, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class TradingPlanGenerator:
    """
    交易计划生成器
    根据狩猎场筛选结果生成交易计划
    """

    DEFAULT_POSITION_RATIO = 5
    DEFAULT_HOLD_DAYS = 10
    STOP_LOSS_PERCENT = 0.05
    TAKE_PROFIT_PERCENT = 0.20

    def __init__(self, db_manager, khunter_dao, trading_plan_dao):
        self.db_manager = db_manager
        self.khunter_dao = khunter_dao
        self.trading_plan_dao = trading_plan_dao

    def generate(self, hunting_date: str) -> Dict[str, Any]:
        """
        生成交易计划

        参数：
            hunting_date: 狩猎日期，格式 YYYY-MM-DD

        返回：
            Dict: 包含 plan_date, hunting_date, total_count, plans
        """
        plan_date = self._get_next_trading_date(hunting_date)
        hunting_results = self._get_hunting_results(hunting_date)
        plans = []
        for stock_data in hunting_results:
            plan = self._generate_plan_for_stock(stock_data, plan_date, hunting_date)
            plans.append(plan)
        self._save_plans(plans)
        return {
            'plan_date': plan_date,
            'hunting_date': hunting_date,
            'total_count': len(plans),
            'plans': plans
        }

    def _get_next_trading_date(self, hunting_date: str) -> str:
        """
        获取下一交易日日期（简单实现：狩猎日期+1天）

        参数：
            hunting_date: 狩猎日期

        返回：
            str: 下一交易日日期
        """
        hunting_dt = datetime.strptime(hunting_date, '%Y-%m-%d')
        next_dt = hunting_dt + timedelta(days=1)
        return next_dt.strftime('%Y-%m-%d')

    def _get_hunting_results(self, hunting_date: str) -> List[Dict[str, Any]]:
        """
        获取狩猎场筛选结果

        参数：
            hunting_date: 狩猎日期

        返回：
            List[Dict]: 狩猎结果列表
        """
        result = self.khunter_dao.query_by_date(hunting_date)
        return result.get('results', [])

    def _generate_plan_for_stock(
        self,
        stock_data: Dict[str, Any],
        plan_date: str,
        hunting_date: str
    ) -> Dict[str, Any]:
        """
        为单只股票生成交易计划

        参数：
            stock_data: 股票数据
            plan_date: 计划日期
            hunting_date: 狩猎日期

        返回：
            Dict: 交易计划
        """
        support_level = float(stock_data.get('support_level', 0))
        buy_plan = self._calculate_buy_plan(support_level)
        return {
            'plan_date': plan_date,
            'hunting_date': hunting_date,
            'stock_code': stock_data.get('stock_code', ''),
            'stock_name': stock_data.get('stock_name', ''),
            'buy_lower_price': buy_plan['buy_lower_price'],
            'buy_upper_price': buy_plan['buy_upper_price'],
            'position_ratio': self.DEFAULT_POSITION_RATIO,
            'support_level': support_level,
            'stop_loss_price': self._calculate_stop_loss(support_level),
            'take_profit_price': self._calculate_take_profit(support_level),
            'hold_days': self.DEFAULT_HOLD_DAYS,
            'remark': ''
        }

    def _calculate_buy_plan(self, support_level: float) -> Dict[str, float]:
        """
        计算买入计划

        参数：
            support_level: 支撑位价格

        返回：
            Dict: 包含 buy_lower_price, buy_upper_price
        """
        return {
            'buy_lower_price': round(support_level * 0.99, 2),
            'buy_upper_price': round(support_level * 1.03, 2)
        }

    def _calculate_stop_loss(self, support_level: float) -> float:
        """
        计算止损价格

        参数：
            support_level: 支撑位价格

        返回：
            float: 止损价格
        """
        return round(support_level * (1 - self.STOP_LOSS_PERCENT), 2)

    def _calculate_take_profit(self, support_level: float) -> float:
        """
        计算止盈价格

        参数：
            support_level: 支撑位价格

        返回：
            float: 止盈价格
        """
        return round(support_level * (1 + self.TAKE_PROFIT_PERCENT), 2)

    def _save_plans(self, plans: List[Dict[str, Any]]) -> int:
        """
        保存交易计划到数据库

        参数：
            plans: 交易计划列表

        返回：
            int: 保存成功的记录数
        """
        if not plans:
            return 0
        return self.trading_plan_dao.save_batch_plans(plans)