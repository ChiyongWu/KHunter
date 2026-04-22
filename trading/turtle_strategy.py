"""
海归策略实现
基于唐奇安通道和ATR的趋势跟踪策略
"""
import pandas as pd
import logging
from trading.timing_strategies import TimingStrategy, TimingResult
from trading.technical_indicators import TechnicalIndicators
from typing import Dict, Optional


class TurtleStrategy(TimingStrategy):
    """海归策略"""
    
    def __init__(self, config):
        """初始化海归策略
        
        Args:
            config: 策略配置
        """
        super().__init__(config)
        
        # 初始化技术指标计算
        self.technical_indicators = TechnicalIndicators()
        
        # 默认参数
        self.n1 = self.config.get('n1', 6)  # 上线周期
        self.n2 = self.config.get('n2', 12)  # 下线周期
        self.atr_period = self.config.get('atr_period', 10)  # ATR周期
        self.entry_atr = self.config.get('entry_atr', 0.02)  # 入场ATR比例
        self.add_atr = self.config.get('add_atr', 0.02)  # 加仓ATR比例
        self.reduce_atr = self.config.get('reduce_atr', 0.03)  # 减仓ATR比例
        self.base_position_amount = self.config.get('base_position_amount', 50000)  # 底仓金额（元）
        self.position_ratio = self.config.get('position_ratio', 0.05)  # 仓位比例（占总资金）
        self.use_fixed_amount = self.config.get('use_fixed_amount', True)  # 是否使用固定金额（False则使用仓位比例）
    
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算海归策略所需指标
        
        Args:
            df: 股票数据
            
        Returns:
            添加了指标的DataFrame
        """
        result = df.copy()
        
        # 确保数据按日期正序排列
        if len(result) > 1 and result['date'].iloc[0] > result['date'].iloc[1]:
            result = result.iloc[::-1].reset_index(drop=True)
        
        # 计算通道线
        result['up'] = result['high'].shift(1).rolling(window=self.n1).max()  # 上线
        result['down'] = result['low'].shift(1).rolling(window=self.n2).min()  # 下线
        
        # 计算真实高点和真实低点
        result['true_high'] = result[['high', 'close']].max(axis=1)
        result['true_low'] = result[['low', 'close']].min(axis=1)
        
        # 计算ATR
        result['atr'] = self.technical_indicators.calculate_atr(result, self.atr_period)
        
        return result
    
    def get_timing_result(self, df: pd.DataFrame, position: Optional[Dict] = None, cash: Optional[float] = None) -> TimingResult:
        """获取海归策略择时结果
        
        Args:
            df: 股票数据
            position: 持仓信息
            cash: 可用资金
            
        Returns:
            择时结果
        """
        result = TimingResult()
        
        # 计算指标
        df = self.calculate_indicators(df)
        
        # 获取最新数据
        latest = df.iloc[-1]
        
        # 计算收益率
        entry_price = position['buy_price'] if position else 0
        current_price = latest['close']
        hold_return = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        
        # 卖出条件（仅基于指标的信号，止盈止损由回测引擎处理）
        if position:
            # 已持仓，判断卖出条件
            # 日线系统：判断前一天是否跌破下线
            if len(df) >= 2:
                prev = df.iloc[-2]  # 前一天的数据
                if pd.notna(prev['down']) and prev['low'] < prev['down']:
                    # 前一天最低价跌破下线，清仓卖出
                    result.is_sell = True
                    result.signal_strength = 1.0
                    result.message = f"前一天跌破下线 {prev['down']:.2f}，卖出信号"
                    result.trade_type = 'sell'
                    # 清仓卖出：不需要100的整数倍
                    result.sell_quantity = position.get('quantity', 0)
        
        # 买入条件（仅在无持仓且无卖出信号时执行）
        if not position and not result.is_sell:
            # 未持仓，判断买入条件
            # 日线系统：判断前一天是否突破上线
            if len(df) >= 2:
                prev = df.iloc[-2]  # 前一天的数据
                if pd.notna(prev['up']) and prev['high'] > prev['up']:
                    # 前一天最高价突破上线
                    buy_price = latest['open']  # 买入价格为当天开盘价
                    # 策略只负责给出信号，限价由回测引擎处理
                    result.is_buy = True
                    result.signal_strength = 1.0
                    result.message = f"前一天突破上线 {prev['up']:.2f}，买入信号"
                    result.support_level = prev['up'] * 0.95  # 支撑位为上线的95%
                    result.trade_type = 'buy'
                    # 计算买入数量：根据固定金额或仓位比例（不依赖可用资金，资金限制由回测引擎处理）
                    if self.use_fixed_amount:
                        # 使用固定金额
                        buy_amount = self.base_position_amount
                    else:
                        # 使用仓位比例（需要外部传入总资金，暂用固定金额兜底）
                        buy_amount = self.base_position_amount
                    # A股规则：买入数量必须是100的整数倍
                    result.buy_quantity = int(buy_amount / buy_price) // 100 * 100
        
        # 计算加仓/减仓信号（仅在有持仓且无卖出信号时执行）
        if position and not result.is_sell:
            # 加仓条件：盈利且价格上涨
            if hold_return > 0 and latest['high'] >= entry_price + 0.5 * latest['atr']:
                add_price = latest['open']  # 加仓价格为当天开盘价
                # 策略只负责给出信号，限价由回测引擎处理
                # 给出加仓信号
                result.is_buy = True
                result.signal_strength = 0.8
                result.message = "满足加仓条件"
                result.trade_type = 'add'
                # 计算加仓数量：基于现有持仓数量的比例
                current_quantity = position.get('quantity', 0)
                # 加仓比例：根据ATR比例，最大不超过50%
                add_ratio = min(self.add_atr / self.entry_atr, 0.5)  # 限制最大加仓比例为50%
                # 计算加仓数量
                base_add_quantity = int(current_quantity * add_ratio) // 100 * 100
                # 确保加仓数量至少为100股
                result.buy_quantity = max(base_add_quantity, 100) if current_quantity >= 100 else 100
            
            # 减仓条件：价格下跌
            if latest['low'] <= entry_price - latest['atr']:
                result.is_sell = True
                result.signal_strength = 0.8
                result.message = "满足减仓条件"
                result.trade_type = 'reduce'
                # 计算减仓数量：基于当前持仓数量的比例
                current_quantity = position.get('quantity', 0)
                # 减仓比例：根据ATR比例，最大不超过50%
                reduce_ratio = min(self.reduce_atr / self.entry_atr, 0.5)  # 限制最大减仓比例为50%
                # 计算减仓数量
                result.sell_quantity = int(current_quantity * reduce_ratio) // 100 * 100
                # 确保减仓数量至少为100股
                result.sell_quantity = max(result.sell_quantity, 100) if current_quantity >= 100 else current_quantity
        
        # 填充指标值
        result.indicators['up'] = latest['up'] if pd.notna(latest['up']) else 0
        result.indicators['down'] = latest['down'] if pd.notna(latest['down']) else 0
        result.indicators['atr'] = latest['atr'] if pd.notna(latest['atr']) else 0
        result.indicators['current_price'] = current_price
        result.indicators['hold_return'] = hold_return
        
        return result
    
    def calculate_support(self, df: pd.DataFrame, key_date: Optional[str] = None) -> float:
        """计算海归策略的支撑位
        
        Args:
            df: 股票数据
            key_date: 关键日期
            
        Returns:
            支撑位价格
        """
        df = self.calculate_indicators(df)
        latest = df.iloc[-1]
        
        # 海归策略的支撑位为下线
        if pd.notna(latest['down']):
            return latest['down']
        
        #  fallback: 使用20日均线
        if len(df) >= 20:
            return df['close'].rolling(window=20).mean().iloc[-1]
        
        return 0.0
