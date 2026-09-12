# -*- coding: utf-8 -*-
"""
全A指数 ADX 计算模块

用途：在每日数据更新时（与"当日温度"同批次）计算全A指数（中证全指 000985.CSI）的 ADX，
      用于刻画市场趋势强度，结果落库备查。

判读口径：
  ADX < 20   无趋势（震荡市）
  20 ~ 25    趋势萌芽
  25 ~ 50    趋势明确
  >= 50      强趋势
  方向由 +DI / -DI 决定：+DI > -DI 多头方向，+DI < -DI 空头方向。

数据源：Tushare index_daily（与 IndexDataFetcher / MarketTemperature 同一通道）
存储：DB 表 market_index_adx（独立表，不写入 market_temperature）
配置：config/risk_config.yaml 的 market_index_adx 段（index_code / period）
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from utils.technical import ADX

logger = logging.getLogger(__name__)


class DataNotAvailableError(Exception):
    """指数数据不可用（非交易日或接口无数据）"""
    pass


class MarketIndexADX:
    """全A指数 ADX 计算器"""

    DEFAULT_INDEX_CODE = '000985.CSI'     # 中证全指：覆盖沪深全部A股，最接近"全A"口径
    DEFAULT_PERIOD = 14                   # Wilder 标准周期
    WARMUP_CALENDAR_DAYS = 400            # 预热窗口（自然日）：Wilder 平滑约需 2×period 根K线，多取避免截断

    # 趋势强度分档（阈值由高到低匹配）
    STRENGTH_LEVELS = [
        (50.0, '强趋势'),
        (25.0, '趋势明确'),
        (20.0, '趋势萌芽'),
        (0.0, '无趋势(震荡)'),
    ]

    def __init__(self, tushare_pro=None, index_code: str = None, period: int = None):
        """
        Args:
            tushare_pro: Tushare Pro 实例（预留，当前复用 IndexDataFetcher 取数）
            index_code: 指数代码，默认取配置，再取 000985.CSI
            period: ADX 周期，默认取配置，再取 14
        """
        self.tushare_pro = tushare_pro
        cfg = self._load_config()
        self.index_code = index_code or cfg.get('index_code') or self.DEFAULT_INDEX_CODE
        self.period = int(period or cfg.get('period') or self.DEFAULT_PERIOD)

    @staticmethod
    def _load_config() -> Dict:
        """从 config/risk_config.yaml 读取 market_index_adx 段"""
        try:
            from utils.risk_config_loader import RiskConfigLoader
            config = RiskConfigLoader().load_config() or {}
            return config.get('market_index_adx', {}) or {}
        except Exception as e:
            logger.warning(f"加载指数ADX配置失败，使用默认值: {e}")
            return {}

    def _fetch_index_df(self, trade_date: str, use_cache: bool) -> Optional[pd.DataFrame]:
        """获取指数日线（截至 trade_date，含预热窗口）"""
        from utils.index_data_fetcher import IndexDataFetcher

        end_date = trade_date
        start_date = (datetime.strptime(trade_date, '%Y%m%d')
                      - timedelta(days=self.WARMUP_CALENDAR_DAYS)).strftime('%Y%m%d')

        fetcher = IndexDataFetcher(ts_code=self.index_code)
        return fetcher.fetch_index_data(start_date=start_date, end_date=end_date,
                                        use_cache=use_cache)

    @classmethod
    def _strength(cls, adx_value: float) -> str:
        """ADX 值 → 趋势强度文本"""
        for threshold, label in cls.STRENGTH_LEVELS:
            if adx_value >= threshold:
                return label
        return '无趋势(震荡)'

    @staticmethod
    def _direction(plus_di: float, minus_di: float) -> str:
        if plus_di > minus_di:
            return '多头'
        if plus_di < minus_di:
            return '空头'
        return '缠绕'

    def calculate(self, trade_date: str, use_cache: bool = False) -> Dict:
        """
        计算指定交易日的全A指数 ADX

        Args:
            trade_date: 交易日期，YYYYMMDD 或 YYYY-MM-DD
            use_cache: 是否使用指数数据缓存（数据更新时应为 False）

        Returns:
            dict: 含 trade_date / index_code / period / adx / plus_di / minus_di /
                  adx_prev / adx_change / trend_strength / trend_direction /
                  close / data_points / has_enough_data

        Raises:
            DataNotAvailableError: 当日无指数数据（非交易日或接口无数据）
        """
        if not trade_date:
            raise ValueError('trade_date is required')

        trade_date = trade_date.replace('-', '')

        df = self._fetch_index_df(trade_date, use_cache)
        if df is None or df.empty:
            raise DataNotAvailableError(f"指数数据为空: {self.index_code} {trade_date}")

        # 只使用截至 trade_date 的数据，杜绝前视偏差
        df['date'] = pd.to_datetime(df['date'])
        df = df[df['date'] <= pd.to_datetime(trade_date)].copy()
        if df.empty:
            raise DataNotAvailableError(f"当日无指数数据: {self.index_code} {trade_date}")

        df = df.sort_values('date').reset_index(drop=True)
        if df['date'].iloc[-1].strftime('%Y%m%d') != trade_date:
            raise DataNotAvailableError(
                f"指数最新日期为 {df['date'].iloc[-1].strftime('%Y%m%d')}，"
                f"与请求日期 {trade_date} 不符（可能非交易日）")

        indicators = ADX(df, self.period)
        last = indicators.iloc[-1]

        adx_value = float(last['adx']) if pd.notna(last['adx']) else 0.0
        plus_di = float(last['plus_di']) if pd.notna(last['plus_di']) else 0.0
        minus_di = float(last['minus_di']) if pd.notna(last['minus_di']) else 0.0

        adx_prev = 0.0
        if len(indicators) > 1:
            prev = indicators.iloc[-2]
            adx_prev = float(prev['adx']) if pd.notna(prev['adx']) else 0.0

        data_points = len(df)
        return {
            'trade_date': trade_date,
            'index_code': self.index_code,
            'period': self.period,
            'adx': round(adx_value, 2),
            'plus_di': round(plus_di, 2),
            'minus_di': round(minus_di, 2),
            'adx_prev': round(adx_prev, 2),
            'adx_change': round(adx_value - adx_prev, 2),
            'trend_strength': self._strength(adx_value),
            'trend_direction': self._direction(plus_di, minus_di),
            'close': round(float(df['close'].iloc[-1]), 2),
            'data_points': data_points,
            'has_enough_data': 1 if data_points >= self.period * 2 else 0,
        }
