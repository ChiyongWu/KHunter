"""
趋势回调缩量择时策略

策略定位：弥补海龟（突破买入）在震荡市中频繁假突破的问题，改为顺势回调低吸。

买入（首次建仓，以下条件同时满足）：
  B1 上升趋势：收盘价 >= MA10 且 近20日收盘线性回归 slope > 0 且 R² >= 0.3
              （口径与"股票池检查上升趋势"规则一致）
  B2 乖离率  ：0 < BIAS5 < 3（回调到位、未追高）
  B3 缩量    ：当日成交量 < 5日均量
  B4 BIAS10  ：> 2.0（规避已跌到MA10附近的走弱股；设为 None 关闭）
  B5 回撤    ：距20日高点回撤 <= 7%（max_dd_from_high20=-7.0；设为 None 关闭）

加仓（已有持仓）：
  A1 盈利门槛：信号日收盘相对持仓成本盈利 > 2%（沿用海龟）
  A2 其余条件与首次买入一致：B1 + B2 + B3
  A3 加仓比例：第 n 次 = 当前持仓量 × 1/(n+1)（1/2、1/3、1/4、1/5，与海龟一致）
  A4 上限    ：最多加仓 4 次（与海龟 max_additions=4 一致）

卖出（满足任一，清仓）：
  S1 收盘价跌破近 10 日最低价（不含当日，与唐奇安下线 .shift(1) 口径一致）
  S2 BIAS5 < -3.5
  （原为 6 / -5.0：破位点距买入点过近，31 笔"刚买就破位"出局；
    调优为 10 / -3.5 后模拟净盈亏 37k → 96k，8/10/12 邻域均 90k~101k）

回测 / 实盘模式（与海龟完全一致）：
  use_prev_day_signal=True （回测）  ：signal_bar = df.iloc[-2]（T-1），以 T 日开盘价成交
  use_prev_day_signal=False（实盘）  ：signal_bar = df.iloc[-1]（T   ），以 T 日收盘价测算、T+1 执行

前视偏差：所有判断仅使用 signal_bar 及其之前的数据，禁止使用 latest（T 日）。
"""
import logging

import numpy as np
import pandas as pd
from scipy import stats

