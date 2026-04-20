import pandas as pd
import numpy as np
from datetime import datetime
from strategy.base_strategy import BaseStrategy

class LimitUpPullbackStrategy(BaseStrategy):
    """
    涨停回马枪策略 - 优化版本
    
    策略逻辑：
    1. 寻找最近出现的涨停板
    2. 涨停后出现合理回调（不破涨停日开盘价）
    3. 回调后出现反转信号（KDJ金叉、MACD金叉）
    4. 成交量萎缩后再次放大
    
    优化点：
    1. 快速预检查，提前过滤无涨停板的股票
    2. 减少不必要的指标计算
    3. 向量化操作替代循环
    """

    def __init__(self, params=None):
        """
        初始化策略
        
        :param params: 策略参数
        """
        # 默认参数
        default_params = {
            # 涨停确认参数
            'limit_up_lookback_days': 10,        # 涨停回溯天数
            'limit_up_threshold': 0.095,         # 涨停阈值（9.5%）
            'volume_ratio_threshold': 1.5,       # 成交量比阈值
            # 回调企稳参数
            'pullback_days_min': 1,             # 最小回调天数
            'pullback_days_max': 7,             # 最大回调天数
            'pullback_range_min': 0.00,         # 最小回调幅度（0%）
            'pullback_range_max': 0.15,         # 最大回调幅度（15%）
            'volume_shrinkage_ratio': 0.5,      # 成交量萎缩比例
            # 再次启动参数
            'kdj_gold_cross_threshold': 20,     # KDJ金叉阈值
            'macd_gold_cross_days': 3,          # MACD金叉确认天数
        }

        # 合并用户参数 - params 中的值覆盖默认值
        if params:
            default_params.update(params)

        # 调用父类初始化
        super().__init__("涨停回马枪策略", default_params)

    def calculate_indicators(self, df) -> pd.DataFrame:
        """
        计算技术指标（MA、KDJ、MACD、成交量均线） - 优化版本
        
        优化策略：
        1. 一次性排序数据（从倒序转为正序）
        2. 在正序数据上计算所有指标
        3. 一次性恢复原始顺序
        这样避免了KDJ、MACD、MA函数内部的重复排序
        
        注意：调用此方法前应先进行快速预检查，确保股票有涨停板
        
        :param df: 股票数据DataFrame（倒序，最新在index=0）
        :return: 添加了指标列的DataFrame
        """
        # 检测数据顺序
        try:
            is_descending = df['date'].iloc[0] > df['date'].iloc[-1]
        except (IndexError, KeyError):
            is_descending = False
        
        # 统一转换为正序计算（从早到晚）
        if is_descending:
            df_calc = df.iloc[::-1].copy().reset_index(drop=True)
        else:
            df_calc = df.copy().reset_index(drop=True)
        
        # 在正序数据上计算所有指标
        result = df_calc.copy()
        
        # 标记涨停（向量化操作）
        result['pct_change'] = result['close'].pct_change()
        result['is_limit_up'] = result['pct_change'] >= self.params['limit_up_threshold']
        result = result.drop('pct_change', axis=1)
        
        # 1. 计算均线（直接在正序数据上计算，避免反转）
        result['ma5'] = result['close'].rolling(window=5, min_periods=1).mean()
        result['ma10'] = result['close'].rolling(window=10, min_periods=1).mean()
        result['ma20'] = result['close'].rolling(window=20, min_periods=1).mean()
        
        # 2. 计算KDJ指标（在正序数据上直接计算）
        kdj_result = self._calculate_kdj_optimized(result)
        result['K'] = kdj_result['K']
        result['D'] = kdj_result['D']
        result['J'] = kdj_result['J']
        
        # 3. 计算MACD指标（在正序数据上直接计算）
        macd_result = self._calculate_macd_optimized(result)
        result['macd'] = macd_result['macd']
        result['macd_signal'] = macd_result['macd_signal']
        result['macd_hist'] = macd_result['macd_hist']
        
        # 4. 计算成交量均线
        result['volume_ma5'] = result['volume'].rolling(window=5, min_periods=1).mean()
        result['volume_ma10'] = result['volume'].rolling(window=10, min_periods=1).mean()
        
        # 恢复原始顺序
        if is_descending:
            result = result.iloc[::-1].reset_index(drop=True)
        
        result.index = df.index
        return result
    
    def _calculate_kdj_optimized(self, df_calc):
        """
        KDJ指标计算 - 优化版本（假设输入数据已是正序）
        
        :param df_calc: 正序排列的DataFrame
        :return: 包含K、D、J列的DataFrame
        """
        n = 9
        m1 = 3
        m2 = 3
        
        # 计算RSV
        low_min = df_calc['low'].rolling(window=n, min_periods=1).min()
        high_max = df_calc['high'].rolling(window=n, min_periods=1).max()
        
        range_val = high_max - low_min
        rsv = pd.Series(index=df_calc.index, dtype=float)
        
        # RSV计算，前n-1个周期不足时用50填充
        for i in range(len(df_calc)):
            if i < n - 1 or range_val.iloc[i] == 0:
                rsv.iloc[i] = 50.0
            else:
                rsv.iloc[i] = (df_calc['close'].iloc[i] - low_min.iloc[i]) / range_val.iloc[i] * 100
        
        # SMA计算 - 通达信风格
        k = pd.Series(index=df_calc.index, dtype=float)
        d = pd.Series(index=df_calc.index, dtype=float)
        
        # 初始化第一日K、D值为50
        k.iloc[0] = 50.0
        d.iloc[0] = 50.0
        
        # 递归计算
        for i in range(1, len(df_calc)):
            k.iloc[i] = (rsv.iloc[i] * 1 + k.iloc[i-1] * (m1 - 1)) / m1
            d.iloc[i] = (k.iloc[i] * 1 + d.iloc[i-1] * (m2 - 1)) / m2
        
        # 计算J值
        j = 3 * k - 2 * d
        
        return pd.DataFrame({
            'K': k,
            'D': d,
            'J': j
        })
    
    def _calculate_macd_optimized(self, df_calc):
        """
        MACD指标计算 - 优化版本（假设输入数据已是正序）
        
        :param df_calc: 正序排列的DataFrame
        :return: 包含macd、macd_signal、macd_hist列的DataFrame
        """
        fastperiod = 12
        slowperiod = 26
        signalperiod = 9
        
        # 计算快速和慢速EMA
        ema_fast = df_calc['close'].ewm(span=fastperiod, adjust=False, min_periods=1).mean()
        ema_slow = df_calc['close'].ewm(span=slowperiod, adjust=False, min_periods=1).mean()
        
        # 计算DIF（MACD线）
        dif = ema_fast - ema_slow
        
        # 计算DEA（信号线）
        dea = dif.ewm(span=signalperiod, adjust=False, min_periods=1).mean()
        
        # 计算MACD柱状图
        macd = 2 * (dif - dea)
        
        return pd.DataFrame({
            'macd': dif,
            'macd_signal': dea,
            'macd_hist': macd
        })

    def _find_limit_up(self, df):
        """
        寻找最近的涨停板 - 向量化优化版本
        
        优化点：
        1. 使用向量化操作找出涨停板
        2. 使用numpy数组操作，避免循环
        
        :param df: 含指标的DataFrame（倒序，最新在index=0）
        :return: 涨停板信息列表，每个元素为 (index, date, close, open, volume)
        """
        lookback_days = self.params['limit_up_lookback_days']
        
        # 只检查最近的lookback_days个交易日
        check_df = df.head(lookback_days)
        
        # 使用向量化操作找出所有涨停板
        limit_up_mask = check_df['is_limit_up'].values
        
        if not limit_up_mask.any():
            return []
        
        # 计算成交量比（当前成交量 / 前一日成交量）- 向量化操作
        volumes = check_df['volume'].values
        # 前一日成交量（由于数据倒序，shift(-1)相当于前一日）
        prev_volumes = np.roll(volumes, -1)
        volume_ratios = np.where(prev_volumes > 0, volumes / prev_volumes, 0)
        
        # 找出成交量放大的涨停板
        volume_ok = volume_ratios >= self.params['volume_ratio_threshold']
        
        # 找出同时满足涨停和成交量放大的位置
        valid_mask = limit_up_mask & volume_ok
        valid_positions = np.where(valid_mask)[0]
        
        # 如果没有找到符合条件的涨停板，直接返回
        if len(valid_positions) == 0:
            return []
        
        # 构建涨停板信息列表 - 使用向量化操作
        limit_ups = []
        dates = check_df['date'].values
        closes = check_df['close'].values
        opens = check_df['open'].values
        
        # 直接使用numpy数组索引，避免循环
        for pos in valid_positions:
            limit_ups.append((
                int(pos),  # 涨停板在DataFrame中的位置
                dates[pos],
                closes[pos],
                opens[pos],
                volumes[pos]
            ))
        
        return limit_ups

    def _check_pullback(self, df, limit_up_info):
        """
        检查涨停后是否出现合理回调 - 向量化优化版本
        
        优化点：
        1. 使用向量化操作计算最高价和最低价
        2. 避免循环操作
        3. 使用numpy的any()函数快速检查条件
        
        :param df: 含指标的DataFrame（倒序，最新在index=0）
        :param limit_up_info: 涨停板信息 (index, date, close, open, volume)
        :return: 回调信息字典，包含回调天数、幅度、是否不破开盘价、是否有成交量萎缩
        """
        # 获取参数
        pullback_days_min = self.params['pullback_days_min']
        pullback_days_max = self.params['pullback_days_max']
        pullback_range_min = self.params['pullback_range_min']
        pullback_range_max = self.params['pullback_range_max']
        volume_shrinkage_ratio = self.params['volume_shrinkage_ratio']

        # 涨停板信息
        lu_idx, lu_date, lu_close, lu_open, lu_volume = limit_up_info

        # 回调期间是涨停板之后的交易日，即索引从0到lu_idx-1
        start_idx = 0
        end_idx = lu_idx

        # 检查回调期间的数据
        if end_idx <= start_idx:
            return None
        
        # 使用向量化操作获取数据
        pullback_df = df.iloc[start_idx:end_idx]
        if pullback_df.empty:
            return None

        # 向量化计算最高价和最低价
        highest_price = pullback_df['high'].max()
        lowest_price = pullback_df['low'].min()
        
        # 回调幅度：从最高价回撤到最低价的幅度
        pullback_range = (highest_price - lowest_price) / highest_price

        # 检查回调幅度是否在范围内
        if pullback_range < pullback_range_min or pullback_range > pullback_range_max:
            return None

        # 检查是否不破涨停日开盘价
        if lowest_price < lu_open:
            return None

        # 向量化检查成交量萎缩
        pullback_volumes = pullback_df['volume'].values
        volume_threshold = lu_volume * volume_shrinkage_ratio
        has_volume_shrinkage = np.any(pullback_volumes <= volume_threshold)

        if not has_volume_shrinkage:
            return None

        # 计算回调天数
        pullback_days = end_idx - start_idx
        # 确保回调天数在范围内
        if pullback_days < pullback_days_min or pullback_days > pullback_days_max:
            return None

        return {
            'pullback_days': pullback_days,
            'pullback_range': pullback_range,
            'lowest_price': lowest_price,
            'highest_price': highest_price,
            'limit_up_open': lu_open,
            'has_volume_shrinkage': has_volume_shrinkage
        }

    def _check_reversal(self, df):
        """
        检查是否出现再次启动信号
        
        :param df: 含指标的DataFrame（倒序，最新在index=0）
        :return: 是否出现反转信号
        """
        # 检查KDJ金叉
        if 'K' in df.columns and 'D' in df.columns and 'J' in df.columns:
            # 最新K、D值
            latest_k = df['K'].iloc[0]
            latest_d = df['D'].iloc[0]
            latest_j = df['J'].iloc[0]
            
            # 前一天K、D值
            if len(df) > 1:
                prev_k = df['K'].iloc[1]
                prev_d = df['D'].iloc[1]
            else:
                prev_k = 0
                prev_d = 0
            
            # KDJ金叉：K上穿D，且J值大于阈值
            kdj_gold_cross = (latest_k > latest_d and prev_k <= prev_d) or (latest_j > self.params['kdj_gold_cross_threshold'])
        else:
            kdj_gold_cross = False

        # 检查MACD金叉
        if 'macd' in df.columns and 'macd_signal' in df.columns and 'macd_hist' in df.columns:
            # 最新MACD值
            latest_macd = df['macd'].iloc[0]
            latest_signal = df['macd_signal'].iloc[0]
            latest_hist = df['macd_hist'].iloc[0]
            
            # 前一天MACD值
            if len(df) > 1:
                prev_macd = df['macd'].iloc[1]
                prev_signal = df['macd_signal'].iloc[1]
                prev_hist = df['macd_hist'].iloc[1]
            else:
                prev_macd = 0
                prev_signal = 0
                prev_hist = 0
            
            # MACD金叉：macd上穿signal，且hist由负转正
            macd_gold_cross = (latest_macd > latest_signal and prev_macd <= prev_signal) or (latest_hist > 0 and prev_hist <= 0)
        else:
            macd_gold_cross = False

        # 检查成交量是否放大
        volume_increase = False
        if 'volume' in df.columns and 'volume_ma5' in df.columns:
            latest_volume = df['volume'].iloc[0]
            volume_ma5 = df['volume_ma5'].iloc[0]
            if volume_ma5 > 0:
                volume_increase = latest_volume > volume_ma5

        # 至少满足两个条件
        conditions = [kdj_gold_cross, macd_gold_cross, volume_increase]
        return sum(conditions) >= 2
    
    def get_selection_criteria(self):
        """
        获取选股条件描述
        :return: 选股条件描述列表
        """
        criteria = []
        
        # 条件1：涨停确认
        limit_up_lookback_days = self.params['limit_up_lookback_days']
        limit_up_threshold = self.params['limit_up_threshold'] * 100
        volume_ratio_threshold = self.params['volume_ratio_threshold']
        criteria.append(f"1. 涨停确认：最近{limit_up_lookback_days}个交易日内出现涨停板（涨幅>={limit_up_threshold:.1f}%），且成交量是前1日的{volume_ratio_threshold:.1f}倍以上")
        
        # 条件2：回调企稳
        pullback_days_min = self.params['pullback_days_min']
        pullback_days_max = self.params['pullback_days_max']
        pullback_range_min = self.params['pullback_range_min'] * 100
        pullback_range_max = self.params['pullback_range_max'] * 100
        criteria.append(f"2. 回调企稳：涨停后{pullback_days_min}-{pullback_days_max}个交易日内出现回调，回调幅度{pullback_range_min:.0f}%-{pullback_range_max:.0f}%，且不破涨停日开盘价")
        
        # 条件3：成交量萎缩
        volume_shrinkage_ratio = self.params['volume_shrinkage_ratio'] * 100
        criteria.append(f"3. 成交量萎缩：回调期间至少一日成交量 <= 涨停日成交量的{volume_shrinkage_ratio:.0f}%")
        
        # 条件4：再次启动
        kdj_gold_cross_threshold = self.params['kdj_gold_cross_threshold']
        criteria.append(f"4. 再次启动：KDJ金叉（J值>{kdj_gold_cross_threshold}）或MACD金叉或成交量放大（满足2个即可）")
        
        return criteria

    def select_stocks(self, data, stock_name="", skip_data_check=False):
        """
        选择符合条件的股票 - 优化版本
        
        优化点：
        1. 快速预检查只检查涨幅，不计算其他指标
        2. 只有通过预检查的股票才计算完整指标
        3. 避免对无涨停板股票的复杂计算
        
        :param data: 股票数据（DataFrame或list）
        :param stock_name: 股票代码
        :param skip_data_check: 是否跳过数据检查
        :return: 选中的股票列表
        """
        try:
            # 数据检查
            if not skip_data_check:
                if data is None or (isinstance(data, list) and len(data) == 0):
                    return []

            # 转换为DataFrame
            if not isinstance(data, pd.DataFrame):
                df = pd.DataFrame(data)
            else:
                df = data.copy()
            
            # 快速预检查：检查是否有涨停板（只检查涨幅，不计算其他指标）
            lookback_days = self.params['limit_up_lookback_days']
            limit_up_threshold = self.params['limit_up_threshold']
            
            # 只取需要的列，提高速度
            # 注意：数据库返回的数据是正序的（从旧到新），所以使用 pct_change(1) 计算相对于前一行的变化
            check_df = df[['close']].head(lookback_days + 1)
            
            # 向量化计算涨跌幅
            # 使用pct_change(1)计算相对于前一行（更新日期）的变化
            pct_change = check_df['close'].pct_change(1)
            
            # 如果没有涨停板，直接返回空列表
            # 注意：pct_change[-1]是NaN，所以从[:-1]检查（排除最后一行）
            if not (pct_change.iloc[:-1] >= limit_up_threshold).any():
                return []

            # 计算指标（只有有涨停板的股票才会到达这里）
            df_with_indicators = self.calculate_indicators(df)

            # 寻找涨停板
            limit_ups = self._find_limit_up(df_with_indicators)
            if not limit_ups:
                return []

            # 检查回调条件
            for lu_info in limit_ups:
                pullback_info = self._check_pullback(df_with_indicators, lu_info)
                if pullback_info:
                    # 检查反转信号
                    if self._check_reversal(df_with_indicators):
                        # 获取最新数据
                        latest = df_with_indicators.iloc[0]
                        
                        # 构建信号字典 - 统一格式（仅保留关键信息）
                        # 格式化日期
                        limit_up_date = lu_info[1]
                        if hasattr(limit_up_date, 'strftime'):
                            limit_up_date_str = limit_up_date.strftime('%Y-%m-%d')
                        else:
                            limit_up_date_str = str(limit_up_date)[:10]
                        
                        signal = {
                            'key_date': limit_up_date_str,
                            'key_date_type': '涨停日',
                            'reasons': [
                                f"涨停日期: {limit_up_date_str}",
                                f"涨停价格: {float(lu_info[2]):.2f}",
                                f"回调天数: {pullback_info['pullback_days']}",
                                f"回调幅度: {pullback_info['pullback_range']:.2%}"
                            ]
                        }
                        return [signal]

            return []

        except Exception:
            return []
