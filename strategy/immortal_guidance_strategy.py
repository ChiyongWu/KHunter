"""
仙人指路策略 (ImmortalGuidanceStrategy)

基于经典技术分析形态的量化选股策略。
通过识别"冲高回落+长上影+放量+趋势向上"形态筛选股票。

核心流程：
1. T日上影线日识别（冲高6%+长上影3%+放量1.5-3倍+收阳线+站5日线）
2. T日趋势过滤（均线多头MA5>MA10>MA20+上升趋势+R²≥0.5）
3. T+1~T+3日确认（回调不破5日线+反包确认）

关键属性：
- key_day: T日（上影线日），仙人指路形态形成的第一天
- support_level: 关键日开盘价，用于支撑位策略买点判断、股票池去除条件判断
- strategy_weight: 70分，技术面评分累加

修改历史：
- 2024-01-新增lookback_days参数，支持追溯最近N个交易日内的信号
- 2024-01-修复反包确认索引计算错误问题
- 2024-01-新增确认后持续性检查，确保信号日后股价持续维持在MA5之上
"""
import pandas as pd
import numpy as np
from strategy.base_strategy import BaseStrategy


class ImmortalGuidanceStrategy(BaseStrategy):
    """
    仙人指路策略类

    继承 BaseStrategy，实现 calculate_indicators() 和 select_stocks() 方法。
    通过三个核心步骤实现选股：
    1. T日上影线日识别（冲高+长上影+放量+收阳线+站线）
    2. T日趋势过滤（均线多头+上升趋势+趋势强度）
    3. T+1~T+3日确认（回调支撑+反包确认+后续持续性检查）

    支持lookback_days参数，可在最近N个交易日内追溯寻找已确认的仙人指路信号。
    """

    def __init__(self, params=None):
        """
        初始化仙人指路策略

        :param params: 用户自定义参数字典，会覆盖默认参数
        """
        default_params = {
            'surge_threshold': 0.06,
            'upper_shadow_ratio': 0.03,
            'volume_ratio_min': 1.5,
            'volume_ratio_max': 3.0,
            'ma_periods': [5, 10, 20],
            'trend_lookback_days': 20,
            'trend_r_squared_threshold': 0.5,
            'anti_body_window': 3,
            'anti_body_ratio': 0.50,
            'strategy_weight': 70,
            'lookback_days': 6,
        }

        if params:
            default_params.update(params)

        super().__init__("仙人指路策略", default_params)

    def calculate_indicators(self, df) -> pd.DataFrame:
        """
        计算技术指标（均线、趋势线）

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :return: 添加了指标列的DataFrame
        """
        if df is None or df.empty:
            return df

        result = df.copy()

        if all(col in result.columns for col in ['ma5', 'ma10', 'ma20', 'volume_ma5']):
            return result

        close_series = result['close'].iloc[::-1]
        volume_series = result['volume'].iloc[::-1]

        ma_periods = self.params['ma_periods']
        for period in ma_periods:
            result[f'ma{period}'] = close_series.rolling(window=period, min_periods=1).mean().iloc[::-1].values

        result['volume_ma5'] = volume_series.shift(1).rolling(window=5, min_periods=1).mean().iloc[::-1].values

        return result

    def _calculate_trend_metrics(self, df, lookback_days=20):
        """
        计算趋势指标（线性回归斜率和R²）

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :param lookback_days: 趋势判断天数
        :return: (slope, r_squared)
        """
        if len(df) < lookback_days:
            return 0.0, 0.0

        if 'trend_slope' in df.columns and 'trend_r_squared' in df.columns:
            return df['trend_slope'].iloc[0], df['trend_r_squared'].iloc[0]

        recent_df = df.iloc[:lookback_days].copy()
        recent_df = recent_df.iloc[::-1].reset_index(drop=True)
        y = recent_df['close'].values
        x = np.arange(len(y))

        try:
            x_mean = np.mean(x)
            y_mean = np.mean(y)
            numerator = np.sum((x - x_mean) * (y - y_mean))
            denominator = np.sum((x - x_mean) ** 2)

            if denominator == 0:
                return 0.0, 0.0

            slope = numerator / denominator
            y_pred = y_mean + slope * (x - x_mean)
            ss_res = np.sum((y - y_pred) ** 2)
            ss_tot = np.sum((y - y_mean) ** 2)

            if ss_tot == 0:
                return 0.0, 0.0

            r_squared = 1 - (ss_res / ss_tot)
            return slope, max(0, r_squared)
        except Exception:
            return 0.0, 0.0

    def select_stocks(self, df, stock_name='') -> list:
        """
        执行仙人指路策略选股

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :param stock_name: 股票名称
        :return: 选股结果列表（只包含确认成功的信号）
        """
        if not self._validate_data(df):
            return []

        if not self._validate_stock_name(stock_name):
            return []

        if not self._quick_filter_with_lookback(df):
            return []

        result = self.calculate_indicators(df)

        if len(result) < 30:
            return []

        try:
            lookback_days = self.params.get('lookback_days', 6)
            selection_result = self._check_immortal_guidance_with_lookback(result, lookback_days)
            return selection_result
        except Exception as e:
            return []

    def _check_immortal_guidance_with_lookback(self, df, lookback_days=6) -> list:
        """
        检查仙人指路形态（支持回溯查找）

        :param df: 含指标的DataFrame（倒序，最新在index=0）
        :param lookback_days: 回溯天数，查找最近N个交易日内出现的信号
        :return: 选股结果列表（只包含确认成功的信号）
        """
        lookback_days = min(lookback_days, len(df) - 3)

        if lookback_days < 1:
            return []

        for day_offset in range(lookback_days):
            today_idx = day_offset
            prev_idx = day_offset + 1

            if prev_idx >= len(df):
                break

            today = df.iloc[today_idx]
            prev_close = df.iloc[prev_idx]['close']

            if prev_close == 0 or pd.isna(prev_close):
                continue

            surge_pct = (today['high'] - prev_close) / prev_close
            
            # 计算上影线长度：根据K线类型确定
            if today['close'] > today['open']:
                # 阳线：上影线 = 最高价 - 收盘价（确保非负）
                upper_shadow = max(0, today['high'] - today['close'])
            else:
                # 阴线：上影线 = 最高价 - 开盘价（确保非负）
                upper_shadow = max(0, today['high'] - today['open'])
            
            # 计算K线实体长度
            body_length = abs(today['close'] - today['open'])
            
            # 计算上影线比例：上影线长度 / (上影线长度 + 实体长度)
            # 避免除零错误
            total_length = upper_shadow + body_length
            upper_shadow_ratio = upper_shadow / total_length if total_length > 0 else 0
            
            upper_shadow_50_price = (today['close'] + today['high']) / 2

            ma5 = today.get('ma5', 0)
            ma10 = today.get('ma10', 0)
            ma20 = today.get('ma20', 0)
            volume_ma5 = today.get('volume_ma5', 0)
            volume_ratio = today['volume'] / volume_ma5 if volume_ma5 > 0 else 0

            if not (self.params['surge_threshold'] <= surge_pct):
                continue

            if not (upper_shadow_ratio >= self.params['upper_shadow_ratio']):
                continue

            if not (self.params['volume_ratio_min'] <= volume_ratio <= self.params['volume_ratio_max']):
                continue

            if not (today['close'] > prev_close):
                continue

            if not (today['close'] > ma5):
                continue

            ma_bullish = (ma5 > ma10 > ma20) and (ma10 > ma20 > 0)
            if not ma_bullish:
                continue

            slope, r_squared = self._calculate_trend_metrics(df, self.params['trend_lookback_days'])
            if not (slope > 0 and r_squared >= self.params['trend_r_squared_threshold']):
                continue

            key_day_open = today['open']
            key_day_date = str(today['date']).split()[0]

            confirmation_result = self._check_confirmation(df, today_idx, key_day_open, upper_shadow_50_price)

            if not confirmation_result['confirmed']:
                continue

            latest_date = str(df.iloc[0]['date']).split()[0]

            return [{
                'date': latest_date,
                'close': round(df.iloc[0]['close'], 2),
                'volume_ratio': round(volume_ratio, 2),
                'reasons': ['仙人指路形态'],
                'key_date': key_day_date,
                'key_date_type': '仙人指路信号日',
                'pattern_date': today['date'],
                'pattern_details': {
                    'surge_pct': round(surge_pct, 4),
                    'upper_shadow_ratio': round(upper_shadow_ratio, 4),
                    'upper_shadow_50_price': round(upper_shadow_50_price, 2),
                    'key_day_open': round(key_day_open, 2),
                    'key_day_close': round(today['close'], 2),
                    'key_day_high': round(today['high'], 2),
                    'volume_ratio': round(volume_ratio, 2),
                    'ma5': round(ma5, 2),
                    'ma10': round(ma10, 2),
                    'ma20': round(ma20, 2),
                    'trend_slope': round(slope, 4),
                    'trend_r_squared': round(r_squared, 4),
                },
                'confirmation_details': {
                    'confirmed': confirmation_result['confirmed'],
                    'confirmed_date': confirmation_result.get('confirmed_date'),
                    'days_to_confirm': confirmation_result.get('days_to_confirm', 0),
                    'anti_body_price': confirmation_result.get('anti_body_price'),
                    'close_above_ma5': confirmation_result.get('close_above_ma5', True),
                    'post_confirmation_stable': confirmation_result.get('post_confirmation_stable', True),
                }
            }]

        return []

    def _check_confirmation(self, df, signal_day_idx, support_price, anti_body_target) -> dict:
        """
        检查T+1~T+3日确认条件，以及确认后是否继续维持在MA5之上

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :param signal_day_idx: 信号日索引
        :param support_price: 关键日开盘价
        :param anti_body_target: 上影线50%位置
        :return: 确认结果字典
        """
        window = self.params['anti_body_window']
        post_confirmation_window = 5
        result = {
            'confirmed': False,
            'confirmed_date': None,
            'days_to_confirm': 0,
            'anti_body_price': None,
            'close_above_ma5': True,
            'post_confirmation_stable': True,
        }

        confirmed_day_idx = None

        for day_idx in range(window):
            check_idx = signal_day_idx - (day_idx + 1)
            if check_idx < 0:
                break

            day_data = df.iloc[check_idx]
            day_close = day_data['close']
            day_ma5 = day_data.get('ma5', 0)
            day_volume = day_data.get('volume', 0)
            day_open = day_data.get('open', 0)
            day_high = day_data.get('high', 0)

            if day_close < day_ma5:
                result['close_above_ma5'] = False
                return result

            if day_close >= support_price:
                if day_close >= anti_body_target:
                    # 加强确认条件
                    # 1. 确认日成交量不能萎缩太多（至少是信号日的50%）
                    signal_day_volume = df.iloc[signal_day_idx].get('volume', 0)
                    if signal_day_volume > 0 and day_volume < signal_day_volume * 0.5:
                        continue
                    
                    # 2. 确认日K线形态健康：不能是长上影线
                    # 计算确认日的上影线比例
                    if day_close > day_open:
                        # 阳线：上影线 = 最高价 - 收盘价（确保非负）
                        confirm_upper_shadow = max(0, day_high - day_close)
                    else:
                        # 阴线：上影线 = 最高价 - 开盘价（确保非负）
                        confirm_upper_shadow = max(0, day_high - day_open)
                    
                    confirm_body_length = abs(day_close - day_open)
                    confirm_total_length = confirm_upper_shadow + confirm_body_length
                    confirm_upper_shadow_ratio = confirm_upper_shadow / confirm_total_length if confirm_total_length > 0 else 0
                    
                    # 确认日的上影线比例不能超过20%
                    if confirm_upper_shadow_ratio > 0.2:
                        continue
                    
                    # 3. 确认日收盘价相对支撑价要有一定涨幅（至少1%）
                    if (day_close - support_price) / support_price < 0.01:
                        continue
                    
                    result['confirmed'] = True
                    result['confirmed_date'] = str(day_data['date']).split()[0]
                    result['days_to_confirm'] = day_idx + 1
                    result['anti_body_price'] = day_close
                    confirmed_day_idx = check_idx
                    break

        if not result['confirmed']:
            return result

        for post_idx in range(1, post_confirmation_window + 1):
            check_idx = confirmed_day_idx - post_idx
            if check_idx < 0:
                break

            day_data = df.iloc[check_idx]
            day_close = day_data['close']
            day_ma5 = day_data.get('ma5', 0)

            if day_close < day_ma5:
                result['post_confirmation_stable'] = False
                result['confirmed'] = False
                return result

        return result

    def _check_immortal_guidance(self, df) -> list:
        """
        检查仙人指路形态（仅检测最新一天，保持向后兼容）

        :param df: 含指标的DataFrame（倒序，最新在index=0）
        :return: 选股结果列表
        """
        today = df.iloc[0]
        prev_close = df.iloc[1]['close'] if len(df) > 1 else None

        if prev_close is None or prev_close == 0:
            return []

        surge_pct = (today['high'] - prev_close) / prev_close

        upper_shadow = today['high'] - today['close']
        upper_shadow_ratio = upper_shadow / today['high'] if today['high'] > 0 else 0

        upper_shadow_50_price = (today['close'] + today['high']) / 2

        ma5 = today.get('ma5', 0)
        ma10 = today.get('ma10', 0)
        ma20 = today.get('ma20', 0)

        volume_ma5 = today.get('volume_ma5', 0)
        volume_ratio = today['volume'] / volume_ma5 if volume_ma5 > 0 else 0

        if not (self.params['surge_threshold'] <= surge_pct):
            return []

        if not (upper_shadow_ratio >= self.params['upper_shadow_ratio']):
            return []

        if not (self.params['volume_ratio_min'] <= volume_ratio <= self.params['volume_ratio_max']):
            return []

        if not (today['close'] > prev_close):
            return []

        if not (today['close'] > ma5):
            return []

        ma_bullish = (ma5 > ma10 > ma20) and (ma10 > ma20 > 0)
        if not ma_bullish:
            return []

        slope, r_squared = self._calculate_trend_metrics(df, self.params['trend_lookback_days'])
        if not (slope > 0 and r_squared >= self.params['trend_r_squared_threshold']):
            return []

        key_day_open = today['open']
        key_day_date = str(today['date']).split()[0]

        confirmation_result = self._check_confirmation(df, 0, key_day_open, upper_shadow_50_price)

        if not confirmation_result['confirmed']:
            return []

        return [{
            'stock_code': '',
            'stock_name': '',
            'signal_date': key_day_date,
            'key_day': key_day_date,
            'key_day_open': key_day_open,
            'support_level': key_day_open,
            'surge_pct': surge_pct,
            'upper_shadow_pct': upper_shadow_ratio,
            'upper_shadow_50_price': upper_shadow_50_price,
            'volume_ratio': volume_ratio,
            'ma5': ma5,
            'ma10': ma10,
            'ma20': ma20,
            'trend_slope': slope,
            'trend_r_squared': r_squared,
            'confirmed': confirmation_result['confirmed'],
            'confirmed_date': confirmation_result.get('confirmed_date'),
            'days_to_confirm': confirmation_result.get('days_to_confirm', 0),
            'anti_body_price': confirmation_result.get('anti_body_price'),
            'close_above_ma5': confirmation_result.get('close_above_ma5', True),
            'strategy_weight': self.params['strategy_weight'],
        }]

    def quick_filter(self, df) -> bool:
        """
        快速过滤 - 检查是否有长上影线形态

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :return: True表示通过快速过滤，False表示未通过
        """
        if df is None or df.empty or len(df) < 2:
            return False

        today = df.iloc[0]
        prev_close = df.iloc[1]['close']

        if prev_close == 0:
            return False

        surge_pct = (today['high'] - prev_close) / prev_close
        if surge_pct < self.params['surge_threshold']:
            return False

        # 计算上影线长度：根据K线类型确定
        if today['close'] > today['open']:
            # 阳线：上影线 = 最高价 - 收盘价
            upper_shadow = today['high'] - today['close']
        else:
            # 阴线：上影线 = 最高价 - 开盘价
            upper_shadow = today['high'] - today['open']
        
        # 计算K线实体长度
        body_length = abs(today['close'] - today['open'])
        
        # 计算上影线比例：上影线长度 / (上影线长度 + 实体长度)
        # 避免除零错误
        total_length = upper_shadow + body_length
        upper_shadow_ratio = upper_shadow / total_length if total_length > 0 else 0
        
        if upper_shadow_ratio < self.params['upper_shadow_ratio']:
            return False

        return True

    def _quick_filter_with_lookback(self, df) -> bool:
        """
        快速过滤（支持回溯）- 检查最近N天是否有潜在的仙人指路形态

        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :return: True表示通过快速过滤，False表示未通过
        """
        if df is None or df.empty:
            return False

        lookback_days = self.params.get('lookback_days', 6)
        lookback_days = min(lookback_days, len(df) - 1)

        if lookback_days < 1:
            return False

        for day_offset in range(lookback_days):
            today_idx = day_offset
            prev_idx = day_offset + 1

            if prev_idx >= len(df):
                break

            today = df.iloc[today_idx]
            prev_close = df.iloc[prev_idx]['close']

            if prev_close == 0 or pd.isna(prev_close):
                continue

            surge_pct = (today['high'] - prev_close) / prev_close
            if surge_pct < self.params['surge_threshold']:
                continue

            # 计算上影线长度：根据K线类型确定
            if today['close'] > today['open']:
                # 阳线：上影线 = 最高价 - 收盘价（确保非负）
                upper_shadow = max(0, today['high'] - today['close'])
            else:
                # 阴线：上影线 = 最高价 - 开盘价（确保非负）
                upper_shadow = max(0, today['high'] - today['open'])
            
            # 计算K线实体长度
            body_length = abs(today['close'] - today['open'])
            
            # 计算上影线比例：上影线长度 / (上影线长度 + 实体长度)
            # 避免除零错误
            total_length = upper_shadow + body_length
            upper_shadow_ratio = upper_shadow / total_length if total_length > 0 else 0
            
            if upper_shadow_ratio < self.params['upper_shadow_ratio']:
                continue

            return True

        return False