from trading.timing_strategies import TimingStrategy, TimingResult
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class UptrendPullbackStrategy(TimingStrategy):
    """趋势回调缩量策略（顺势低吸 + 金字塔加仓）"""

    # 默认参数（均可通过 config 覆盖）
    DEFAULT_CONFIG = {
        # 趋势（与股票池规则一致）
        'trend_ma': 10,           # 趋势均线 MA10
        'trend_lookback': 20,     # 线性回归窗口
        'r_squared_min': 0.3,     # 拟合度下限

        # 买入
        'bias5_buy_low': 0.0,     # BIAS5 下界（不含）
        'bias5_buy_high': 3.0,    # BIAS5 上界（不含）
        'vol_ma_period': 5,       # 均量周期
        
        # 规避"回调转下跌"（经 117 笔样本特征分析后新增；设为 None 可关闭该过滤）
        # bias10_min：买入要求 BIAS10 > 该值 —— 真正的强势回调应明显高于10日线，
        #             已跌到MA10附近的往往是趋势走弱（大赚组均值+7.15% vs 大亏组+2.31%）
        'bias10_min': 2.0,

        # max_dd_from_high20：距20日高点回撤上限（负值为回撤幅度）
        #             回撤过深（>7%）明显更容易转为下跌
        # 设为 None 可关闭该过滤（B5）
        'max_dd_from_high20': -7.0,

        # 加仓（与海龟一致）
        'max_add_count': 4,                 # 最多加仓次数
        'add_profit_min': 0.02,             # 加仓盈利门槛基数（第1次加仓要求盈利 > 2%）
        'add_profit_progressive': True,     # 递进门槛：第n次加仓要求盈利 > 基数 × n
                                            # 即 第1次>2% 第2次>4% 第3次>6% 第4次>8%
        'min_add_quantity': 100,            # 加仓最小手数（与海龟 max(...,100) 一致）

        # 卖出
        # break_days / bias5_sell 经回测明细（result_id=640，117笔）参数敏感性分析后调优：
        #   原值 6 / -5.0 → 破位点距买入点过近（策略买在回调缩量处），
        #   导致 31 笔"刚买就破位"出局（平均 -4.10%、胜率仅 6.5%）。
        #   调整为 10 / -3.5 后，模拟净盈亏 37k → 96k（+158%），
        #   且 8/10/12 邻域均在 90k~101k，属参数平原而非过拟合单点。
        'break_days': 10,         # 破位参考天数（不含当日）
        'bias5_sell': -3.5,       # 超跌阈值

        # 仓位
        'base_position_amount': 20000,      # 首仓金额（元）
    }

    def __init__(self, config):
        super().__init__(config)
        cfg = dict(self.DEFAULT_CONFIG)
        cfg.update(self.config or {})
        self.cfg = cfg

        self.trend_ma = int(cfg['trend_ma'])
        self.trend_lookback = int(cfg['trend_lookback'])
        self.r_squared_min = float(cfg['r_squared_min'])
        self.bias5_buy_low = float(cfg['bias5_buy_low'])
        self.bias5_buy_high = float(cfg['bias5_buy_high'])
        self.vol_ma_period = int(cfg['vol_ma_period'])
        self.bias10_min = cfg.get('bias10_min', 2.0)
        self.max_dd_from_high20 = cfg.get('max_dd_from_high20', -7.0)
        self.max_add_count = int(cfg['max_add_count'])
        self.add_profit_min = float(cfg['add_profit_min'])
        self.add_profit_progressive = bool(cfg.get('add_profit_progressive', True))
        self.min_add_quantity = int(cfg['min_add_quantity'])
        self.break_days = int(cfg['break_days'])
        self.bias5_sell = float(cfg['bias5_sell'])
        self.base_position_amount = float(cfg['base_position_amount'])

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算策略指标（内部统一按日期正序，最新在最后）"""
        result = df.copy()

        # 确保数据按日期正序排列（项目数据为倒序，最新在前）
        if len(result) > 1 and result['date'].iloc[0] > result['date'].iloc[1]:
            result = result.iloc[::-1].reset_index(drop=True)

        # 均线 / 乖离率
        result['ma5'] = result['close'].rolling(window=5).mean()
        result['ma10'] = result['close'].rolling(window=self.trend_ma).mean()
        ma5 = result['ma5'].replace(0, np.nan)
        result['bias5'] = (result['close'] - ma5) / ma5 * 100

        # 均量
        result['vol_ma'] = result['volume'].rolling(window=self.vol_ma_period).mean()

        # 近 N 日最低价（不含当日，与海龟唐奇安下线 .shift(1) 口径一致）
        result['break_low'] = result['low'].rolling(window=self.break_days).min().shift(1)

        return result

    # ------------------------------------------------------------------
    # 条件判断
    # ------------------------------------------------------------------
    def _check_uptrend(self, df: pd.DataFrame, idx: int) -> Tuple[bool, Dict]:
        """上升趋势判断：收盘价 >= MA10 且 slope > 0 且 R² >= r_squared_min

        Args:
            df: 已正序化的K线
            idx: 信号K线在 df 中的位置（iloc 下标，含该K线参与计算）

        Returns:
            (是否上升, 明细)
        """
        lookback = self.trend_lookback
        if idx + 1 < max(lookback, self.trend_ma):
            return False, {'reason': f'数据不足（需{max(lookback, self.trend_ma)}日）'}

        close = float(df['close'].iloc[idx])
        ma_n = float(df['close'].iloc[idx + 1 - self.trend_ma: idx + 1].mean())

        prices = df['close'].iloc[idx + 1 - lookback: idx + 1].values.astype(float)
        x = np.arange(len(prices))
        slope, _, r_value, _, _ = stats.linregress(x, prices)
        r_squared = r_value ** 2

        passed = (close >= ma_n) and (slope > 0) and (r_squared >= self.r_squared_min)
        detail = {
            'close': close,
            f'ma{self.trend_ma}': ma_n,
            'slope': slope,
            'r_squared': r_squared,
            'passed': passed,
        }
        return passed, detail

    def _check_buy_conditions(self, df: pd.DataFrame, idx: int) -> Tuple[bool, Dict]:
        """买入三条件：上升趋势 + 0<BIAS5<上界 + 缩量"""
        trend_ok, trend_detail = self._check_uptrend(df, idx)
        bar = df.iloc[idx]

        bias5 = bar.get('bias5')
        vol_ma = bar.get('vol_ma')
        volume = bar.get('volume')

        bias_ok = pd.notna(bias5) and (self.bias5_buy_low < float(bias5) < self.bias5_buy_high)
        vol_ok = (pd.notna(vol_ma) and pd.notna(volume)
                  and float(volume) < float(vol_ma))

        # ========== 规避"回调转下跌"：BIAS10 下限 + 20日高点回撤上限 ==========
        # 仅在数据充足（>=20根）时校验；样本不足时放行（由趋势检查统一把关）
        bias10 = None
        dd_from_high20 = None
        close_val = float(bar['close']) if pd.notna(bar.get('close')) else None
        if close_val and close_val > 0 and idx >= 19:
            ma10 = float(df.iloc[idx - 9: idx + 1]['close'].mean())
            if ma10 > 0:
                bias10 = (close_val - ma10) / ma10 * 100
            high20 = float(df.iloc[idx - 19: idx + 1]['high'].max())
            if high20 > 0:
                dd_from_high20 = (close_val - high20) / high20 * 100

        # 未配置（None）时不启用对应过滤
        bias10_ok = True
        if self.bias10_min is not None and bias10 is not None:
            bias10_ok = bias10 > self.bias10_min
        dd_ok = True
        if self.max_dd_from_high20 is not None and dd_from_high20 is not None:
            dd_ok = dd_from_high20 >= self.max_dd_from_high20

        detail = {
            'trend_ok': trend_ok,
            'trend': trend_detail,
            'bias5': float(bias5) if pd.notna(bias5) else None,
            'bias_ok': bool(bias_ok),
            'volume': float(volume) if pd.notna(volume) else None,
            'vol_ma': float(vol_ma) if pd.notna(vol_ma) else None,
            'vol_ok': bool(vol_ok),
            'bias10': bias10,
            'bias10_ok': bool(bias10_ok),
            'dd_from_high20': dd_from_high20,
            'dd_ok': bool(dd_ok),
        }
        return bool(trend_ok and bias_ok and vol_ok and bias10_ok and dd_ok), detail

    def _check_sell_signal(self, df: pd.DataFrame, idx: int) -> Tuple[bool, str]:
        """卖出：收盘价跌破近 N 日最低价（不含当日）或 BIAS5 < 阈值"""
        bar = df.iloc[idx]
        break_low = bar.get('break_low')
        if pd.notna(break_low) and float(bar['close']) < float(break_low):
            return True, f"收盘价{float(bar['close']):.2f}跌破近{self.break_days}日最低价{float(break_low):.2f}"

        bias5 = bar.get('bias5')
        if pd.notna(bias5) and float(bias5) < self.bias5_sell:
            return True, f"BIAS5={float(bias5):.2f}<{self.bias5_sell}"

        return False, ""

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def get_timing_result(self, df: pd.DataFrame, position: Optional[Dict] = None,
                          cash: Optional[float] = None, use_prev_day_signal: bool = True,
                          stock_code: str = "") -> TimingResult:
        """获取择时结果（与海龟同构：卖出优先 → 加仓 → 首仓）"""
        result = TimingResult()

        if df is None or len(df) < 2:
            return result

        df = self.calculate_indicators(df)
        latest = df.iloc[-1]

        # 模式分支（与海龟一致）
        if use_prev_day_signal:
            # 回测：T-1 信号K线 + T 日开盘成交
            if len(df) < 3:
                return result
            signal_idx = len(df) - 2
            signal_date_offset = 1
        else:
            # 实盘：T 日信号K线 + T 日收盘测算（T+1 执行）
            signal_idx = len(df) - 1
            signal_date_offset = 0

        signal_bar = df.iloc[signal_idx]
        # 买卖测算价口径与海龟一致
        ref_price = float(latest['open']) if use_prev_day_signal else float(latest['close'])
        prefix = f"T-{signal_date_offset}日" if signal_date_offset else "今日"

        # 填充指标（便于日志/排查）
        for key in ('ma5', 'ma10', 'bias5', 'vol_ma', 'break_low'):
            val = signal_bar.get(key)
            result.indicators[key] = float(val) if pd.notna(val) else 0.0
        result.indicators['signal_close'] = float(signal_bar['close'])
        result.indicators['ref_price'] = ref_price

        # ========== 卖出（优先判断） ==========
        if position:
            is_sell, reason = self._check_sell_signal(df, signal_idx)
            if is_sell:
                result.is_sell = True
                result.signal_strength = 1.0
                result.trade_type = 'sell'
                result.sell_quantity = position.get('quantity', 0)
                result.message = f"{prefix}{reason}，卖出信号"
                return result

        # ========== 加仓 ==========
        if position:
            current_quantity = int(position.get('quantity', 0) or 0)
            add_count = int(position.get('add_count', 0) or 0)
            entry_price = float(position.get('buy_price', 0) or 0)

            if add_count >= self.max_add_count:
                return result

            # A1 盈利门槛（递进式）：第 n 次加仓要求盈利 > add_profit_min × n
            #    第1次 >2%，第2次 >4%，第3次 >6%，第4次 >8%
            #    关闭递进（add_profit_progressive=False）时，所有加仓均要求 > add_profit_min
            signal_close = float(signal_bar['close'])
            profit_ratio = (signal_close - entry_price) / entry_price if entry_price > 0 else 0.0
            add_seq = add_count + 1
            profit_threshold = (self.add_profit_min * add_seq
                                if self.add_profit_progressive else self.add_profit_min)
            result.indicators['add_profit_threshold'] = profit_threshold
            if profit_ratio <= profit_threshold:
                result.indicators['add_profit_ratio'] = profit_ratio
                return result

            # A2 其余条件与首次买入一致
            buy_ok, detail = self._check_buy_conditions(df, signal_idx)
            result.indicators.update({f'add_{k}': v for k, v in detail.items()})
            result.indicators['add_profit_ratio'] = profit_ratio
            if not buy_ok:
                return result

            # A3 加仓比例：与海龟一致（基准=当前持仓量，1/(n+1)）
            add_ratio = 1.0 / (add_count + 2)
            add_quantity = int(current_quantity * add_ratio) // 100 * 100
            add_quantity = max(add_quantity, self.min_add_quantity)

            result.is_buy = True
            result.signal_strength = 0.8
            result.trade_type = 'add'
            # add_count = 加仓后的累计总次数（首次加仓=1，上限4），
            # 与 position['add_count'] 语义一致（回测引擎/实盘运行器据此跟踪加仓进度）
            result.add_count = add_count + 1
            result.buy_quantity = add_quantity
            result.message = (f"{prefix}加仓#{add_count + 1}（盈利{profit_ratio * 100:.1f}%"
                              f">门槛{profit_threshold * 100:.1f}%，"
                              f"数量{add_quantity}股）")
            result.indicators['last_add_price'] = signal_close
            return result

        # ========== 首次建仓 ==========
        buy_ok, detail = self._check_buy_conditions(df, signal_idx)
        result.indicators.update(detail)
        if buy_ok and ref_price > 0:
            result.is_buy = True
            result.signal_strength = 1.0
            result.trade_type = 'buy'
            result.buy_quantity = max(int(self.base_position_amount / ref_price) // 100 * 100, 100)
            result.message = (f"{prefix}上升趋势回调缩量（BIAS5={detail.get('bias5'):.2f}，"
                              f"量比={detail.get('volume')}/{detail.get('vol_ma'):.0f}），买入信号")

        return result

    def get_hunting_result(self, df: pd.DataFrame, position: Optional[Dict] = None,
                           cash: Optional[float] = None) -> TimingResult:
        """狩猎场/实盘模式：T 日信号"""
        return self.get_timing_result(df, position, cash, use_prev_day_signal=False)

    def calculate_support(self, df: pd.DataFrame, key_date: Optional[str] = None) -> float:
        """支撑位：近 N 日最低价（不含当日）"""
        df = self.calculate_indicators(df)
        if len(df) == 0:
            return 0.0
        val = df.iloc[-1].get('break_low')
        return float(val) if pd.notna(val) else 0.0
