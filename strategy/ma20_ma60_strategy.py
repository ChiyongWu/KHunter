"""
520560策略 - 均线金叉+量能共振策略

选股条件：
1. 均线金叉：5日均线（MA5）上穿20日均线（MA20）
   - 当日：MA5 > MA20
   - 前一日：MA5 ≤ MA20

2. 量能共振：5日均量线（VOL5）≥ 60日均量线（VOL60）

3. 趋势确认：20日均线方向向上
   - 当日MA20 > N日前MA20（默认N=5）

4. 同日共振：以上三个条件必须在同一天同时满足

策略特点：
- 价量配合：价格趋势 + 成交量共振
- 趋势确认：排除下降趋势中的假金叉
- 信号可靠：三重条件确认
"""
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from strategy.base_strategy import BaseStrategy


class MA20MA60Strategy(BaseStrategy):
    """520560策略 - 均线金叉+量能共振策略"""
    
    def __init__(self, params=None):
        """
        初始化策略
        
        Args:
            params: 策略参数，包含以下可选参数：
                - ma_short_period: 短期均线周期，默认5
                - ma_long_period: 长期均线周期，默认20
                - vol_short_period: 短期均量线周期，默认5
                - vol_long_period: 长期均量线周期，默认60
                - trend_lookback_days: 判断均线趋势的回溯天数，默认5
        """
        # 默认参数配置
        default_params = {
            'ma_short_period': 5,              # 短期均线周期（MA5）
            'ma_long_period': 20,              # 长期均线周期（MA20）
            'vol_short_period': 5,             # 短期均量线周期（VOL5）
            'vol_long_period': 60,             # 长期均量线周期（VOL60）
            'trend_lookback_days': 5           # 判断均线趋势的回溯天数
        }
        
        # 合并用户参数
        if params:
            default_params.update(params)
        
        super().__init__("520560策略", default_params)
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算策略所需指标
        
        Args:
            df: 股票K线数据，包含date, close, volume等字段
            
        Returns:
            添加了指标的DataFrame
        """
        if df.empty or len(df) < 2:
            return pd.DataFrame()
        
        result = df.copy()
        
        # 按日期升序排序，确保技术指标计算正确
        result = result.sort_values('date', ascending=True)
        
        close = result['close']
        volume = result['volume']
        
        # 计算均线
        result['ma_short'] = close.rolling(window=self.params['ma_short_period'], min_periods=1).mean()
        result['ma_long'] = close.rolling(window=self.params['ma_long_period'], min_periods=1).mean()
        
        # 计算均量线
        result['vol_short'] = volume.rolling(window=self.params['vol_short_period'], min_periods=1).mean()
        result['vol_long'] = volume.rolling(window=self.params['vol_long_period'], min_periods=1).mean()
        
        # 计算均线金叉信号
        # 金叉 = 当日短期均线上穿长期均线
        result['ma_cross_signal'] = (result['ma_short'] > result['ma_long']) & \
                                    (result['ma_short'].shift(1) <= result['ma_long'].shift(1))
        
        # 计算量能共振信号
        result['vol_resonance_signal'] = result['vol_short'] >= result['vol_long']
        
        # 计算趋势向上信号
        # 20日均线方向向上：当日MA20 > N日前MA20
        lookback = self.params['trend_lookback_days']
        result['trend_up_signal'] = result['ma_long'] > result['ma_long'].shift(lookback)
        
        # 综合信号：三个条件同时满足
        result['signal'] = result['ma_cross_signal'] & \
                          result['vol_resonance_signal'] & \
                          result['trend_up_signal']
        
        # 按日期降序排序返回
        result = result.sort_values('date', ascending=False)
        
        return result
    
    def get_selection_criteria(self):
        """
        获取选股条件描述
        
        Returns:
            list: 选股条件描述列表
        """
        criteria = []
        
        ma_short = self.params['ma_short_period']
        ma_long = self.params['ma_long_period']
        vol_short = self.params['vol_short_period']
        vol_long = self.params['vol_long_period']
        trend_days = self.params['trend_lookback_days']
        
        criteria.append(f"1. 均线金叉：{ma_short}日均线上穿{ma_long}日均线")
        criteria.append(f"2. 量能共振：{vol_short}日均量线≥{vol_long}日均量线")
        criteria.append(f"3. 趋势向上：{ma_long}日均线方向向上（{trend_days}日内上涨）")
        criteria.append(f"4. 同日共振：以上三个条件同一天满足")
        
        return criteria
    
    def select_stocks(self, df, stock_code='', stock_name='', df_with_indicators=None) -> list:
        """
        选股方法 - 返回满足条件的股票列表
        
        Args:
            df: 股票K线数据
            stock_code: 股票代码（可选）
            stock_name: 股票名称（可选）
            df_with_indicators: 预计算的指标数据（可选）
            
        Returns:
            list: 满足条件的信号列表
        """
        try:
            # 使用预计算的指标数据或重新计算
            if df_with_indicators is not None:
                result_df = df_with_indicators
            else:
                result_df = self.calculate_indicators(df)
            
            if result_df.empty:
                return []
            
            # 获取最新数据
            latest = result_df.iloc[0]
            
            # 判断是否满足选股条件
            if latest.get('signal', False):
                # 格式化日期
                if hasattr(latest['date'], 'strftime'):
                    date_str = latest['date'].strftime('%Y-%m-%d')
                    key_date_str = date_str
                else:
                    date_str = str(latest['date'])[:10]
                    key_date_str = date_str
                
                return [{
                    'date': date_str,
                    'close': round(float(latest['close']), 2),
                    'key_date': key_date_str,
                    'key_date_type': '金叉日',
                    'stock_code': stock_code,
                    'signal_date': date_str,
                    'ma_short': round(float(latest['ma_short']), 2),
                    'ma_long': round(float(latest['ma_long']), 2),
                    'vol_short': round(float(latest['vol_short']), 0),
                    'vol_long': round(float(latest['vol_long']), 0),
                    'strategy_name': self.name,
                    'reasons': ['均线金叉', '量能共振', '趋势向上']
                }]
            
            return []
        except Exception as e:
            self.logger.error(f"处理股票 {stock_code} {stock_name} 失败: {str(e)}")
            return []
    
    def batch_select_stocks(self, data: dict) -> list:
        """
        批量选股方法 - 返回满足条件的股票列表（兼容API调用）
        
        Args:
            data: 包含股票代码和K线数据的字典
            
        Returns:
            list: 满足条件的股票代码列表
        """
        selected = []
        
        for stock_code, df in data.items():
            try:
                signals = self.select_stocks(df)
                if signals:
                    for signal in signals:
                        signal['stock_code'] = stock_code
                        selected.append(signal)
            except Exception as e:
                self.logger.error(f"处理股票 {stock_code} 失败: {str(e)}")
                continue
        
        return selected
    
    def get_signal_date(self, df: pd.DataFrame) -> str:
        """
        获取信号日期
        
        Args:
            df: 股票K线数据
            
        Returns:
            信号日期，如果没有信号返回空字符串
        """
        df = self.calculate_indicators(df)
        
        if df.empty:
            return ""
        
        # 找到最新的信号日期
        signal_rows = df[df['signal']]
        
        if signal_rows.empty:
            return ""
        
        return str(signal_rows.iloc[0]['date'])