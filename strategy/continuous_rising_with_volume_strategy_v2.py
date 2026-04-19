"""
连阳回调策略
核心逻辑：
1. 在选股日前3-4天寻找倍量长阳线（关键日）
2. 检查关键日前是否有连续阳线（3-4天）
3. 检查关键日后是否有缩量调整（2-3天）
4. 缩量调整期间不跌破关键日的开盘价（支撑位）
"""
from strategy.base_strategy import BaseStrategy
import pandas as pd
import numpy as np


class ContinuousRisingWithVolumeStrategyV2(BaseStrategy):
    """
    连阳回调策略

    核心逻辑：
    1. 在选股日前3-4天寻找倍量长阳线（关键日）
    2. 检查关键日前是否有连续阳线（3-4天）
    3. 检查关键日后是否有缩量调整（2-3天）
    4. 缩量调整期间不跌破关键日的开盘价（支撑位）
    """

    def __init__(self, params=None):
        """
        初始化策略
        :param params: 策略参数
        """
        super().__init__("连阳回调策略", params)
        # 从配置文件加载参数
        self.min_consecutive_阳 = self.params.get('min_consecutive_阳', 3)  # 最小连续阳线天数
        self.max_consecutive_阳 = self.params.get('max_consecutive_阳', 5)  # 最大连续阳线天数
        self.volume_multiplier = self.params.get('volume_multiplier', 2.0)  # 倍量阈值（2倍）
        self.key_day_rise_min = self.params.get('key_day_rise_min', 0.04)  # 倍量阳线最小涨幅（4%）
        self.max_adjust_days = self.params.get('max_adjust_days', 4)  # 缩量调整最大天数
        self.min_adjust_days = self.params.get('min_adjust_days', 2)  # 缩量调整最小天数
        self.key_day_offset_min = 3  # 关键日距今最小天数
        self.key_day_offset_max = 4  # 关键日距今最大天数

    def calculate_indicators(self, df):
        """
        计算技术指标
        :param df: 股票K线数据
        :return: 计算后的K线数据
        """
        # 检查数据是否包含必要的列
        required_columns = ['open', 'close', 'volume', 'low']
        for col in required_columns:
            if col not in df.columns:
                # 如果缺少必要的列，返回空DataFrame
                print(f'数据缺少必要的列: {col}')
                return df.head(0)  # 返回空DataFrame

        # 处理None值
        for col in required_columns:
            if df[col].isnull().any():
                # 如果有None值，返回空DataFrame
                print(f'数据包含None值: {col}')
                return df.head(0)  # 返回空DataFrame

        # 确保数据按日期降序排列
        df = df.sort_values('date', ascending=False).reset_index(drop=True)

        # 计算是否收阳
        df['is_阳线'] = df['close'] > df['open']

        # 计算涨幅
        df['涨幅'] = (df['close'] - df['open']) / df['open']

        # 计算前五日平均成交量
        # 方法：先按升序排列计算，再按降序排列回来
        # 创建一个临时的升序数据框用于计算
        df_asc = df.iloc[::-1].copy().reset_index(drop=True)
        # 计算前5天的均量（shift(1)表示向后移动1行，即不包括当前行）
        df_asc['前五日平均成交量'] = df_asc['volume'].shift(1).rolling(
            window=5, min_periods=1
        ).mean()
        # 反转回降序，并将结果赋值回原DataFrame
        df['前五日平均成交量'] = df_asc['前五日平均成交量'].iloc[::-1].values

        # 计算是否倍量阳线（需要同时满足涨幅要求）
        df['is_倍量阳线'] = (
            df['is_阳线'] &
            (df['volume'] > df['前五日平均成交量'] * self.volume_multiplier) &
            (df['涨幅'] >= self.key_day_rise_min)
        )

        return df

    def select_stocks(self, df, stock_name=''):
        """
        选股逻辑
        :param df: 股票K线数据（按日期降序排列，最新在前）
        :param stock_name: 股票名称
        :return: 选股信号
        """
        # 确保数据足够
        if len(df) < self.key_day_offset_max + self.max_adjust_days + 5:
            return []

        # 计算选股日（今天）的索引 - 数据按日期降序，所以今天是第一行（iloc[0]）
        today_idx = 0

        # 第一步：检查3-4天前是否有倍量阳线
        # 降序数据中，3天前是iloc[3]，4天前是iloc[4]
        for key_day_offset in [self.key_day_offset_min, self.key_day_offset_max]:
            key_day_idx = today_idx + key_day_offset

            if key_day_idx >= len(df):
                continue

            # 检查是否是倍量阳线
            if not df.iloc[key_day_idx]['is_倍量阳线']:
                continue

            # 【条件1】检查倍量阳线前是否有连续阳线（3-5天）
            # 在降序数据中，向"后"是更小的索引（今天方向），向"前"是更大的索引（更老的日期）
            
            # 找到连续阳线的起始位置（向"后"检查，即今天方向，更小的索引）
            start_idx = key_day_idx
            for i in range(1, 10):  # 最多检查10天
                check_idx = key_day_idx - i
                if check_idx >= 0 and df.iloc[check_idx]['is_阳线']:
                    start_idx = check_idx
                else:
                    break

            # 找到连续阳线的结束位置（向"前"检查，即更老的日期，更大的索引）
            end_idx = key_day_idx
            for i in range(1, 10):  # 最多检查10天
                check_idx = key_day_idx + i
                if check_idx < len(df) and df.iloc[check_idx]['is_阳线']:
                    end_idx = check_idx
                else:
                    break

            # 计算连续阳线总数（包括倍量阳线本身）
            total_consecutive_阳 = end_idx - start_idx + 1

            # 检查是否满足连续阳线要求（3-5天）
            if not (3 <= total_consecutive_阳 <= 5):
                continue

            # 【条件2】检查倍量阳线后是否有连续缩量K线（2-3天）
            # 获取倍量阳线的成交量
            key_day_volume = df.iloc[key_day_idx]['volume']

            # 检查倍量阳线后的缩量情况
            valid_shrink_days = 0
            for shrink_day_offset in range(1, self.max_adjust_days + 1):
                # 缩量日索引（向今天方向，更小的索引）
                shrink_day_idx = key_day_idx - shrink_day_offset

                # 检查缩量日是否存在
                if shrink_day_idx < 0:
                    break

                # 检查是否缩量（成交量小于倍量阳线）
                shrink_volume = df.iloc[shrink_day_idx]['volume']
                if shrink_volume >= key_day_volume:
                    break

                valid_shrink_days += 1
            
            # 检查是否满足连续缩量天数要求（至少2天）
            if valid_shrink_days >= self.min_adjust_days:
                # 找到倍量阳线的日期
                key_date = df.iloc[key_day_idx]['date']
                if hasattr(key_date, 'strftime'):
                    key_date_str = key_date.strftime('%Y-%m-%d')
                else:
                    key_date_str = str(key_date)[:10]
                
                # 返回选股信号
                signal_info = {
                    'key_date': key_date_str,
                    'key_date_type': '倍量阳线',
                    'reasons': [f'倍量阳线前有{total_consecutive_阳}天连阳', f'倍量后有{valid_shrink_days}天缩量']
                }
                return [signal_info]
        
        # 没有找到符合条件的股票，返回空列表
        return []
