"""
连阳回调策略 V2
核心逻辑：
1. 在选股日前3-4天寻找倍量阳线（关键日），且收盘价>MA5
2. 检查关键日前是否有连续阳线（≥3天）
3. 检查趋势：均线多头排列（MA5>MA10>MA20）
4. 检查关键日后是否有缩量调整（≥3天）
5. 缩量调整期间回调不破5日线
"""
from strategy.base_strategy import BaseStrategy
from utils.technical import calculate_daily_return, MA
import pandas as pd
import numpy as np


class ContinuousRisingWithVolumeStrategyV2(BaseStrategy):
    """
    连阳回调策略

    核心逻辑：
    1. 在选股日前3-4天寻找倍量阳线（关键日），且收盘价>MA5
    2. 检查关键日前是否有连续阳线（≥3天）
    3. 检查趋势：均线多头排列（MA5>MA10>MA20）
    4. 检查关键日后是否有缩量调整（≥3天）
    5. 缩量调整期间回调不破5日线
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
        self.volume_multiplier = self.params.get('volume_multiplier', 2.0)  # 倍量阈值（当日成交量/前5日均量）
        self.key_day_rise_min = self.params.get('key_day_rise_min', 0.05)  # 倍量阳线最小涨幅（5%）
        self.max_adjust_days = self.params.get('max_adjust_days', 4)  # 缩量调整最大天数
        self.min_adjust_days = self.params.get('min_adjust_days', 3)  # 缩量调整最小天数
        self.key_day_offset_min = self.params.get('key_day_offset_min', 3)  # 关键日距今最小天数
        self.key_day_offset_max = self.params.get('key_day_offset_max', 4)  # 关键日距今最大天数
        self.body_ratio_min = self.params.get('body_ratio_min', 0.01)  # 阳线最小实体比例
        self.max_rally_pct = self.params.get('max_rally_pct', 0.25)  # 连续阳线期间最大累计涨幅
        self.ma_period = self.params.get('ma_period', 5)  # 均线周期（用于收盘站线判断）
        # 趋势过滤参数
        self.trend_lookback_days = self.params.get('trend_lookback_days', 20)  # 趋势判断天数
        self.trend_r_squared_threshold = self.params.get('trend_r_squared_threshold', 0.3)  # 趋势R²阈值
        self.enable_ma_filter = self.params.get('enable_ma_filter', True)  # 是否启用均线多头过滤

    def _calculate_trend_indicators(self, df):
        """
        计算趋势指标（线性回归）
        :param df: 股票K线数据（升序）
        :return: R²值
        """
        lookback = min(self.trend_lookback_days, len(df))
        if lookback < 5:
            return 0

        close_prices = df['close'].values[-lookback:]
        x = np.arange(lookback)
        y = close_prices

        # 线性回归
        if len(x) < 2:
            return 0

        x_mean = np.mean(x)
        y_mean = np.mean(y)
        numerator = np.sum((x - x_mean) * (y - y_mean))
        denominator = np.sum((x - x_mean) ** 2)

        if denominator == 0:
            return 0

        slope = numerator / denominator
        y_pred = y_mean + slope * (x - x_mean)

        # 计算R²
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - y_mean) ** 2)

        if ss_tot == 0:
            return 0

        r_squared = 1 - (ss_res / ss_tot)
        return r_squared

    def calculate_indicators(self, df):
        """
        计算技术指标
        :param df: 股票K线数据（倒序，最新在前）
        :return: 计算后的K线数据（恢复原始倒序）
        """
        # 检查数据是否包含必要的列
        required_columns = ['open', 'close', 'volume', 'low']
        for col in required_columns:
            if col not in df.columns:
                print(f'数据缺少必要的列: {col}')
                return df.head(0)

        # 处理None值
        for col in required_columns:
            if df[col].isnull().any():
                print(f'数据包含None值: {col}')
                return df.head(0)

        # 检测数据顺序
        try:
            is_descending = df['date'].iloc[0] > df['date'].iloc[-1]
        except (IndexError, KeyError):
            is_descending = False

        # 统一转换为升序计算（从早到晚）
        if is_descending:
            df_calc = df.iloc[::-1].copy().reset_index(drop=True)
        else:
            df_calc = df.copy().reset_index(drop=True)

        # 计算是否收阳（收盘价 > 开盘价）
        df_calc['is_阳线'] = df_calc['close'] > df_calc['open']

        # 计算阳线实体比例（收盘-开盘）/开盘价
        df_calc['实体比例'] = (df_calc['close'] - df_calc['open']) / df_calc['open']

        # 计算涨幅（相对于前一天收盘价）
        # 注意：数据已按升序排列，pct_change()会计算相对于前一行的变化
        df_calc['涨幅'] = df_calc['close'].pct_change()
        
        # 对于第一行（最老的数据），涨幅为NaN，需要填充为0
        df_calc['涨幅'] = df_calc['涨幅'].fillna(0)

        # 计算前五日平均成交量（不包括当日）
        # 先shift(1)排除当日，再rolling计算前5日平均
        df_calc['前五日平均成交量'] = df_calc['volume'].shift(1).rolling(window=5, min_periods=1).mean()

        # 计算MA均线（5、10、20日）
        # 注意：数据已按升序排列，直接使用rolling计算
        df_calc['ma5'] = df_calc['close'].rolling(window=5, min_periods=1).mean()
        df_calc['ma10'] = df_calc['close'].rolling(window=10, min_periods=1).mean()
        df_calc['ma20'] = df_calc['close'].rolling(window=20, min_periods=1).mean()

        # 计算均线多头排列（MA5 > MA10 > MA20）
        df_calc['均线多头'] = (df_calc['ma5'] > df_calc['ma10']) & (df_calc['ma10'] > df_calc['ma20'])

        # 计算收盘是否站上MA5
        df_calc['站上MA5'] = df_calc['close'] > df_calc['ma5']

        # 计算是否倍量阳线（需要同时满足：收阳、倍量、涨幅要求）
        df_calc['is_倍量阳线'] = (
            df_calc['is_阳线'] &
            (df_calc['volume'] > df_calc['前五日平均成交量'] * self.volume_multiplier) &
            (df_calc['涨幅'] >= self.key_day_rise_min)
        )

        # 恢复原始顺序（如果原始数据是倒序的）
        if is_descending:
            result = df_calc.iloc[::-1].reset_index(drop=True)
        else:
            result = df_calc

        result.index = df.index
        return result

    def quick_filter(self, df):
        """
        快速过滤：检查距今3-4天的位置是否有倍量阳线
        
        参考涨停回马枪策略的做法，快速过滤只做初步筛选
        
        注意：数据是倒序的（最新在前），需要正确处理

        :param df: 股票数据DataFrame（倒序，最新在前）
        :return: True表示通过快速过滤，False表示未通过
        """
        # 数据需要至少包含：关键日距今最大天数 + 最大调整天数 + 5天缓冲
        required_days = self.key_day_offset_max + self.max_adjust_days + 5
        if len(df) < required_days:
            return False

        # 快速过滤：检查距今3-4天的位置是否有倍量阳线
        # 对于倒序数据（最新在前）：
        # - index=0 是今天
        # - index=1 是昨天
        # - index=3 是距今3天
        # - index=4 是距今4天
        # 
        # 快速过滤的目的是初步筛选，排除完全没有倍量阳线的股票
        # 详细的条件检查（连续阳线、缩量等）在select_stocks中进行
        
        for offset in range(self.key_day_offset_min, self.key_day_offset_max + 1):
            key_day_idx = offset
            
            if key_day_idx < 0 or key_day_idx >= len(df):
                continue
            
            # 需要有前一天的数据
            if key_day_idx + 1 >= len(df):
                continue

            key_day = df.iloc[key_day_idx]
            prev_day = df.iloc[key_day_idx + 1]

            # 检查是否收阳线
            if key_day['close'] <= key_day['open']:
                continue

            # 检查涨幅是否足够
            prev_close = prev_day['close']
            if prev_close > 0:
                rise_ratio = (key_day['close'] - prev_close) / prev_close
                if rise_ratio < self.key_day_rise_min:
                    continue
            else:
                continue
            
            # 检查是否倍量（成交量 > 前5日均量 × 倍量阈值）
            # 计算前5日平均成交量（不包括当日）
            # 注意：数据是倒序的，所以索引越大日期越早
            start_idx = key_day_idx + 1
            end_idx = min(key_day_idx + 6, len(df))
            
            if start_idx < end_idx:
                avg_volume = df.iloc[start_idx:end_idx]['volume'].mean()
                if avg_volume > 0 and key_day['volume'] > avg_volume * self.volume_multiplier:
                    return True

        return False

    def select_stocks(self, df, stock_name=''):
        """
        选股逻辑
        :param df: 股票K线数据（倒序，最新在前）
        :param stock_name: 股票名称
        :return: 选股信号
        """
        # 确保数据足够
        required_days = self.key_day_offset_max + self.max_adjust_days + 5
        if len(df) < required_days:
            return []

        # 第一步：快速过滤 - 检查前3-4天内是否有足够涨幅的阳线
        if not self.quick_filter(df):
            return []

        # 第二步：计算指标（会自动处理数据顺序，最后恢复原始倒序）
        df = self.calculate_indicators(df)

        # 第三步：遍历可能的关键日（距今3-4天范围内）
        # 注意：数据是倒序的（最新在前）
        # - index=0 是今天（最新）
        # - index=1 是昨天
        # - index=3 是距今3天
        # - index=4 是距今4天

        for key_day_offset in range(self.key_day_offset_min, self.key_day_offset_max + 1):
            # 关键日索引（倒序数据中的距今N天）
            key_day_idx = key_day_offset

            if key_day_idx < 0 or key_day_idx >= len(df):
                continue

            # 检查关键日是否满足条件
            key_day = df.iloc[key_day_idx]

            # 【条件1】关键日必须是倍量阳线
            if not key_day.get('is_倍量阳线', False):
                continue

            # 【条件2】关键日收盘价必须站上MA5
            if not key_day.get('站上MA5', False):
                continue

            # 【条件3】趋势过滤：均线多头排列（强制条件）
            # 注意：均线多头是强制条件，必须满足
            if not key_day.get('均线多头', False):
                continue

            # 【条件4】检查连续阳线：包含关键日在内，总共≥3天连续阳线
            # 注意：数据是倒序（最新在前），索引越大日期越早
            # 需要双向检查：向前（更早日期）和向后（更近日期）
            
            # 向前（更早日期）检查连续阳线
            before_count = 0
            for i in range(1, self.max_consecutive_阳 + 1):
                check_idx = key_day_idx + i
                if check_idx < len(df) and df.iloc[check_idx].get('is_阳线', False):
                    before_count += 1
                else:
                    break

            # 向后（更近日期）检查连续阳线
            after_count = 0
            for i in range(1, self.max_consecutive_阳 + 1):
                check_idx = key_day_idx - i
                if check_idx >= 0 and df.iloc[check_idx].get('is_阳线', False):
                    after_count += 1
                else:
                    break

            # 计算包含关键日的连续阳线总天数
            consecutive_阳_days = before_count + 1 + after_count  # 关键日本身算1天

            # 检查连续阳线天数是否满足要求（≥3天）
            if consecutive_阳_days < self.min_consecutive_阳:
                continue

            # 【条件5】检查连续阳线期间累计涨幅不超过阈值
            # 从最早的阳线到最新的阳线计算涨幅
            # 注意：倒序数据中，索引越大日期越早
            # 最早的阳线位置
            start_idx = key_day_idx + before_count
            # 最新的阳线位置
            end_idx = key_day_idx - after_count
            start_price = df.iloc[start_idx]['close']  # 最早阳线的收盘价
            end_price = df.iloc[end_idx]['close']  # 最新阳线的收盘价
            rally_pct = (end_price - start_price) / start_price

            if rally_pct > self.max_rally_pct:
                continue

            # 【条件6】检查关键日后缩量调整
            # 获取关键日的成交量
            key_day_volume = key_day['volume']

            # 在关键日后检查缩量调整
            # 注意：数据是倒序（最新在前），所以索引越小日期越近
            # key_day_idx 是距今3-4天的位置
            # key_day_idx - shrink_offset 检查的是更近的日期（索引更小=日期更近）
            # 
            # 逻辑：在关键日后的max_adjust_days天内，找到连续的缩量天数
            # 不要求从哪一天开始，只要关键日之后有连续的缩量即可
            # 缩量期间不允许跌破MA10
            
            valid_shrink_days = 0
            found_shrink_sequence = False

            # 遍历关键日后的每一天，寻找缩量序列
            for start_offset in range(1, self.max_adjust_days + 1):
                start_idx = key_day_idx - start_offset
                
                if start_idx < 0:
                    break
                
                # 从这一天开始，检查是否有连续的缩量
                temp_shrink_days = 0
                
                for shrink_offset in range(start_offset, self.max_adjust_days + 1):
                    shrink_day_idx = key_day_idx - shrink_offset
                    
                    if shrink_day_idx < 0:
                        break
                    
                    shrink_day = df.iloc[shrink_day_idx]
                    
                    # 检查是否缩量（成交量小于关键日）
                    if shrink_day['volume'] >= key_day_volume:
                        break
                    
                    # 检查是否跌破MA10（不允许跌破）
                    if shrink_day['close'] < shrink_day['ma10']:
                        break
                    
                    temp_shrink_days += 1
                
                # 如果找到足够的缩量天数，记录下来
                if temp_shrink_days >= self.min_adjust_days:
                    valid_shrink_days = temp_shrink_days
                    found_shrink_sequence = True
                    break

            # 检查是否满足缩量调整天数要求（≥3天）
            if not found_shrink_sequence:
                continue

            # 所有条件都满足，返回选股信号
            key_date = key_day['date']
            if hasattr(key_date, 'strftime'):
                key_date_str = key_date.strftime('%Y-%m-%d')
            else:
                key_date_str = str(key_date)[:10]

            signal_info = {
                'key_date': key_date_str,
                'key_date_type': '倍量阳线',
                'consecutive_阳_days': consecutive_阳_days,
                'rally_pct': round(rally_pct * 100, 2),
                'shrink_days': valid_shrink_days,
                'ma10': key_day['ma10'],
                'reasons': [
                    f'连续{consecutive_阳_days}天阳线（涨幅{rally_pct*100:.1f}%）',
                    f'关键日后{valid_shrink_days}天缩量调整',
                    f'均线多头排列（MA5>MA10>MA20）',
                    f'调整期间回调不破MA10'
                ]
            }
            return [signal_info]

        # 没有找到符合条件的股票
        return []

    def get_selection_criteria(self):
        """
        获取选股条件描述
        :return: 选股条件描述列表
        """
        criteria = []

        # 条件1：关键日倍量阳线
        volume_multiplier = self.params.get('volume_multiplier', 2.0)
        key_day_rise_min = self.params.get('key_day_rise_min', 0.05) * 100
        criteria.append(
            f"1. 关键日倍量阳线：距今{self.key_day_offset_min}-{self.key_day_offset_max}天的倍量阳线 "
            f"（涨幅≥{key_day_rise_min:.0f}%，成交量≥前5日均量的{volume_multiplier:.1f}倍，收盘价>MA5）"
        )

        # 条件2：连续阳线
        criteria.append(
            f"2. 连续阳线：包含关键日在内，关键日之前连续阳线≥{self.min_consecutive_阳}天"
        )

        # 条件3：均线多头排列
        criteria.append(
            f"3. 均线多头排列：MA5 > MA10 > MA20（上升趋势确认）"
        )

        # 条件4：缩量调整
        criteria.append(
            f"4. 缩量调整：关键日后≥{self.min_adjust_days}天缩量调整（成交量<关键日），"
            f"调整期间回调不破MA10"
        )

        return criteria
