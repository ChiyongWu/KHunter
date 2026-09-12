# -*- coding: utf-8 -*-
"""
龙头策略 (LeaderStrategy)

选股条件（全部数据来自 tushare limit_list_d，按选股日实时获取，单接口覆盖）：
  1. 当日涨停        -> limit_type='U'（已排除 ST/退市/炸板未封）
  2. 封板时间 < 11:00 -> 最后封板时间 last_time（默认判定字段，参数可配为 first_time）
  3. 当日换手率 15%-25% -> turnover_ratio（无限售流通股口径，单位 %，=通达信显示值）
  4. 流通市值 < 200 亿  -> float_mv（单位 元；200 亿 = 20000000000）

实现说明：
  - 策略运行框架为“逐只股票传入 K 线”（BaseStrategy.execute_selection），
    本策略在 select_stocks 内按 (code, trade_date) 命中当日涨停股，
    涨停判定由数据层（limit_type='U'）保证，无需在 K 线里重算涨幅阈值。
  - 涨停数据按选股日实时批量获取：一次接口调用取回该交易日全部涨停股，
    仅在内存中参与选股，本地不落库存储任何涨停数据。
  - 同一交易日只请求一次（实例内缓存复用），全市场逐只选股不会重复请求接口。
  - 封板口径默认为最后封板时间 < 11:00，不限制炸板（max_open_times 保持 None 即不限制）。
"""
import os
import pandas as pd
from strategy.base_strategy import BaseStrategy


