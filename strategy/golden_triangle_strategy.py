"""
金三角策略 - 均线金叉三角形形态选股策略

形态定义：
- A点：5日均线上穿10日均线
- B点：5日均线上穿20日均线
- C点：10日均线上穿20日均线
- A、B、C三点形成三角形

特殊形态：
- 金蜘蛛：A、B、C三点汇聚于同一天（ac_interval=0）
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from strategy.base_strategy import BaseStrategy


class GoldenTriangleStrategy(BaseStrategy):
    """金三角策略"""

    def __init__(self, params=None):
        default_params = {
            'short_period': 5,
            'mid_period': 10,
            'long_period': 20,
            'super_long_period': 60,
            'ac_interval': 3,
            'c_cross_min_gain': 0.03,
            'c_cross_min_volume_ratio': 1.3,
            'lookback_days': 30,
            'strategy_weight': 50,
        }
        if params:
            default_params.update(params)
        super().__init__("GoldenTriangleStrategy", default_params)

    def calculate_indicators(self, df) -> pd.DataFrame:
        """计算技术指标"""
        result = df.copy()
        if len(result) > 1 and str(result['date'].iloc[0]) > str(result['date'].iloc[1]):
            result = result.iloc[::-1].reset_index(drop=True)

        short_period = int(self.params['short_period'])
        mid_period = int(self.params['mid_period'])
        long_period = int(self.params['long_period'])
        super_long_period = int(self.params['super_long_period'])

        result['sma_short'] = result['close'].rolling(window=short_period).mean()
        result['sma_mid'] = result['close'].rolling(window=mid_period).mean()
        result['sma_long'] = result['close'].rolling(window=long_period).mean()
        result['sma_super_long'] = result['close'].rolling(window=super_long_period).mean()
        result['volume_ma5'] = result['volume'].rolling(window=5).mean()

        result['prev_close'] = result['close'].shift(1)
        result['gain'] = (result['close'] - result['prev_close']) / result['prev_close']

        result = result.ffill().bfill()
        result = result.iloc[::-1].reset_index(drop=True)
        return result

    def get_selection_criteria(self):
        """获取选股条件描述"""
        return [
            f"1. A点形成：{self.params['short_period']}日均线上穿{self.params['mid_period']}日均线",
            f"2. B点形成：{self.params['short_period']}日均线上穿{self.params['long_period']}日均线",
            f"3. C点形成：{self.params['mid_period']}日均线上穿{self.params['long_period']}日均线",
            f"4. C点涨幅 >= {self.params['c_cross_min_gain']*100}%",
            f"5. C点量能比 >= {self.params['c_cross_min_volume_ratio']}",
            f"6. A-C间隔 <= {self.params['ac_interval']}天",
            f"7. 均线多头排列：MA{self.params['short_period']} >= MA{self.params['mid_period']} >= MA{self.params['long_period']} > MA{self.params['super_long_period']}",
        ]

    def quick_filter(self, df):
        """快速过滤：检查数据是否足够"""
        if df is None or df.empty:
            return False
        if len(df) < max(int(self.params['super_long_period']), 60):
            return False
        return True

    def select_stocks(self, df, stock_name='') -> list:
        """选股逻辑"""
        if df.empty or len(df) < 60:
            return []

        if stock_name and not self._validate_stock_name(stock_name):
            return []

        df = self.calculate_indicators(df.copy())

        latest_idx = 0
        latest = df.iloc[latest_idx]

        cross_points = self._find_cross_points(df, latest_idx)
        if cross_points is None:
            return []

        a_date, b_date, c_date, a_idx, b_idx, c_idx = cross_points

        ac_interval_days = (datetime.strptime(c_date, '%Y-%m-%d') -
                           datetime.strptime(a_date, '%Y-%m-%d')).days
        if ac_interval_days > int(self.params['ac_interval']):
            return []

        c_day_data = df.iloc[c_idx]
        c_gain = c_day_data['gain'] if not pd.isna(c_day_data['gain']) else 0
        if c_gain < float(self.params['c_cross_min_gain']):
            return []

        c_volume_ratio = c_day_data['volume'] / c_day_data['volume_ma5'] \
            if c_day_data['volume_ma5'] > 0 else 0
        if c_volume_ratio < float(self.params['c_cross_min_volume_ratio']):
            return []

        sma_short = latest['sma_short']
        sma_mid = latest['sma_mid']
        sma_long = latest['sma_long']
        sma_super_long = latest['sma_super_long']

        if not (sma_short >= sma_mid >= sma_long > sma_super_long):
            return []

        triangle_type = 'golden_spider' if ac_interval_days == 0 else 'golden_triangle'

        return [{
            'signal': 'buy',
            'reason': f'金三角形态({triangle_type})',
            'date': latest['date'],
            'close': latest['close'],
            'stock_code': '',
            'stock_name': stock_name,
            'triangle_type': triangle_type,
            'cross_details': {
                'a_cross_date': a_date,
                'b_cross_date': b_date,
                'c_cross_date': c_date,
                'c_cross_gain': c_gain,
                'c_cross_volume_ratio': c_volume_ratio,
                'ac_interval_days': ac_interval_days,
            },
            'ma_details': {
                'sma_short': sma_short,
                'sma_mid': sma_mid,
                'sma_long': sma_long,
                'sma_super_long': sma_super_long,
            },
            'pattern_confirmed': True,
            'strategy_weight': self.params['strategy_weight'],
        }]

    def _find_cross_points(self, df, latest_idx) -> Optional[Tuple]:
        """查找A、B、C三个金叉点

        逻辑：今天（index=0）必须满足条件
        - C点：MA10 >= MA20
        - A点（向前查找ac_interval天）：MA5 >= MA10
        - B点（向前查找ac_interval天）：MA5 >= MA20
        """
        ac_interval = int(self.params['ac_interval'])

        # 需要至少有前一天的数据来检查C点
        if latest_idx + 1 >= len(df):
            return None

        curr = df.iloc[latest_idx]
        prev = df.iloc[latest_idx + 1]

        sma_mid_curr = curr['sma_mid']
        sma_long_curr = curr['sma_long']
        sma_mid_prev = prev['sma_mid']
        sma_long_prev = prev['sma_long']

        is_c_point = sma_mid_prev <= sma_long_prev and sma_mid_curr >= sma_long_curr
        if not is_c_point:
            return None

        c_idx = latest_idx

        a_idx, b_idx = None, None

        for i in range(latest_idx + 1, min(latest_idx + ac_interval + 1, len(df))):
            if i + 1 >= len(df):
                continue

            curr = df.iloc[i]
            prev = df.iloc[i + 1]

            sma_short_curr = curr['sma_short']
            sma_mid_curr = curr['sma_mid']
            sma_long_curr = curr['sma_long']

            sma_short_prev = prev['sma_short']
            sma_mid_prev = prev['sma_mid']
            sma_long_prev = prev['sma_long']

            if a_idx is None:
                if sma_short_prev <= sma_mid_prev and sma_short_curr >= sma_mid_curr:
                    a_idx = i

            if b_idx is None:
                if sma_short_prev <= sma_long_prev and sma_short_curr >= sma_long_curr:
                    b_idx = i

        if a_idx is None or b_idx is None:
            return None

        return (
            str(df.iloc[a_idx]['date']).split()[0],
            str(df.iloc[b_idx]['date']).split()[0],
            str(df.iloc[c_idx]['date']).split()[0],
            a_idx, b_idx, c_idx
        )
