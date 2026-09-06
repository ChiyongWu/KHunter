"""
低位海龟策略实现
基于唐奇安通道和ATR的趋势跟踪策略（低位版，去除MA20过滤）
专用于配合低位选股策略（如低位九转）使用：
低位股处于下跌末端，价格常低于MA20，原海龟的MA20强趋势过滤会否决所有信号，
故本策略去除MA20过滤，仅保留突破、上影线、阳线过滤。
"""
import pandas as pd
from trading.turtle_strategy import TurtleStrategy
from typing import Dict, Optional


class LowTurtleStrategy(TurtleStrategy):
    """低位海龟策略（去除MA20过滤）"""

    def __init__(self, config):
        """初始化低位海龟策略

        默认参数：n_entry=1, n_exit=6, atr_period=12（配合低位策略的短线突破）
        其余参数沿用海龟原值。

        Args:
            config: 策略配置
        """
        # 低位版预设：1/6/12
        low_preset = {
            'n_entry': 1,        # 入场通道：1日高点（前一日高点）
            'n_exit': 6,         # 出场通道：6日低点
            'atr_period': 12,    # ATR周期：12日
            'entry_atr': 0.02,   # 入场ATR比例
            'add_atr': 0.5,      # 加仓ATR间隔
            'exit_atr': 2.0,     # ATR止损倍数
        }
        # 合并调用方传入的配置（调用方配置优先）
        merged = dict(low_preset)
        if isinstance(config, dict):
            merged.update(config)
        super().__init__(merged)

    def _check_buy_signal(self, df: pd.DataFrame, signal_bar: pd.Series, latest: pd.Series, use_prev_day_signal: bool = True) -> bool:
        """检查买入信号（低位版，去除MA20过滤）

        Args:
            df: 股票数据
            signal_bar: 信号K线
            latest: 最新K线
            use_prev_day_signal: 是否使用前一天信号

        Returns:
            是否满足买入条件
        """
        # 1. 价格突破上线（使用突破当日的high与up比较）
        if not (pd.notna(signal_bar['up']) and bool(signal_bar['high'] > signal_bar['up'])):
            return False

        # 2. 上影线过滤：上影线不超过4%
        # 上影线 = high - max(open, close)，相对于实体上端计算
        upper_shadow = signal_bar['high'] - max(signal_bar['open'], signal_bar['close'])
        upper_shadow_ratio = upper_shadow / max(signal_bar['open'], signal_bar['close'])
        if upper_shadow_ratio > 0.04:
            return False

        # 3. 阳线过滤：信号发出当日必须是阳线且收盘涨幅 > 0%
        # 找到signal_bar在df中的实际位置
        try:
            signal_bar_idx = df[(df['date'] == signal_bar['date']) & (df['close'] == signal_bar['close'])].index[0]
            # 前一天索引
            prev_day_idx = signal_bar_idx - 1
            if prev_day_idx >= 0:
                prev_close = df['close'].iloc[prev_day_idx]
                is_bullish = bool(signal_bar['close'] > signal_bar['open'])  # 阳线
                is_rising = bool(signal_bar['close'] > prev_close)  # 收盘涨幅>0
                if not (is_bullish and is_rising):
                    return False
        except Exception:
            # 如果无法定位signal_bar，使用固定索引（兼容旧逻辑）
            if use_prev_day_signal:
                prev_close_idx = len(df) - 3
                if prev_close_idx >= 0:
                    prev_close = df['close'].iloc[prev_close_idx]
                    is_bullish = bool(signal_bar['close'] > signal_bar['open'])
                    is_rising = bool(signal_bar['close'] > prev_close)
                    if not (is_bullish and is_rising):
                        return False
            else:
                prev_close_idx = len(df) - 2
                if prev_close_idx >= 0:
                    prev_close = df['close'].iloc[prev_close_idx]
                    is_bullish = bool(signal_bar['close'] > signal_bar['open'])
                    is_rising = bool(signal_bar['close'] > prev_close)
                    if not (is_bullish and is_rising):
                        return False

        # 4. 低位版：去除MA20趋势过滤（低位股常低于MA20，不应被否决）
        # 保留突破+上影线+阳线过滤即可发出买入信号
        return True

    def get_timing_result(self, df: pd.DataFrame, position: Optional[Dict] = None,
                          cash: Optional[float] = None, use_prev_day_signal: bool = True, stock_code: str = "") -> object:
        """获取低位海龟策略择时结果（重写父类以支持低位加仓，去除MA20限制）

        Args:
            df: 股票数据
            position: 持仓信息
            cash: 可用资金
            use_prev_day_signal: 是否使用前一天信号
            stock_code: 股票代码（用于指标缓存隔离）

        Returns:
            择时结果
        """
        from trading.timing_strategies import TimingResult
        result = TimingResult()

        # 计算指标（复用父类，仍计算ma20但不用于过滤）
        df = self.calculate_indicators(df)

        # 获取数据
        latest = df.iloc[-1]  # 最新K线

        # 根据模式确定信号K线和判断逻辑
        if use_prev_day_signal:
            if len(df) < 2:
                return result
            signal_bar = df.iloc[-2]  # T-1日信号K线
            signal_date_offset = 1
        else:
            signal_bar = latest
            signal_date_offset = 0

        # 计算收益率
        entry_price = position['buy_price'] if position else 0
        current_price = latest['close']
        hold_return = (current_price - entry_price) / entry_price if entry_price > 0 else 0

        # === 卖出条件（优先判断，复用父类逻辑） ===
        if position and len(df) >= 2:
            is_sell, reason = self._check_sell_signal(df, signal_bar, latest, entry_price, use_prev_day_signal)
            if is_sell:
                result.is_sell = True
                result.signal_strength = 1.0
                result.message = f"T-{signal_date_offset}日{reason}，卖出信号" if signal_date_offset else f"今日{reason}，卖出信号"
                result.trade_type = 'sell'
                result.sell_quantity = position.get('quantity', 0)

        # === 买入条件（低位版，无MA20限制） ===
        if not position and not result.is_sell:
            if len(df) >= 2:
                if self._check_buy_signal(df, signal_bar, latest, use_prev_day_signal):
                    buy_price = latest['open']
                    result.is_buy = True
                    result.signal_strength = 1.0
                    result.message = f"T-{signal_date_offset}日突破上线 {signal_bar['up']:.2f}，低位买入信号" if signal_date_offset else f"今日突破上线 {signal_bar['up']:.2f}，低位买入信号"
                    result.support_level = signal_bar['up'] * 0.95
                    result.trade_type = 'buy'
                    buy_amount = self.base_position_amount
                    result.buy_quantity = max(int(buy_amount / buy_price) // 100 * 100, 100)

        # === 加仓/减仓信号（低位版，去除MA20限制） ===
        if position and not result.is_sell:
            current_quantity = position.get('quantity', 0)
            add_count = position.get('add_count', 0)
            max_additions = 4

            if add_count < max_additions:
                profit_ratio = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                if profit_ratio > 0.02:  # 盈利超过2%
                    last_add_price = position.get('last_add_price') or entry_price
                    add_threshold = last_add_price + self.add_atr * latest['atr']

                    if latest['high'] >= add_threshold:
                        # 低位版加仓：去掉MA20限制，仅保留阳线+涨幅+上影线
                        prev_close = df['close'].iloc[-2] if len(df) >= 2 else latest['close']
                        is_bullish = latest['close'] > latest['open']
                        is_rising = latest['close'] > prev_close

                        # 检查上影线
                        upper_shadow = latest['high'] - max(latest['open'], latest['close'])
                        upper_shadow_ratio = upper_shadow / max(latest['open'], latest['close']) if max(latest['open'], latest['close']) > 0 else 0
                        upper_shadow_ok = upper_shadow_ratio <= 0.04

                        # 阳线 + 涨幅>0 + 上影线<4%（无MA20限制）
                        if is_bullish and is_rising and upper_shadow_ok:
                            result.is_buy = True
                            result.signal_strength = 0.8
                            result.message = f"加仓#{add_count + 1}，突破{add_threshold:.2f}"
                            result.trade_type = 'add'
                            result.add_count = add_count + 1
                            result.indicators['last_add_price'] = latest['close']
                            add_ratio = 1.0 / (add_count + 2)
                            add_quantity = int(current_quantity * add_ratio) // 100 * 100
                            result.buy_quantity = max(add_quantity, 100)

        # 填充指标值
        result.indicators['up'] = latest['up'] if pd.notna(latest['up']) else 0
        result.indicators['down'] = latest['down'] if pd.notna(latest['down']) else 0
        result.indicators['atr'] = latest['atr'] if pd.notna(latest['atr']) else 0
        result.indicators['ma20'] = latest['ma20'] if pd.notna(latest['ma20']) else 0
        result.indicators['current_price'] = current_price
        result.indicators['hold_return'] = hold_return

        return result

    def calculate_support(self, df: pd.DataFrame, key_date: Optional[str] = None) -> float:
        """计算低位海龟策略的支撑位（去除MA20 fallback，改用N日低点）

        Args:
            df: 股票数据
            key_date: 关键日期

        Returns:
            支撑位价格
        """
        df = self.calculate_indicators(df)
        latest = df.iloc[-1]

        # 优先使用下线作为支撑位
        if pd.notna(latest['down']):
            return latest['down']

        # 低位版 fallback：使用近N日最低价（n_exit日内），不依赖MA20
        if len(df) >= self.n_exit:
            return df['low'].tail(self.n_exit).min()

        # 极短数据兜底：取全部最低价
        if len(df) >= 1:
            return df['low'].min()

        return 0.0
