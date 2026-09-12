# -*- coding: utf-8 -*-
"""阻力位突破策略 - 识别"窄幅震荡后放量长阳突破平台高点"的信号

策略原理（2026-09-12 按需求重构）：
1. **突破日即为选股日**：只在"今天"判定，不再回看最近 N 日（不再有回踩环节）
2. **平台条件**：前 70 日**窄幅震荡**——区间最高价与最低价的距离（振幅）≤ 25%
3. **突破条件**：今日**收盘 > 前 70 日最高价**（严格 100% 突破）
4. **力度条件**：今日涨幅 > 5%
5. **量能条件**：今日成交量 ≥ 前 5 日均量 × 1.8（放量确认）
6. **高点陈旧**：平台最高点（阻力位）距今 ≥ 10 个交易日——排除"近日已冲高/已突破"的股票

选股条件（全部满足）：
- C0 高点陈旧：平台最高点距今 ≥ min_peak_days(10) 个交易日
- C1 平台振幅：前 lookback_days(70) 日 (最高价 - 最低价) / 最低价 ≤ max_range_pct(25%)
- C2 突破：今日收盘价 > 前 lookback_days(70) 日最高价（100% 突破）
- C3 涨幅：今日涨幅 > min_change_pct(5%)
- C4 放量：今日成交量 ≥ 前 volume_ma_period(5) 日均量 × volume_ratio(1.8)
- C5 均线多头排列（可选，默认关闭）：MA5 > MA10 > MA20

⚠️ 数据方向：本策略的 `select_stocks` 统一按 date 升序（最旧在前、最新在后）处理，
不依赖调用方传入的顺序；`calculate_indicators` 始终返回正序。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from strategy.base_strategy import BaseStrategy


class ResistanceBreakoutStrategy(BaseStrategy):
    """阻力位突破策略 - 窄幅震荡平台 + 今日放量长阳突破"""

    def __init__(self, params=None):
        """初始化策略参数"""
        default_params = {
            'lookback_days': 70,          # 平台（阻力位）回溯天数（70 个交易日）
            'min_peak_days': 10,          # 【C0】平台最高点距今日最少交易日数（0=不限制）
            'max_range_pct': 25.0,        # 【C1】前 N 日振幅上限（%），低点到高点的距离
            'breakout_ratio': 0.0,        # 【C2】突破阈值：0 = 严格 100% 突破；-0.02 = 允许到 98%
            'min_change_pct': 0.05,       # 【C3】今日最小涨幅（> 5%）
            'volume_ratio': 1.8,          # 【C4】今日成交量 / 前 N 日均量
            'volume_ma_period': 5,        # 【C4】成交量均值周期
            'enable_ma_bullish': False,   # 【C5】是否要求均线多头排列（与窄幅震荡冲突，默认关闭）
            'ma_short_period': 5,
            'ma_mid_period': 10,
            'ma_long_period': 20,
        }
        if params:
            default_params.update(params)

        super().__init__("阻力位突破策略", default_params)

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

        lookback_days = int(self.params['lookback_days'])
        vol_period = int(self.params['volume_ma_period'])

        # 平台/阻力位：前 N 日最高价（含当日，用于展示）
        result['resistance_level'] = result['high'].rolling(
            window=lookback_days, min_periods=1).max()
        # 平台区间最低价
        result['platform_low'] = result['low'].rolling(
            window=lookback_days, min_periods=1).min()
        # 成交量均线 / 量比
        result['volume_ma'] = result['volume'].rolling(
            window=vol_period, min_periods=1).mean()
        result['volume_ratio'] = result['volume'] / result['volume_ma']
        # 均线
        result['ma_short'] = result['close'].rolling(
            window=int(self.params['ma_short_period']), min_periods=1).mean()
        result['ma_mid'] = result['close'].rolling(
            window=int(self.params['ma_mid_period']), min_periods=1).mean()
        result['ma_long'] = result['close'].rolling(
            window=int(self.params['ma_long_period']), min_periods=1).mean()

        return result

    # ------------------------------------------------------------------ #
    # 条件描述
    # ------------------------------------------------------------------ #
    def get_selection_criteria(self):
        p = self.params
        criteria = [
            f"1. 平台高点陈旧：平台（阻力位）最高点距今 ≥ {int(p['min_peak_days'])} 个"
            f"交易日（排除近 {int(p['min_peak_days'])} 日内已冲高/已突破的股票）",
            f"2. 平台窄幅震荡：前 {int(p['lookback_days'])} 日（最高价-最低价）/最低价 ≤ "
            f"{p['max_range_pct']}%",
            f"3. 突破平台高点：今日收盘价 > 前 {int(p['lookback_days'])} 日最高价"
            f"（{100 + p['breakout_ratio'] * 100:.0f}%）",
            f"4. 当日长阳：今日涨幅 > {p['min_change_pct'] * 100:.0f}%",
            f"5. 放量确认：今日成交量 ≥ 前 {int(p['volume_ma_period'])} 日均量 × "
            f"{p['volume_ratio']}",
        ]
        if p.get('enable_ma_bullish', False):
            criteria.append(
                f"{len(criteria) + 1}. 均线多头排列：MA{p['ma_short_period']} > "
                f"MA{p['ma_mid_period']} > MA{p['ma_long_period']}")
        criteria.append(f"{len(criteria) + 1}. 选股日 = 突破日（当日判定，不含回踩环节）")
        return criteria

    # ------------------------------------------------------------------ #
    # 快速过滤
    # ------------------------------------------------------------------ #
    def quick_filter(self, df) -> bool:
        """快速过滤：最新一日是否为大阳线（涨幅 > 阈值）

        与 `select_stocks` 一致，内部自动识别数据方向。
        """
        if df is None or df.empty or len(df) < 2:
            return False
        d = self._to_ascending(df)
        try:
            last = d.iloc[-1]
            prev = d.iloc[-2]
            if float(prev['close']) <= 0:
                return False
            change = (float(last['close']) - float(prev['close'])) / float(prev['close'])
            return bool(change > float(self.params['min_change_pct']))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 选股主逻辑
    # ------------------------------------------------------------------ #
    def select_stocks(self, df, stock_name='') -> list:
        """选股逻辑：**突破日即为选股日**"""
        if df is None or df.empty:
            return []

        p = self.params
        lookback = int(p['lookback_days'])
        min_len = max(lookback + 1, int(p['volume_ma_period']) + 1, 25)
        if len(df) < min_len:
            return []

        # 统一正序
        d = self._to_ascending(df).reset_index(drop=True)
        if len(d) < min_len:
            return []

        # 退市检查：最新数据距今超过 5 年 → 视为已退市
        try:
            from datetime import datetime
            latest_date = datetime.strptime(str(d['date'].iloc[-1])[:10], '%Y-%m-%d')
            if (datetime.now() - latest_date).days > 365 * 5:
                return []
        except Exception:
            pass

        today = d.iloc[-1]
        try:
            close_today = float(today['close'])
            high_today = float(today['high'])
            low_today = float(today['low'])
            vol_today = float(today['volume'])
            prev_close = float(d['close'].iloc[-2])
        except Exception:
            return []
        if close_today <= 0 or prev_close <= 0 or vol_today <= 0:
            return []
        if pd.isna(close_today) or pd.isna(vol_today):
            return []

        # 平台窗口：今日之前 lookback 日（不含今日）
        win = d.iloc[-(lookback + 1):-1]
        if len(win) < lookback:
            return []
        win_high = float(win['high'].max())
        win_low = float(win['low'].min())
        if win_high <= 0 or win_low <= 0:
            return []

        # ---- C0 平台最高点须"陈旧"：近 N 日内不得已有最高点 ----
        #   若平台最高点落在最近 min_peak_days 日内，说明这个"高点"本身就是近日冲高
        #   /放量突破留下的，属"突破后的连续拉升"，不是首次突破 → 排除。
        #   例：600830 香溢融通 2026-09-01 已放量涨停突破（收 9.23，平台前高仅 8.53），
        #   09-02 算出的"阻力位" 9.23 就是 09-01 自己的高点，再判定命中即为追高。
        peak_min_days = int(p.get('min_peak_days', 10))
        peak_pos = int(win['high'].astype(float).idxmax())
        days_since_peak = len(d) - 1 - peak_pos
        if peak_min_days > 0 and days_since_peak < peak_min_days:
            return []

        # ---- C1 平台振幅（低点 → 高点距离）----
        max_range = float(p['max_range_pct'])
        range_pct = (win_high - win_low) / win_low * 100
        if max_range > 0 and range_pct > max_range:
            return []

        # ---- C2 突破平台高点（严格 100%，除非 breakout_ratio < 0）----
        ratio = float(p['breakout_ratio'])
        if close_today < win_high * (1 + ratio):
            return []

        # ---- C3 今日涨幅 ----
        change_pct = (close_today - prev_close) / prev_close * 100
        if change_pct <= float(p['min_change_pct']) * 100:
            return []

        # ---- C4 放量：今日量 ≥ 前 N 日均量 × 倍数 ----
        vol_period = int(p['volume_ma_period'])
        vol_window = d['volume'].iloc[-(vol_period + 1):-1]
        if len(vol_window) < vol_period:
            return []
        vol_ma = float(vol_window.mean())
        vol_ratio = float(p['volume_ratio'])
        if vol_ma <= 0:
            return []
        vr = vol_today / vol_ma
        if vr < vol_ratio:
            return []

        # ---- C5 均线多头排列（可选）----
        ma_info = None
        if p.get('enable_ma_bullish', False):
            ma_s = d['close'].rolling(int(p['ma_short_period'])).mean().iloc[-1]
            ma_m = d['close'].rolling(int(p['ma_mid_period'])).mean().iloc[-1]
            ma_l = d['close'].rolling(int(p['ma_long_period'])).mean().iloc[-1]
            if pd.isna(ma_s) or pd.isna(ma_m) or pd.isna(ma_l):
                return []
            if not (ma_s > ma_m > ma_l):
                return []
            ma_info = (float(ma_s), float(ma_m), float(ma_l))

        # ---- 生成信号 ----
        key_date = str(d['date'].iloc[-1])[:10]
        reasons = [
            f"平台最高点{win_high:.2f}距今{days_since_peak}个交易日"
            f"（≥{peak_min_days}，排除近日已冲高/已突破）",
            f"平台窄幅震荡：前{lookback}日高低区间 {win_low:.2f}~{win_high:.2f}，"
            f"振幅{range_pct:.1f}%（≤{max_range}%）",
            f"今日突破平台高点 {win_high:.2f}：收盘{close_today:.2f}"
            f"（{100 + ratio * 100:.0f}% 口径），涨幅{change_pct:.1f}%",
            f"放量确认：量比{vr:.2f}（≥{vol_ratio}）",
        ]
        if ma_info:
            reasons.append(f"均线多头排列：MA{int(p['ma_short_period'])}"
                           f"{ma_info[0]:.2f} > MA{int(p['ma_mid_period'])}"
                           f"{ma_info[1]:.2f} > MA{int(p['ma_long_period'])}"
                           f"{ma_info[2]:.2f}")

        return [{
            'code': '',
            'name': stock_name,
            'key_date': key_date,
            'key_date_type': '阻力位突破日',
            'price': round(close_today, 2),
            'resistance': round(win_high, 2),          # 平台高点（阻力位）
            'platform_low': round(win_low, 2),         # 平台低点
            'days_since_peak': days_since_peak,        # 平台最高点距今交易日数
            'range_pct': round(range_pct, 2),          # 平台振幅
            'change_pct': round(change_pct, 2),        # 今日涨幅
            'volume_ratio': round(vr, 2),              # 量比
            'breakout_ratio': round((close_today - win_high) / win_high, 4),
            'days_since_breakout': 0,                  # 突破日即为选股日
            'reasons': reasons,
            'strategy_type': 'ResistanceBreakoutStrategy',
        }]
