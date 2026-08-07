"""
超跌反弹策略（OversoldReboundStrategy）

策略逻辑：
  1. 超跌深度检查（必要条件，C1）：近 lookback_days 个交易日内，区间最高价到最低价的
     下跌幅度超过 decline_threshold（默认50%），即股票处于深度超跌状态。
  2. 底部特征检查（满足之一，C2）：近 bottom_window 个交易日内出现以下三种底部特征之一：
     a) MACD底背离：窗口内价格创新低，但MACD柱未同步创新低（动能背离）。
     b) 底分型形态：近三根K线最低价居中（缠论简化底分型）。
     c) 低位九转：最新交易日完成买入Countdown第9根（复用LowTD9Strategy状态机）。

组合：C1 AND (C2a OR C2b OR C2c)

数据约定：df 倒序，index=0 为最新交易日（与框架 execute_selection 入口规范化一致）。
"""

from strategy.base_strategy import BaseStrategy
from strategy.low_td9_strategy import LowTD9Strategy
import pandas as pd


class OversoldReboundStrategy(BaseStrategy):
    """超跌反弹策略：深度超跌 + 任一底部特征（MACD底背离/底分型/低位九转）"""

    # 默认参数（与 config/strategy_params.yaml 的 params 段保持一致）
    DEFAULT_PARAMS = {
        'lookback_days': 30,          # 超跌检查回溯交易日数
        'decline_threshold': 0.50,    # 区间最高→最低下跌幅度阈值
        'bottom_window': 3,           # 底部特征搜索窗口
        'macd_divergence_days': 20,   # MACD底背离判断窗口
        'macd_fast': 12,              # MACD快线EMA周期
        'macd_slow': 26,              # MACD慢线EMA周期
        'macd_signal': 9,             # MACD信号线EMA周期
        'enable_bottom_fractal': True,   # 启用底分型特征
        'fractal_require_yang': False,   # 底分型右侧是否要求阳线
        'enable_low_td9': True,          # 启用低位九转特征
    }

    def __init__(self, params=None):
        """初始化策略，合并默认参数并指定中文名称"""
        # 合并用户参数到默认参数（用户值覆盖默认值）
        merged = dict(self.DEFAULT_PARAMS)
        if params:
            merged.update(params)
        super().__init__("超跌反弹", merged)  # 合并后传给基类
        # 低位九转复用实例（仅调用其状态机，不含低位过滤）
        self._td9 = LowTD9Strategy()

    # ------------------------------------------------------------------ #
    # 指标计算
    # ------------------------------------------------------------------ #
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算 MACD 指标（DIF / DEA / MACD柱），返回带指标的 DataFrame

        :param df: 原始日线数据（倒序，index=0最新），需含 close 列
        :return: 新增 dif/dea/macd 三列的 DataFrame
        """
        df = df.copy()  # 避免修改原始数据
        close = df['close']  # 直接基于收盘价序列计算
        # 快线与慢线指数移动平均
        ema_fast = close.ewm(span=self.params['macd_fast'], adjust=False).mean()
        ema_slow = close.ewm(span=self.params['macd_slow'], adjust=False).mean()
        dif = ema_fast - ema_slow  # DIF = 快线 - 慢线
        dea = dif.ewm(span=self.params['macd_signal'], adjust=False).mean()  # 信号线
        df['dif'] = dif
        df['dea'] = dea
        df['macd'] = dif - dea  # MACD柱 = DIF - DEA
        return df

    # ------------------------------------------------------------------ #
    # 规则1：超跌深度检查（C1）
    # ------------------------------------------------------------------ #
    def _check_oversold(self, df: pd.DataFrame, reasons: list) -> bool:
        """
        检查近 lookback_days 日区间最高价到最低价的下跌幅度是否超过阈值

        :param df: 含 high/low 的 DataFrame（倒序）
        :param reasons: 命中理由列表（命中时追加说明）
        :return: True 表示满足超跌条件
        """
        lookback = int(self.params['lookback_days'])  # 回溯交易日数
        threshold = float(self.params['decline_threshold'])  # 下跌幅度阈值
        n = len(df)
        if n < lookback:
            return False  # 数据不足无法判断
        window = df.head(lookback)  # 取最近 lookback 日（倒序）
        max_high = window['high'].max()  # 区间最高价
        min_low = window['low'].min()  # 区间最低价
        if max_high <= 0 or pd.isna(max_high) or pd.isna(min_low):
            return False  # 价格异常
        decline = (max_high - min_low) / max_high  # 下跌幅度
        if decline > threshold:
            reasons.append(f"近{lookback}日超跌幅度{decline*100:.1f}%（最高→最低）")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 规则2a：MACD底背离
    # ------------------------------------------------------------------ #
    def _check_macd_divergence(self, df: pd.DataFrame, reasons: list) -> bool:
        """
        检查近 macd_divergence_days 窗口内是否出现价格新低而MACD柱未同步新低

        :param df: 含 low/macd 的 DataFrame（倒序，已计算指标）
        :param reasons: 命中理由列表
        :return: True 表示出现MACD底背离
        """
        days = int(self.params['macd_divergence_days'])  # 背离判断窗口
        n = len(df)
        if n < days:
            return False  # 数据不足
        window = df.head(days).reset_index(drop=True)  # 取窗口并重置索引（idx0最新）
        low = window['low']
        macd = window['macd']
        # 找最低价位置（时间序：iloc），取第一个最低点
        min_low_idx = int(low.idxmin())
        min_low = low.iloc[min_low_idx]  # 窗口最低价
        macd_at_low = macd.iloc[min_low_idx]  # 最低价当日的MACD柱
        macd_min = macd.min()  # 窗口内MACD柱最小值
        # 底背离：价格创新低时，MACD柱未同步创新低（动能背离）
        if macd_at_low > macd_min:
            reasons.append("底部特征：MACD底背离（价格新低而动能未新低）")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 规则2b：底分型形态（缠论简化版）
    # ------------------------------------------------------------------ #
    def _check_bottom_fractal(self, df: pd.DataFrame, reasons: list) -> bool:
        """
        检查近三根K线是否构成底分型：中间K线最低价低于左右两根

        倒序约定：idx0=最新，idx1=前一交易日，idx2=前二交易日
        底分型判定：low[idx1] < low[idx0] 且 low[idx1] < low[idx2]

        :param df: 含 open/close/low 的 DataFrame（倒序）
        :param reasons: 命中理由列表
        :return: True 表示出现底分型
        """
        if len(df) < 3:
            return False  # 至少需要三根K线
        # 取最近三根（倒序 idx0/1/2）
        l0 = df['low'].iloc[0]
        l1 = df['low'].iloc[1]
        l2 = df['low'].iloc[2]
        # 中间K线最低价低于左右两侧（底分型核心）
        if l1 < l0 and l1 < l2:
            # 可选增强：右侧K线要求阳线确认
            if self.params.get('fractal_require_yang', False):
                is_yang = df['close'].iloc[0] > df['open'].iloc[0]  # 最新K线收阳
                if not is_yang:
                    return False
            reasons.append("底部特征：底分型形态（近三日最低价居中）")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 规则2c：低位九转（复用 LowTD9Strategy 状态机）
    # ------------------------------------------------------------------ #
    def _check_low_td9(self, df: pd.DataFrame, reasons: list) -> bool:
        """
        检查最新交易日是否完成买入 Countdown 第9根（今日9转）

        复用 LowTD9Strategy 的正向状态机，仅判定"今日9转完成"，
        不附加完美信号/低位环境过滤（超跌条件已由C1保证）。

        :param df: 含 close/low 的 DataFrame（倒序）
        :param reasons: 命中理由列表
        :return: True 表示今日完成低位九转
        """
        setup_window = int(self._td9.params.get('setup_window', 9))        # Setup连续根数
        setup_offset = int(self._td9.params.get('setup_offset', 4))        # Setup比较偏移
        countdown_offset = int(self._td9.params.get('countdown_offset', 2)) # Countdown偏移
        target = int(self._td9.params.get('countdown_target', 9))          # 9转目标
        res = self._td9._run_td_state_machine(
            df, setup_window, setup_offset, countdown_offset, target, True
        )
        if res.get('countdown_complete', False):
            reasons.append("底部特征：低位九转今日完成（买入Countdown第9根）")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 选股主逻辑
    # ------------------------------------------------------------------ #
    def select_stocks(self, df: pd.DataFrame, stock_name: str = '') -> list:
        """
        超跌反弹选股主逻辑：C1 AND (C2a OR C2b OR C2c)

        :param df: 个股日线数据（倒序，index=0最新）
        :param stock_name: 股票名称（用于信号说明）
        :return: 命中返回 [signal_dict]，否则 []
        """
        # 数据充足性检查（至少需覆盖超跌窗口+背离窗口）
        min_len = max(
            int(self.params['lookback_days']),
            int(self.params['macd_divergence_days']),
        )
        if df is None or len(df) < min_len:
            return []  # 数据不足，直接剪枝

        # 计算 MACD 指标（C2a 需要）
        df = self.calculate_indicators(df)

        reasons = []  # 累计命中理由
        # 规则1：超跌深度（必要条件）
        if not self._check_oversold(df, reasons):
            return []  # 不满足超跌，快速剪枝

        # 规则2：底部特征（满足之一）
        hit_features = []  # 记录命中的特征标签
        if self.params.get('enable_low_td9', True) and self._check_low_td9(df, reasons):
            hit_features.append('低位九转')
        if self.params.get('enable_bottom_fractal', True) and self._check_bottom_fractal(df, reasons):
            hit_features.append('底分型')
        if self._check_macd_divergence(df, reasons):
            hit_features.append('MACD底背离')

        if not hit_features:
            return []  # 无底部特征，不入选

        # 组装信号
        key_date = str(df['date'].iloc[0])[:10] if 'date' in df.columns else ''
        signal = {
            'code': '',  # 由调用方（execute_selection）填充
            'name': stock_name,
            'key_date': key_date,
            'key_date_type': '超跌反弹',
            'reasons': reasons,
            'strategy_type': 'OversoldReboundStrategy',
            'features': hit_features,  # 命中的底部特征列表
        }
        return [signal]

    # ------------------------------------------------------------------ #
    # 选股条件说明（前端展示用）
    # ------------------------------------------------------------------ #
    def get_selection_criteria(self) -> list:
        """
        返回策略选股条件的可读说明列表

        :return: 条件说明字符串列表
        """
        t = float(self.params['decline_threshold'])
        lb = int(self.params['lookback_days'])
        return [
            f"必要条件：近{lb}交易日区间最高价到最低价下跌幅度 > {t*100:.0f}%",
            "底部特征（满足之一）：",
            "  ① MACD底背离：价格创新低而MACD柱未同步新低",
            "  ② 底分型：近三根K线最低价居中",
            "  ③ 低位九转：最新交易日完成买入Countdown第9根",
        ]