class LeaderStrategy(BaseStrategy):
    """龙头策略：当日涨停 + 早封板 + 高换手 + 小流通盘。"""

    # 涨停数据获取相关状态（类级：跨实例复用，避免重复初始化）
    _collector = None             # 取数模块缓存（False 表示不可用）
    _pro = None                   # tushare pro_api 实例缓存

    def __init__(self, params=None):
        default_params = {
            'seal_time_field': 'last_time',    # 封板时间判定字段：last_time=最后封板（默认）；first_time=首次封板
            'seal_time_limit': 110000,         # 封板时间上限 HHMMSS（11:00）
            'max_open_times': None,            # 炸板次数上限；None=不限制（严格按字面条件）
            'turnover_min': 15.0,              # 换手率下限(%)
            'turnover_max': 25.0,              # 换手率上限(%)
            'float_mv_max': 20000000000,       # 流通市值上限(元) = 200 亿
            'min_limit_times': 1,              # 连板下限；1=不过滤首板
            'max_limit_times': None,           # 连板上限；None=不限制
            'limit_type': 'U',                 # 涨停（数据源仅取 U）
            'strategy_weight': 70,             # 技术面评分权重
        }
        if params:
            default_params.update(params)
        super().__init__("龙头策略", default_params)
        # 当日涨停池缓存（仅内存，不落库）：{trade_date: {code(6位): row}}
        self._pool_cache = {}
        self._current_code = ''
        # 本次选股中已实时获取过的交易日（实例级，保证同一交易日只请求一次）
        self._fetched_dates = set()

    # ---------- 框架接口重写 ----------
    def analyze_stock(self, stock_code, stock_name, df, selection_date=None):
        """捕获当前股票代码，并显式把 selection_date 透传给选股核心。

        说明：基类 BaseStrategy.execute_selection 调用 select_stocks 时不传 selection_date，
        会使本策略退而用 DataFrame 首行日期作为 trade_date 去查涨停池。web_server 传入的是
        升序 K 线（首行=最早历史日），从而导致查到古老交易日的涨停池而漏选。此处绕过该断点，
        直接以 web_server 传入的 selection_date（已按交易时段回退到前一交易日）作为选股日。
        """
        self._current_code = stock_code
        # 基础数据校验（与框架 execute_selection 保持一致）
        if not self._validate_data(df):
            return None
        signals = self.select_stocks(df, stock_name, selection_date=selection_date)
        if signals:
            return {
                'code': stock_code,
                'name': stock_name,
                'signals': signals,
            }
        return None

    def execute_selection(self, df, stock_code='', stock_name='', selection_date=None):
        """记录当前股票代码后，走基类标准选股流程。

        背景：本策略靠“股票代码 + 选股日”命中涨停池，而基类 execute_selection 并不会
        把 stock_code 传给 select_stocks（select_stocks 只接收 df 与 stock_name）。
        回测引擎与策略运行器正是通过 execute_selection 调用策略，若不在此处记录代码，
        select_stocks 会拿到空的 _current_code 而永远命中不到涨停池，
        表现为“回测/策略运行器选股结果恒为 0”（web_server 走 analyze_stock 则不受影响）。
        此处补齐代码记录，再交由基类完成数据校验、停牌判断与指标计算流程。
        """
        self._current_code = stock_code
        return super().execute_selection(df, stock_code, stock_name, selection_date=selection_date)

    def calculate_indicators(self, df):
        # 涨停判定由数据层保证，无需计算技术指标
        return df

    def quick_filter(self, df):
        return True

    # ---------- 工具 ----------
    @staticmethod
    def _to_yyyymmdd(d):
        s = str(d).replace('-', '').replace('/', '').strip()
        return s[:8] if len(s) >= 8 else s

    @staticmethod
    def _norm_code(code):
        code = str(code or '').strip()
        return code.split('.')[0] if '.' in code else code

    @staticmethod
    def _parse_nullable_int(value):
        """把配置值解析为“可空整数”，用于表示“不限制”的参数。

        背景：strategy_params.yaml 中“不限制”写作 null，经配置加载后常变成字符串
        'null'（本项目其它策略如 volume_ratio_max 亦采用该写法）。若直接用
        int('null') 会抛 ValueError，导致该股票被框架计为“分析失败”而漏选。
        此处统一把 None/''/'null'/'none'/'~'/'nan' 视为不限制（返回 None）。
        """
        if value is None:
            return None
        s = str(value).strip().lower()
        if s in ('', 'null', 'none', '~', 'nan'):
            return None
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _get_collector(cls):
        """按路径加载取数模块 scripts/collect_limit_up_pool.py（仅加载一次）。"""
        if cls._collector is not None:
            return cls._collector or None
        try:
            import importlib.util
            import os
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(root, 'scripts', 'collect_limit_up_pool.py')
            spec = importlib.util.spec_from_file_location(
                '_khunter_limit_up_collector', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls._collector = mod
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f'加载涨停取数模块失败，实时获取不可用: {e}')
            cls._collector = False
        return cls._collector or None

    def _fetch_pool_by_date(self, trade_date):
        """按交易日期实时批量获取当日全部涨停股（一次接口调用，仅内存使用，不落库）。

        本地不留存任何涨停数据：取回后直接构建 {code: row} 缓存供本次选股复用。
        同一交易日只请求一次——全市场 5000+ 只逐只选股时也不会重复请求接口。
        """
        if trade_date in self._fetched_dates:
            return self._pool_cache.get(trade_date, {})
        self._fetched_dates.add(trade_date)

        mod = LeaderStrategy._get_collector()
        if not mod:
            return {}
        try:
            if LeaderStrategy._pro is None:
                LeaderStrategy._pro = mod.get_pro()
            rows = mod.fetch_limit_up_day(LeaderStrategy._pro, trade_date)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f'按交易日实时获取涨停池失败 {trade_date}: {e}')
            return {}

        pool = {}
        for r in rows:
            pool[self._norm_code(r.get('code') or r.get('ts_code'))] = r
        import logging
        logging.getLogger(__name__).info(
            f'龙头策略按交易日实时获取涨停池 {trade_date}: {len(pool)} 只（内存，不落库）')
        return pool

    def _load_day_pool(self, trade_date):
        """取某交易日的涨停股：一次批量实时取回该日全部涨停股，内存缓存复用。"""
        if trade_date in self._pool_cache:
            return self._pool_cache[trade_date]
        pool = self._fetch_pool_by_date(trade_date)
        self._pool_cache[trade_date] = pool
        return pool

    # ---------- 选股核心 ----------
    def select_stocks(self, df, stock_name='', selection_date=None):
        if df is None or len(df) == 0:
            return []
        if not self._validate_stock_name(stock_name):
            return []

        sel_date = selection_date or str(df.iloc[0]['date']).split()[0]
        trade_date = self._to_yyyymmdd(sel_date)
        code = self._norm_code(getattr(self, '_current_code', ''))

        # 条件1：当日涨停（数据层保证 limit_type='U'）
        pool = self._load_day_pool(trade_date)
        row = pool.get(code)
        if row is None:
            return []

        # 条件2：封板时间 < 上限（默认最后封板时间）
        seal_field = self.params.get('seal_time_field', 'last_time')
        seal_val = row.get(seal_field)
        if seal_val is None or seal_val == '':
            return []  # 封板时间缺失，保守不入选
        try:
            if int(str(seal_val).zfill(6)) >= int(self.params['seal_time_limit']):
                return []
        except (ValueError, TypeError):
            return []

        # 条件3：换手率 15%-25%
        tr = row.get('turnover_ratio')
        if tr is None or pd.isna(tr):
            return []
        tr = float(tr)
        if not (self.params['turnover_min'] <= tr <= self.params['turnover_max']):
            return []

        # 条件4：流通市值 < 200 亿
        mv = row.get('float_mv')
        if mv is None or pd.isna(mv):
            return []
        mv = float(mv)
        if mv >= self.params['float_mv_max']:
            return []

        # 附加过滤：炸板次数（None/'null' 等表示不限制）
        max_open = self._parse_nullable_int(self.params.get('max_open_times'))
        if max_open is not None:
            if (row.get('open_times') or 0) > max_open:
                return []

        # 附加过滤：连板下限
        min_lt = self._parse_nullable_int(self.params.get('min_limit_times', 1))
        if min_lt and min_lt > 1:
            if (row.get('limit_times') or 0) < min_lt:
                return []
        # 附加过滤：连板上限（避免高位连板接盘；None=不限制）
        max_lt = self._parse_nullable_int(self.params.get('max_limit_times', None))
        if max_lt:
            if (row.get('limit_times') or 0) > max_lt:
                return []

        # ---------- 命中：构造信号 ----------
        limit_times = row.get('limit_times') or 0
        reasons = [
            f"当日涨停（limit_type={self.params.get('limit_type')}）",
            f"最后封板时间 {seal_val} < {self.params['seal_time_limit']}",
            f"换手率 {tr:.2f}% ∈ [{self.params['turnover_min']},{self.params['turnover_max']}]%",
            f"流通市值 {mv/1e8:.2f}亿 < {self.params['float_mv_max']/1e8:.0f}亿",
        ]
        if limit_times and limit_times > 1:
            reasons.append(f"连板 {limit_times} 板")

        # 收盘价优先取涨停池当日快照（已与K线当日收盘价交叉核对一致）。
        # 回退时严格只用“选股日及之前”的K线取最近收盘价，避免未来函数：
        # 直接用 df.iloc[0] 在降序未切片数据上会取到选股日之后的K线。
        close = row.get('close')
        if not close:
            try:
                past = df[df['date'].astype(str).str.slice(0, 10) <= sel_date]
                past = past.sort_values('date')
                close = float(past.iloc[-1]['close']) if len(past) else 0.0
            except Exception:
                close = 0.0
        close = float(close)
        signal = {
            'date': sel_date,
            'close': round(close, 2),
            'volume_ratio': 0,
            'reasons': reasons,
            'key_date': sel_date,
            'key_date_type': '涨停日',
            'pattern_details': {
                'pct_chg': row.get('pct_chg'),
                'turnover_ratio': tr,
                'float_mv': mv,
                'float_mv_yi': round(mv / 1e8, 2),
                'first_time': row.get('first_time'),
                'last_time': row.get('last_time'),
                'open_times': row.get('open_times'),
                'limit_times': limit_times,
                'industry': row.get('industry'),
                'fd_amount': row.get('fd_amount'),
            },
            'confirmation_details': {
                'confirmed': True,
                'confirmed_date': sel_date,
                'support_level': round(close, 2),
            },
            'strategy_weight': self.params.get('strategy_weight', 70),
        }
        return [signal]

    # ---------- 展示 ----------
    def get_selection_criteria(self):
        p = self.params
        return [
            f"1. 当日涨停（数据源 tushare limit_list_d 实时获取，limit_type='{p.get('limit_type')}'，已排除ST/退市/炸板未封）",
            f"2. 最后封板时间 < {p['seal_time_limit']}（字段 {p.get('seal_time_field')}，HHMMSS）",
            f"3. 换手率 {p['turnover_min']}%-{p['turnover_max']}%（turnover_ratio，无限售流通股口径）",
            f"4. 流通市值 < {p['float_mv_max']/1e8:.0f} 亿（float_mv，单位元）",
            f"5. 炸板次数上限 {'不限' if p.get('max_open_times') is None else p['max_open_times']}；"
            f"连板区间 {p.get('min_limit_times', 1)}~{'不限' if p.get('max_limit_times') is None else p['max_limit_times']}",
        ]


if __name__ == '__main__':
    # 自测：对指定交易日，按日实时获取涨停股并逐只跑 select_stocks，输出命中
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    td = sys.argv[1] if len(sys.argv) > 1 else '20260827'
    strat = LeaderStrategy()
    pool = strat._load_day_pool(td)      # 按交易日实时批量获取（内存，不落库）
    import datetime as _dt
    day = _dt.datetime.strptime(td, '%Y%m%d').strftime('%Y-%m-%d')
    hits = []
    for code, r in pool.items():
        strat._current_code = code
        df = pd.DataFrame([{'date': day, 'close': r.get('close')}])
        sig = strat.select_stocks(df, r.get('name'), selection_date=day)
        if sig:
            hits.append((code, r.get('name'), sig[0]['pattern_details']))
    print(f'{td} 龙头策略命中 {len(hits)} 只:')
    for code, name, det in hits:
        print(f"  {code} {name} | 换手{det['turnover_ratio']}% 流通{det['float_mv_yi']}亿 "
              f"首封{det['first_time']} 末封{det['last_time']} 炸板{det['open_times']} 连板{det['limit_times']}")
