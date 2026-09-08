"""
低位海龟策略实现
基于唐奇安通道和ATR的趋势跟踪策略（低位版，去除MA20过滤）
专用于配合低位选股策略（如低位九转）使用：
低位股处于下跌末端，价格常低于MA20，原海龟的MA20强趋势过滤会否决所有信号，
故本策略去除MA20过滤，仅保留突破、上影线、阳线过滤。

实现说明（重构）：
  早期版本整体重写了 get_timing_result 与 _check_buy_signal，与父类
  TurtleStrategy 存在约 100 行逻辑重复，导致父类的前视偏差修复、加仓规则
  调整无法自动同步（例如父类已移除 same_row 查找，子类仍保留旧实现，
  存在 date+close 匹配失败与父子行为不一致的风险）。

  现改为：仅通过 USE_MA_FILTER=False 关闭 MA20 过滤、SIGNAL_LABEL 标识文案，
  其余买入/卖出/加仓/指标填充逻辑全部复用父类实现，保证：
    1. 父子行为一致，父类修复自动继承；
    2. 消除重复代码与维护遗漏风险。
"""
import pandas as pd
from trading.turtle_strategy import TurtleStrategy
from typing import Optional


class LowTurtleStrategy(TurtleStrategy):
    """低位海龟策略（去除MA20过滤，其余逻辑完全复用父类）"""

    # 关闭 MA20 趋势过滤：低位股常低于 MA20，启用会否决所有信号
    USE_MA_FILTER = False

    # 买入信号文案标识，便于日志/前端区分信号来源
    SIGNAL_LABEL = '低位'

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

    def calculate_support(self, df: pd.DataFrame, key_date: Optional[str] = None) -> float:
        """计算低位海龟策略的支撑位（去除MA20 fallback，改用N日低点）

        与父类差异：父类 fallback 用 MA20，低位股不适用，故改用 n_exit 日最低价。

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
