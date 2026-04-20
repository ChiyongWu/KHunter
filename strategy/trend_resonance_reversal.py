"""
趋势共振反转策略 - 多指标共振底部反转

选股条件：
1. RSI从30以下突破至50以上（最近3天内）
2. 随后出现5日均线上穿20日均线
3. 随后出现DIF线上穿DEA线
4. 所有信号在3个交易日内发生（时间共振）
"""
import pandas as pd
import numpy as np
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from strategy.base_strategy import BaseStrategy
from utils.technical import MA, MACD, RSI


class TrendResonanceReversalStrategy(BaseStrategy):
    """趋势共振反转策略 - 多指标共振底部反转"""
    
    def __init__(self, params=None):
        # 默认参数
        default_params = {
            'rsi_period': 14,           # RSI计算周期
            'rsi_oversold': 30,         # RSI超卖阈值
            'rsi_breakout': 50,         # RSI突破阈值
            'short_ma_period': 5,       # 短期均线周期
            'long_ma_period': 20,       # 长期均线周期
            'macd_fast': 12,            # MACD快线周期
            'macd_slow': 26,            # MACD慢线周期
            'macd_signal': 9,           # MACD信号线周期
            'signal_days': 3,           # 信号共振时间窗口（天）
        }
        
        if params:
            default_params.update(params)
        
        super().__init__("趋势共振反转策略", default_params)
    
    def calculate_indicators(self, df) -> pd.DataFrame:
        """
        计算趋势共振反转策略所需的指标
        
        参数：
            df: 股票日线数据DataFrame（可能是倒序或正序）
        
        返回：
            添加了指标的DataFrame（正序，最新在最后）
        """
        result = df.copy()
        
        # 检测数据顺序
        is_descending = False
        if len(result) > 1 and result['date'].iloc[0] > result['date'].iloc[1]:
            is_descending = True
            result = result.iloc[::-1].reset_index(drop=True)
        
        # 计算RSI指标
        rsi_df = RSI(result, period=self.params['rsi_period'])
        result['rsi'] = rsi_df['rsi']
        
        # 计算均线
        result['ma_short'] = MA(result['close'], self.params['short_ma_period'])
        result['ma_long'] = MA(result['close'], self.params['long_ma_period'])
        
        # 计算MACD指标
        macd_df = MACD(result, 
                      fastperiod=self.params['macd_fast'],
                      slowperiod=self.params['macd_slow'],
                      signalperiod=self.params['macd_signal'])
        result['macd_dif'] = macd_df['macd']          # DIF线
        result['macd_dea'] = macd_df['macd_signal']   # DEA线
        result['macd_hist'] = macd_df['macd_hist']    # MACD柱状图
        
        # 始终返回正序数据（最新在最后）
        return result
    
    def get_selection_criteria(self):
        """
        获取选股条件描述
        :return: 选股条件描述列表
        """
        criteria = []
        
        # 条件1：RSI突破
        rsi_period = self.params['rsi_period']
        rsi_oversold = self.params['rsi_oversold']
        rsi_breakout = self.params['rsi_breakout']
        criteria.append(f"1. RSI突破：RSI({rsi_period})从{rsi_oversold}以下突破至{rsi_breakout}以上")
        
        # 条件2：均线金叉
        short_ma_period = self.params['short_ma_period']
        long_ma_period = self.params['long_ma_period']
        criteria.append(f"2. 均线金叉：{short_ma_period}日均线上穿{long_ma_period}日均线")
        
        # 条件3：MACD金叉
        criteria.append(f"3. MACD金叉：DIF线上穿DEA线")
        
        # 条件4：顺序要求
        criteria.append(f"4. 顺序要求：RSI信号先出现，随后出现均线金叉和MACD金叉")
        
        return criteria
    
    def select_stocks(self, df, stock_name='') -> list:
        """
        选股逻辑 - 识别趋势共振反转信号
        
        条件：
        1. RSI 从 30 以下突破至 50 以上（最近 3 天内）
        2. 随后出现 5日均线上穿20日均线
        3. 随后出现 DIF线上穿DEA线
        4. 所有信号在 3 个交易日内发生
        
        参数：
            df: 股票日线数据DataFrame
            stock_name: 股票名称
        
        返回：
            符合条件的信号列表
        """
        if df.empty or len(df) < 30:
            return []
        
        try:
            # 计算指标（返回正序数据，最新在最后）
            df = self.calculate_indicators(df)
            
            # 获取参数
            rsi_oversold = self.params['rsi_oversold']
            rsi_breakout = self.params['rsi_breakout']
            signal_days = self.params['signal_days']
            
            # 检查数据是否足够
            if len(df) < 30:
                return []
            
            # 初始化信号标记
            signals = []
            
            # 第一步：在最近 signal_days 天内寻找 RSI 突破信号
            # 数据是正序的（最新在最后），所以从最后向前搜索
            rsi_breakout_day = None
            
            # 搜索范围：最后 signal_days 天
            search_start = max(0, len(df) - signal_days - 1)
            
            for i in range(len(df) - 1, search_start, -1):
                # 当前 RSI >= 突破阈值，且前一日 RSI < 突破阈值
                if (df['rsi'].iloc[i] >= rsi_breakout and 
                    df['rsi'].iloc[i-1] < rsi_breakout):
                    # 检查是否曾经超卖
                    lookback = min(signal_days * 2, i)
                    if df['rsi'].iloc[max(0, i-lookback):i].min() <= rsi_oversold:
                        rsi_breakout_day = i
                        break  # 找到最近的 RSI 突破信号
            
            # 如果没有找到 RSI 突破信号，直接返回
            if rsi_breakout_day is None:
                return []
            
            # 第二步：在 RSI 突破之后寻找均线金叉和 MACD 金叉
            # 搜索范围：从 RSI 突破日开始向后搜索，直到 RSI 突破日之后 signal_days 天
            ma_cross_day = None
            macd_cross_day = None
            
            search_end = min(len(df), rsi_breakout_day + signal_days + 1)
            
            for i in range(rsi_breakout_day, search_end):
                # 均线金叉信号检测
                if ma_cross_day is None and i > 0:
                    # 当前短期均线 > 长期均线，且前一日短期均线 <= 长期均线
                    if (df['ma_short'].iloc[i] > df['ma_long'].iloc[i] and 
                        df['ma_short'].iloc[i-1] <= df['ma_long'].iloc[i-1]):
                        ma_cross_day = i
                
                # MACD 金叉信号检测
                if macd_cross_day is None and i > 0:
                    # 当前 DIF > DEA，且前一日 DIF <= DEA
                    if (df['macd_dif'].iloc[i] > df['macd_dea'].iloc[i] and 
                        df['macd_dif'].iloc[i-1] <= df['macd_dea'].iloc[i-1]):
                        macd_cross_day = i
                
                # 如果两个信号都找到了，提前退出
                if ma_cross_day is not None and macd_cross_day is not None:
                    break
            
            # 第三步：检查是否找到了均线金叉和 MACD 金叉
            if ma_cross_day is not None and macd_cross_day is not None:
                # 关键日期：RSI 突破日
                key_date = df['date'].iloc[rsi_breakout_day]
                key_date_str = key_date.strftime('%Y-%m-%d') if hasattr(key_date, 'strftime') else str(key_date)[:10]
                
                signal = {
                    'stock_code': df['code'].iloc[0] if 'code' in df.columns else stock_name,
                    'stock_name': stock_name,
                    'date': df['date'].iloc[rsi_breakout_day],
                    'key_date': key_date_str,
                    'key_date_type': 'RSI突破日',
                    'rsi_breakout_day': df['date'].iloc[rsi_breakout_day],
                    'ma_cross_day': df['date'].iloc[ma_cross_day],
                    'macd_cross_day': df['date'].iloc[macd_cross_day],
                    'rsi_value': df['rsi'].iloc[rsi_breakout_day],
                    'ma_short': df['ma_short'].iloc[ma_cross_day],
                    'ma_long': df['ma_long'].iloc[ma_cross_day],
                    'macd_dif': df['macd_dif'].iloc[macd_cross_day],
                    'macd_dea': df['macd_dea'].iloc[macd_cross_day],
                    'close': df['close'].iloc[rsi_breakout_day],
                    'reason': f'RSI突破({df["rsi"].iloc[rsi_breakout_day]:.1f}), 均线金叉, MACD金叉'
                }
                signals.append(signal)
            
            return signals
            
        except Exception as e:
            import logging
            logging.error(f"趋势共振反转策略选股失败: {str(e)}")
            return []
