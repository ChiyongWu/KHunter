# -*- coding: utf-8 -*-
"""主升低吸策略 - 识别"一拉 → 二调 → 长阳贴紧突破回调中最近反弹高点 R"的结构性买点

来源：`doc/结构买点规则_一拉二调突破R.md`
说明书：`strategy/spec/主升低吸策略说明书.md`

结构定义（五要素，全部满足才出信号；**选股日 = 突破日**，不含回踩 / 回看环节）：

- **C1 一拉**：在近 recent_days(60) 交易日内，对 swing high **按高度由高到低**扫描，
  取**第一个**满足涨幅区间 [rally_min_pct(30%), rally_max_pct(100%)] 的高点作为 H
  （L = H 前 rally_low_lookback(45) 日内的最低价）。
  涨幅不达标的高点**跳过、继续往下找**；窗口内全部候选都不满足 → 该股票无信号。
- **C2 二调**：H 之后最低价 P 的回撤 ∈ [pullback_min_pct(18%), 一拉涨幅 − pullback_gap_pct(8%))；
  且要求 **P > L**（回调低点不得跌破起始低点，即一拉不得被全额回吐）。
- **C3 定义 R**：H 之后、今日之前的**最后一个** swing high（R < H；**允许 R 早于 P**）。
- **C4 突破**：今日阳线实体 ≥ min_body_pct(5%)；收盘 > R 且突破幅度 ≤ breakout_max_pct(2%)；
  量比 ≥ volume_ratio(1.8)。
- **C5 均线**：今日收盘 > MA(ma_period=5)（含今日收盘）。

其他约定：
- **不做冷却去重**：同一结构在相邻交易日可连续触发（如需去重由引擎 / 运行器在信号层处理）。
- **无未来函数**：swing high 的右侧 swing_window 根 K 线必须已出现，信号日收盘后即可判定。
- **数据方向**：内部统一转为正序（最旧在前）处理，不依赖调用方传入顺序。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from strategy.base_strategy import BaseStrategy


class MainUptrendDipBuyStrategy(BaseStrategy):
    """主升低吸策略 - 主升途中回调结束后贴紧 R 的放量长阳突破"""

    def __init__(self, params=None):
        """初始化策略参数"""
        default_params = {
            # ---- C1 一拉 ----
            'rally_min_pct': 30.0,        # 一拉涨幅下限（%）
            'rally_max_pct': 100.0,       # 一拉涨幅上限（%）；0 = 不限制
            'rally_low_lookback': 45,     # L 取值窗口：H 之前多少个交易日内取最低价
            'recent_days': 60,            # "近期"的定义：H 只在该窗口内寻找
            # ---- C2 二调 ----
            'pullback_min_pct': 18.0,     # 二调回撤下限（%）
            'pullback_gap_pct': 8.0,      # 二调回撤动态上限 = 一拉涨幅 − 该值（%）；0 = 关闭
            'require_p_above_l': True,    # 二调低点 P 必须高于起始低点 L（不得全额回吐一拉）
            # ---- C3 swing high ----
            'swing_window': 2,            # swing high：前后各 N 根最高价均更低
            # ---- C4 突破 ----
            'min_body_pct': 5.0,          # 突破日阳线实体下限（%）
            'breakout_max_pct': 2.0,      # 突破 R 的幅度上限（%）；核心参数
            'volume_ratio': 1.8,          # 量比下限
            'volume_ma_period': 5,        # 量比均量周期（不含今日）
            # ---- C5 均线 ----
            'require_close_above_ma': True,   # 是否要求收盘站上均线
            'ma_period': 5,                   # 均线周期
            # ---- 其他 ----
            'min_data_len': 106,          # 最小 K 线长度（= recent_days + rally_low_lookback + 1）
        }
        if params:
            default_params.update(params)

        super().__init__("主升低吸策略", default_params)

    # ------------------------------------------------------------------ #
    # 数据方向
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_ascending(df: pd.DataFrame) -> pd.DataFrame:
        """统一转为正序（最旧在前、最新在后），自动识别方向"""
        if df is None or df.empty or 'date' not in df.columns:
            return df
        d = df.reset_index(drop=True)
        if len(d) > 1 and str(d['date'].iloc[0]) > str(d['date'].iloc[-1]):
            d = d.iloc[::-1].reset_index(drop=True)
        return d

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #
    def calculate_indicators(self, df) -> pd.DataFrame:
        """计算指标（始终返回**正序**）"""
        result = self._to_ascending(df).copy()

        ma_period = int(self.params['ma_period'])
        vol_period = int(self.params['volume_ma_period'])

        # 均线（含当日收盘）
        result['ma'] = result['close'].rolling(
            window=ma_period, min_periods=1).mean()
        # 量比均量（不含当日，用于展示）
        result['vol_ma'] = result['volume'].shift(1).rolling(
            window=vol_period, min_periods=1).mean()

        return result

    # ------------------------------------------------------------------ #
    # 条件描述
    # ------------------------------------------------------------------ #
    def get_selection_criteria(self):
        p = self.params
        criteria = [
            f"1. 一拉：H = 近 {int(p['recent_days'])} 日内的 swing high 按高度由高到低扫描，"
            f"取第一个涨幅 ∈ [{p['rally_min_pct']}%, {p['rally_max_pct']}%] 者"
            f"（L = H 前 {int(p['rally_low_lookback'])} 日最低价；不达标的高点跳过）",
            f"2. 二调：H 之后最低点回撤 ∈ [{p['pullback_min_pct']}%, "
            f"一拉涨幅 − {p['pullback_gap_pct']}%)"
            + ("，且回调低点 > 起始低点 L" if p.get('require_p_above_l', True) else ""),
            f"3. R = H 之后、今日之前的最后一个 swing high（R < H，允许 R 早于 P）",
            f"4. 突破：阳线实体 ≥ {p['min_body_pct']}%、收盘 > R 且突破幅度 ≤ "
            f"{p['breakout_max_pct']}%、量比 ≥ {p['volume_ratio']}",
        ]
        if p.get('require_close_above_ma', True):
            criteria.append(f"5. 均线：选股日收盘价 > MA{int(p['ma_period'])}")
        criteria.append("6. 选股日 = 突破日（当日判定，不含回踩环节）")
        return criteria

    # ------------------------------------------------------------------ #
    # swing high 识别
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_swing_highs(highs: np.ndarray, window: int) -> list:
        """返回 swing high 的正序下标列表

        判定：``high[i]`` 严格大于左侧 window 根与右侧 window 根的最高价。
        右侧 window 根必须已存在（``i + window <= n - 1``）→ 无未来函数。
        """
        w = max(1, int(window))
        n = len(highs)
        out = []
        for i in range(w, n - w):
            h = highs[i]
            if h > highs[i - w:i].max() and h > highs[i + 1:i + w + 1].max():
                out.append(i)
        return out

    # ------------------------------------------------------------------ #
    # 快速过滤
    # ------------------------------------------------------------------ #
    def quick_filter(self, df) -> bool:
        """快速过滤：最新一日是否为大阳线（实体 ≥ min_body_pct）

        与 `select_stocks` 的 C4 一致，内部自动识别数据方向。
        """
        if df is None or df.empty or len(df) < 2:
            return False
        d = self._to_ascending(df)
        try:
            last = d.iloc[-1]
            o = float(last['open'])
            c = float(last['close'])
            if o <= 0:
                return False
            return bool((c - o) / o * 100 >= float(self.params['min_body_pct']))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 选股主逻辑
    # ------------------------------------------------------------------ #
    def select_stocks(self, df, stock_name='') -> list:
        """选股逻辑：**选股日 = 突破日**"""
        if df is None or df.empty:
            return []

        p = self.params
        try:
            d = self._to_ascending(df).reset_index(drop=True)

            recent = int(p['recent_days'])
            lb_low = int(p['rally_low_lookback'])
            sw = int(p['swing_window'])
            min_len = max(int(p.get('min_data_len', 0) or 0), recent + lb_low + 1)
            if len(d) < min_len:
                return []

            highs = d['high'].astype(float).values
            lows = d['low'].astype(float).values
            closes = d['close'].astype(float).values
            opens = d['open'].astype(float).values
            vols = d['volume'].astype(float).values
            dates = d['date'].astype(str).values

            n = len(d)
            last = n - 1                      # 今日（= 选股日 = 突破日）

            # ---- 0. swing high 序列 ----
            swing_highs = self._find_swing_highs(highs, sw)
            if not swing_highs:
                return []

            # ---- C1 一拉：H = 窗口内"由高到低"扫描，取第一个满足涨幅区间者 ----
            #   不符合 H 定义的高点（一拉涨幅不在区间内）直接跳过、继续找下一个；
            #   窗口内全部候选都不满足 → 该股票无信号。
            win_lo = last - recent
            cand = [i for i in swing_highs if win_lo <= i <= last - 1]
            if not cand:
                return []

            rally_min = float(p['rally_min_pct'])
            rally_max = float(p.get('rally_max_pct', 0) or 0)

            hi = None
            H = L = 0.0
            l_idx = 0
            rally_pct = 0.0
            for i in sorted(cand, key=lambda x: (-float(highs[x]), -x)):
                if i - lb_low < 0:            # L 的窗口不完整 → 该候选不可用
                    continue
                seg = lows[i - lb_low:i]      # H 前 lb_low 个交易日
                lo = float(seg.min())
                if lo <= 0:
                    continue
                rp = (float(highs[i]) - lo) / lo * 100
                if rp < rally_min:
                    continue                  # 涨幅不足 → 跳过该高点
                if rally_max > 0 and rp > rally_max:
                    continue                  # 涨幅过大 → 跳过该高点
                hi = i
                H = float(highs[i])
                l_idx = i - lb_low + int(np.argmin(seg))
                L = lo
                rally_pct = rp
                break
            if hi is None:
                return []

            # ---- C2 二调 ----
            if hi >= last:                    # H 即今日 → 无回调
                return []
            p_slice = lows[hi + 1:last + 1]   # H 之后至今日
            p_idx = hi + 1 + int(np.argmin(p_slice))
            P = float(p_slice.min())
            if P <= 0:
                return []
            pullback_pct = (H - P) / H * 100
            if pullback_pct < float(p['pullback_min_pct']):
                return []
            # 回调低点不得跌破起始低点 L（否则一拉被全额回吐，非"二调"而是趋势走坏）
            # 例：600546 山煤国际 2026-08-06（一拉 44.4%、回调 31.3%，P=10.50 < L=10.59）被此条排除
            if p.get('require_p_above_l', True) and P <= L:
                return []
            gap = float(p.get('pullback_gap_pct', 0) or 0)
            if gap > 0 and pullback_pct >= rally_pct - gap:
                return []

            # ---- C3 定义 R：H 之后、今日之前的最后一个 swing high ----
            after = [i for i in swing_highs if hi < i <= last - 1]
            if not after:
                return []
            ri = max(after)
            R = float(highs[ri])
            if R <= 0 or R >= H:
                return []

            # ---- C4 突破 ----
            C = float(closes[last])
            O = float(opens[last])
            V = float(vols[last])
            if O <= 0 or C <= 0 or V <= 0:
                return []
            if not np.isfinite([C, O, V]).all():
                return []

            body_pct = (C - O) / O * 100
            if body_pct < float(p['min_body_pct']):
                return []

            if C <= R:
                return []
            breakout_pct = (C - R) / R * 100
            if breakout_pct > float(p['breakout_max_pct']):
                return []

            vmp = int(p['volume_ma_period'])
            if last - vmp < 0:
                return []
            vol_ma = float(vols[last - vmp:last].mean())
            if vol_ma <= 0:
                return []
            vr = V / vol_ma
            if vr < float(p['volume_ratio']):
                return []

            # ---- C5 均线 ----
            ma_n = None
            if p.get('require_close_above_ma', True):
                mp = int(p['ma_period'])
                if last - mp + 1 < 0:
                    return []
                ma_n = float(closes[last - mp + 1:last + 1].mean())
                if C <= ma_n:
                    return []

            # ---- 生成信号 ----
            key_date = str(dates[last])[:10]
            h_date = str(dates[hi])[:10]
            l_date = str(dates[l_idx])[:10]
            p_date = str(dates[p_idx])[:10]
            r_date = str(dates[ri])[:10]
            gap_txt = f"且 < 一拉涨幅-{gap}%" if gap > 0 else "（无动态上限）"

            reasons = [
                f"一拉：{h_date} 高 {H:.2f} 较前 {lb_low} 日低点 {L:.2f} 上涨 "
                f"{rally_pct:.1f}%（∈[{rally_min}%, {rally_max}%]）",
                f"二调：{h_date} → {p_date} 低 {P:.2f}，回撤 {pullback_pct:.1f}%"
                f"（≥{p['pullback_min_pct']}% {gap_txt}）"
                + (f"，未跌破起始低点 {L:.2f}"
                   if p.get('require_p_above_l', True) else ""),
                f"R={R:.2f}（{r_date} 反弹高点，晚于 H、早于今日）",
                f"突破：收盘 {C:.2f} > R {R:.2f}，幅度 +{breakout_pct:.1f}%"
                f"（≤{p['breakout_max_pct']}%），实体 +{body_pct:.1f}%，量比 {vr:.2f}",
            ]
            if ma_n is not None:
                reasons.append(f"均线：收盘 {C:.2f} > MA{int(p['ma_period'])} {ma_n:.2f}")

            return [{
                'code': '',
                'name': stock_name,
                'key_date': key_date,               # 信号日 = 突破日
                'key_date_type': '一拉二调突破R日',
                'price': round(C, 2),
                'volume_ratio': round(vr, 2),
                'reasons': reasons,
                'strategy_type': 'MainUptrendDipBuyStrategy',
                # ---- 结构详情 ----
                'rally_high': round(H, 2),
                'rally_high_date': h_date,
                'rally_low': round(L, 2),
                'rally_low_date': l_date,
                'rally_pct': round(rally_pct, 2),
                'rally_days': int(hi - l_idx),
                'pullback_low': round(P, 2),
                'pullback_low_date': p_date,
                'pullback_pct': round(pullback_pct, 2),
                'pullback_days': int(p_idx - hi),
                'r_price': round(R, 2),
                'r_date': r_date,
                'body_pct': round(body_pct, 2),
                'breakout_pct': round(breakout_pct, 2),
                'days_since_high': int(last - hi),
                'ma_value': round(ma_n, 2) if ma_n is not None else None,
                'days_since_breakout': 0,          # 突破日即为选股日
            }]
        except Exception:
            return []
