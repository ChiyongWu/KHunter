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
            Dict: 包含 plan_date, hunting_date, total_count, plans, temperature_info
        """
        # 获取温度信息
        temperature_info = self._get_market_temperature(hunting_date)
        
        # 获取下一交易日和狩猎结果
        plan_date = self._get_next_trading_date(hunting_date)
        hunting_results = self._get_hunting_results(hunting_date)
        
        # 获取温度约束建议（仅供参考，不限制数据）
        temp_constraints = self._get_temp_constraints(temperature_info)
        
        # 生成交易计划（不改变数据，只添加建议信息）
        plans = []
        for stock_data in hunting_results:
            plan = self._generate_plan_for_stock(stock_data, plan_date, hunting_date)
            plans.append(plan)
        
        return {
            'plan_date': plan_date,
            'hunting_date': hunting_date,
            'total_count': len(plans),
            'plans': plans,
            'temperature_info': temperature_info,
            'temp_constraints': temp_constraints  # 温度约束建议（仅供参考）
        }

    def _get_market_temperature(self, hunting_date: str) -> Dict[str, Any]:
        """
        获取市场温度信息

        参数：
            hunting_date: 狩猎日期

        返回：
            Dict: 温度信息字典
        """
        try:
            # 转换日期格式为 YYYYMMDD
            trade_date = hunting_date.replace('-', '')
            
            # 导入市场温度计算器
            from utils.market_temperature import MarketTemperature
            calculator = MarketTemperature()
            
            # 计算温度
            temp_data = calculator.calculate(trade_date, use_cache=True)
            
            return {
                'temperature': temp_data.get('temperature'),
                'status': temp_data.get('status'),
                'position_ratio': temp_data.get('position_ratio'),
                'action': temp_data.get('action'),
                'suggestion': self._generate_temperature_suggestion(temp_data)
            }
        except Exception as e:
            logger.warning(f"获取市场温度失败: {e}")
            return {
                'temperature': None,
                'status': '未知',
                'position_ratio': 1.0,
                'action': '正常执行',
                'suggestion': '市场温度数据获取失败，请手动判断'
            }

    def _generate_temperature_suggestion(self, temp_data: Dict) -> str:
        """
        生成温度建议文本

        参数：
            temp_data: 温度数据

        返回：
            str: 建议文本
        """
        temp = temp_data.get('temperature')
        status = temp_data.get('status', '未知')
        position_ratio = temp_data.get('position_ratio', 1.0)
        
        if temp is None:
            return '市场温度数据获取失败，建议谨慎操作'
        
        # 根据温度状态生成具体建议
        suggestions = []
        
        # 仓位建议
        position_pct = int(position_ratio * 100)
        if temp >= 80:
            suggestions.append(f'当前市场活跃({temp}°)，建议仓位{position_pct}%')
            suggestions.append('可同时持有3-5只股票')
        elif temp >= 65:
            suggestions.append(f'当前市场正常({temp}°)，建议仓位{position_pct}%')
            suggestions.append('建议持有2-3只股票')
        elif temp >= 50:
            suggestions.append(f'当前市场偏冷({temp}°)，建议仓位{position_pct}%')
            suggestions.append('建议仅持有1-2只最强股票')
        elif temp >= 30:
            suggestions.append(f'当前市场寒冷({temp}°)，建议仓位{position_pct}%')
            suggestions.append('建议仅持有1只最强股票，轻仓试探')
        elif temp >= 15:
            suggestions.append(f'当前市场冰封({temp}°)，建议仓位{position_pct}%')
            suggestions.append('建议观望为主，极轻仓试探')
        else:
            suggestions.append(f'当前市场极端({temp}°)，建议暂停买入')
            suggestions.append('耐心等待市场回暖')
        
        return '；'.join(suggestions)

    def _get_temp_constraints(self, temperature_info: Dict) -> tuple:
        """
        根据温度信息获取交易约束

        参数：
            temperature_info: 温度信息

        返回：
            tuple: (最大股票数量, 调整后的仓位系数)
        """
        temp = temperature_info.get('temperature')
        if temp is None:
            return (999, 1.0)  # 无限制
        
        if temp >= 80:
            return (5, 1.0)
        elif temp >= 65:
            return (3, 0.8)
        elif temp >= 50:
            return (2, 0.5)
        elif temp >= 30:
            return (1, 0.25)
        elif temp >= 15:
            return (1, 0.1)
        else:
            return (0, 0.0)  # 禁止买入

    def _get_next_trading_date(self, hunting_date: str) -> str:
        """
        获取下一交易日日期（简单实现：狩猎日期+1天）

        参数：
            hunting_date: 狩猎日期

        返回：
            str: 下一交易日日期，格式 YYYY-M-D
        """
        hunting_dt = datetime.strptime(hunting_date, '%Y-%m-%d')
        next_dt = hunting_dt + timedelta(days=1)
        # 使用不带前导零的格式
        return f"{next_dt.year}-{next_dt.month}-{next_dt.day}"

    def _get_hunting_results(self, hunting_date: str) -> List[Dict[str, Any]]:
        """
        获取狩猎场筛选结果

        参数：
            hunting_date: 狩猎日期

        返回：
            List[Dict]: 狩猎结果列表
        """
        result = self.khunter_dao.query_by_date(hunting_date)
        results = result.get('results', [])
        
        # 添加排名信息
        for idx, item in enumerate(results, 1):
            item['rank'] = idx
        
        return results

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
            'remark': '',
            'rank': stock_data.get('rank', 0)
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