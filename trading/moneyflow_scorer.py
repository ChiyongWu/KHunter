# -*- coding: utf-8 -*-
"""
资金面评分器模块

基于资金流向数据计算资金面得分。
数据来源：Tushare Pro moneyflow_ths 接口（同花顺个股资金流向）
         Tushare Pro hk_hold 接口（北向资金持股）

个股图谱使用实时数据，不从本地数据库降级。

评分维度：
  1. 主力资金净流入（5日累计）- 权重 35%
  2. 大单占比（近5日）- 权重 20%
  3. 北向资金（最近季度）- 权重 20%
  4. 主力与散户方向 - 权重 25%

一票否决条件（2026-09-11 增加"占成交额比"强度口径）：
  - 5日主力净额 < -10000 万元 且 大单净流入占比 < -5%（无占比字段时回退 净额/成交额）
  - 大单净流出占比 > 1% 且 小单净流入占比 > 1%（出货信号）
"""

import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# 导入数据库管理器
from utils.db_manager import DBManager
from utils.tushare_client import get_tushare_pro, load_tushare_config
# 导入资金面详情模型
from trading.stock_score_models import MoneyflowDetail

# 配置日志记录器
logger = logging.getLogger(__name__)


# ============================================================
# 资金面评分常量配置
# ============================================================

# 各维度权重
WEIGHT_MAIN_NET_FLOW = 0.55   # 主力净流入权重
WEIGHT_LARGE_RATIO = 0.10     # 大单占比权重
WEIGHT_NORTH_FUND = 0.10      # 北向资金权重
WEIGHT_DIRECTION = 0.25       # 主力散户方向权重

# 一票否决得分
VETO_SCORE = -100
# 主力净流入一票否决阈值（万元，绝对口径）
VETO_MAIN_NET_FLOW_THRESHOLD = -10000
# 【2026-09-11】主力净额"占成交额比"一票否决阈值（%，相对口径）
#   问题：绝对阈值（-10000 万元）对不同成交额的股票不等价——
#        大成交额股票正常的资金波动就可能超过 1 亿，容易被错杀。
#   处理：采用**联合判定**——仅当【绝对额超阈】且【净额/成交额 < 本阈值】时才否决；
#        成交额很大导致占比很轻时，视为正常波动 → 不否决。
#        设 None 可关闭相对口径（回退为纯绝对阈值）。
VETO_NET_FLOW_RATIO_THRESHOLD = -5.0
# 【2026-09-11】出货信号（条件2）占比阈值（%）：需**同时**满足——
#   ① 大单净流出额占成交额 >  1%
#   ② 小单净流入额占成交额 >  1%
#   原口径（大单净流入<0 且 小单净流入>0）不含强度，轻微流动即被判出货；
#   改为占比口径后，只有"量级显著"的流出/流入才判出货。
#   设 None 可关闭（回退为纯方向判定）。
VETO_DISTRIBUTION_RATIO_THRESHOLD = 1.0

# Tushare API 重试配置
MAX_RETRIES = 3        # 最大重试次数
RETRY_INTERVAL = 1     # 重试间隔（秒）

# 内存缓存 TTL（秒）
CACHE_TTL = 300  # 5分钟


class MemoryCache:
    """
    内存缓存管理器

    仅用于同一请求周期内的数据缓存，避免重复调用 Tushare 接口。
    缓存有效期为 5 分钟。
    """

    def __init__(self, ttl: int = CACHE_TTL):
        """
        初始化内存缓存

        参数:
            ttl: 缓存有效期（秒），默认 300 秒
        """
        # 缓存字典，key -> (data, timestamp)
        self._cache: Dict[str, Tuple] = {}
        # 缓存有效期
        self._ttl = ttl

    def get(self, key: str):
        """
        获取缓存数据

        参数:
            key: 缓存键
        返回:
            缓存数据，过期或不存在返回 None
        """
        if key in self._cache:
            data, timestamp = self._cache[key]
            # 检查是否过期
            if time.time() - timestamp < self._ttl:
                return data
            # 过期则删除
            del self._cache[key]
        return None

    def set(self, key: str, value):
        """
        设置缓存数据

        参数:
            key: 缓存键
            value: 缓存值
        """
        # 存储数据和时间戳
        self._cache[key] = (value, time.time())


class MoneyflowScorer:
    """
    资金面评分器

    根据 Tushare 资金流向数据计算资金面得分。
    四个评分维度：主力净流入、大单占比、北向资金、主力散户方向。
    支持一票否决机制。
    """

    def __init__(self, db_manager: DBManager = None, tushare_token: str = None, data_cache=None):
        """
        初始化资金面评分器

        参数:
            db_manager: 数据库管理器实例，为 None 时使用默认实例
            tushare_token: Tushare API token，为 None 时从配置文件读取
            data_cache: 评分数据本地落地缓存（BacktestDataCache）。提供时资金数据
                        本地优先读取（区间覆盖管理，缺失只增量拉缺口），回测场景
                        注入以消除评分阶段远程请求；None 时保持纯远程模式
        """
        # 使用传入的 db_manager 或创建默认实例
        from utils.global_db import get_global_db
        self.db = db_manager or get_global_db()
        # 初始化 Tushare token
        self._token = tushare_token or self._load_tushare_token()
        # 初始化 Tushare pro API 对象
        self._pro = None
        # 初始化内存缓存
        self._cache = MemoryCache()
        # 本地落地缓存（可选）
        self.data_cache = data_cache
        # 记录初始化日志（改为debug级别，避免频繁输出）
        logger.debug("资金面评分器初始化完成")

    def _load_tushare_token(self) -> str:
        """
        从配置文件加载 Tushare token

        返回:
            str: Tushare API token
        """
        try:
            # 优先使用 token 字段，兼容 api_key 字段
            token = load_tushare_config()['token']
            logger.debug("Tushare token 加载成功")
            return token
        except Exception as e:
            # 配置文件读取失败，返回空字符串
            logger.warning(f"Tushare token 加载失败: {e}")
            return ""

    def _get_pro(self):
        """
        获取 Tushare pro API 实例（延迟初始化）

        返回:
            tushare pro API 对象
        """
        if self._pro is None:
            try:
                # 从配置读取 api_key/base_url 初始化 pro API（base_url 非空时走中转站）
                self._pro = get_tushare_pro(self._token)
                if self._pro is None:
                    raise ValueError("Tushare api_key 未配置")
                logger.debug("Tushare pro API 初始化成功")
            except Exception as e:
                logger.error(f"Tushare pro API 初始化失败: {e}")
                raise
        return self._pro

    def _format_date(self, date_str: str) -> str:
        """
        将日期字符串统一转换为 YYYYMMDD 格式（Tushare 接口要求）

        参数:
            date_str: 日期字符串，支持 YYYYMMDD 或 YYYY-MM-DD
        返回:
            str: YYYYMMDD 格式的日期字符串
        """
        # 去除空白字符
        date_str = date_str.strip()
        # 如果包含横杠，去除横杠
        if "-" in date_str:
            return date_str.replace("-", "")
        return date_str

    def _get_trade_dates(self, end_date: str, count: int = 5) -> List[str]:
        """
        获取截止日期前的 N 个交易日列表

        参数:
            end_date: 截止日期（YYYYMMDD 格式）
            count: 需要的交易日数量，默认 5
        返回:
            List[str]: 交易日列表（YYYYMMDD 格式），按日期升序
        """
        # 解析截止日期
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        # 向前推算足够多的自然日（考虑周末和节假日）
        start_dt = end_dt - timedelta(days=count * 3)
        start_date = start_dt.strftime("%Y%m%d")

        # 生成自然日列表，过滤周末
        dates = []
        current = start_dt
        while current <= end_dt:
            # 排除周六(5)和周日(6)
            if current.weekday() < 5:
                dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)

        # 取最后 count 个交易日
        return dates[-count:] if len(dates) >= count else dates

    def _call_tushare_with_retry(self, func, **kwargs):
        """
        带重试机制的 Tushare API 调用

        参数:
            func: Tushare API 调用函数
            **kwargs: API 参数
        返回:
            DataFrame: API 返回的数据
        """
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                # 调用 Tushare API
                result = func(**kwargs)
                return result
            except Exception as e:
                last_error = e
                # 记录重试日志
                logger.warning(
                    f"Tushare API 调用失败（第 {attempt + 1} 次）: {e}"
                )
                # 非最后一次重试时等待
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_INTERVAL)
        # 所有重试都失败
        logger.error(f"Tushare API 调用失败（已重试 {MAX_RETRIES} 次）: {last_error}")
        return None

    def _convert_ts_code(self, stock_code: str) -> str:
        """
        将6位股票代码转换为 Tushare 格式（带交易所后缀）

        参数:
            stock_code: 6位股票代码，如 '000001'
        返回:
            str: Tushare 格式代码，如 '000001.SZ'
        """
        # 去除空白
        code = stock_code.strip()
        # 如果已经包含后缀，直接返回
        if "." in code:
            return code
        # 根据首位数字判断交易所
        if code.startswith("6"):
            return f"{code}.SH"
        # 深圳交易所（0开头、3开头）
        return f"{code}.SZ"

    def _fetch_moneyflow_data(
        self, stock_code: str, score_date: str
    ) -> Optional[pd.DataFrame]:
        """
        从 Tushare moneyflow_ths 接口获取个股资金流向数据

        获取近5个交易日的资金流向数据。
        个股图谱应该取实时数据，不从本地数据库降级。

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期（YYYYMMDD 格式）
        返回:
            DataFrame: 资金流向数据，失败返回 None
        """
        # 构建缓存键
        cache_key = f"moneyflow_{stock_code}_{score_date}"
        # 检查缓存
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"命中缓存: {cache_key}")
            return cached

        # 计算5日交易日范围
        trade_dates = self._get_trade_dates(score_date, 5)
        if not trade_dates:
            logger.warning(f"无法获取交易日列表: {score_date}")
            return None

        # 起始日期和结束日期
        start_date = trade_dates[0]
        end_date = trade_dates[-1]
        # 转换为 Tushare 格式代码
        ts_code = self._convert_ts_code(stock_code)

        try:
            def _remote(f_start, f_end):
                pro = self._get_pro()
                # 调用 Tushare moneyflow_ths 接口（区间数据）
                return self._call_tushare_with_retry(
                    pro.moneyflow_ths,
                    ts_code=ts_code,
                    start_date=f_start,
                    end_date=f_end,
                )

            # 本地落地缓存优先（覆盖型：只拉缺口），否则直接远程
            if self.data_cache is not None:
                df = self.data_cache.fetch_range("moneyflow_ths", ts_code, start_date, end_date, _remote)
            else:
                df = _remote(start_date, end_date)
            # 检查返回数据是否有效
            if df is not None and not df.empty:
                logger.debug(
                    f"获取资金流向数据成功: {stock_code}, {len(df)} 条记录"
                )
                # 写入缓存
                self._cache.set(cache_key, df)
                return df
            # Tushare 返回空数据，尝试降级到 moneyflow 接口获取历史数据
            logger.warning(f"Tushare moneyflow_ths 返回空数据: {stock_code}，尝试 moneyflow 接口")
            return self._fetch_moneyflow_historical(stock_code, start_date, end_date)
        except Exception as e:
            # 获取实时数据失败，尝试降级到 moneyflow 接口
            logger.error(f"Tushare moneyflow_ths 获取失败: {stock_code}, {e}，尝试 moneyflow 接口")
            return self._fetch_moneyflow_historical(stock_code, start_date, end_date)

    def _fetch_moneyflow_historical(
        self, stock_code: str, start_date: str, end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        从 Tushare moneyflow 接口获取个股历史资金流向数据

        moneyflow 接口与 moneyflow_ths 的区别：
        - moneyflow_ths：同花顺数据，字段 net_amount 是主力净流入，仅近期数据
        - moneyflow：沪深A数据，字段 net_mf_amount 是全市场净流入，支持历史数据

        本方法作为 moneyflow_ths 的降级方案，用于获取历史数据。

        参数:
            stock_code: 股票代码（6位数字）
            start_date: 开始日期（YYYYMMDD 格式）
            end_date: 结束日期（YYYYMMDD 格式）
        返回:
            DataFrame: 资金流向数据，失败返回 None
        """
        # 构建缓存键
        cache_key = f"moneyflow_hist_{stock_code}_{start_date}_{end_date}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"命中历史资金流向缓存: {cache_key}")
            return cached

        # 转换为 Tushare 格式代码
        ts_code = self._convert_ts_code(stock_code)

        try:
            def _remote(f_start, f_end):
                pro = self._get_pro()
                # 调用 Tushare moneyflow 接口获取历史数据（区间数据）
                return self._call_tushare_with_retry(
                    pro.moneyflow,
                    ts_code=ts_code,
                    start_date=f_start,
                    end_date=f_end,
                )

            # 本地落地缓存优先（覆盖型），否则直接远程
            if self.data_cache is not None:
                df = self.data_cache.fetch_range("moneyflow", ts_code, start_date, end_date, _remote)
            else:
                df = _remote(start_date, end_date)
            # 检查返回数据是否有效
            if df is not None and not df.empty:
                logger.debug(
                    f"moneyflow 接口获取资金流向数据成功: {stock_code}, {len(df)} 条记录"
                )
                # 按日期降序排序（最新日期在前）
                if "trade_date" in df.columns:
                    df = df.sort_values("trade_date", ascending=False)
                # 写入缓存
                self._cache.set(cache_key, df)
                return df
            # moneyflow 也返回空数据
            logger.warning(f"Tushare moneyflow 返回空数据: {stock_code}")
            return None
        except Exception as e:
            logger.error(f"Tushare moneyflow 获取失败: {stock_code}, {e}")
            return None

    def _extract_from_moneyflow(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        从 Tushare moneyflow 格式数据中提取指标

        moneyflow 接口的字段：
        - net_mf_amount: 净流入额（万元）= 大单 + 中单 + 小单 + 特大单
        - buy_lg_amount, sell_lg_amount: 大单买入卖出金额（万元）
        - buy_sm_amount, sell_sm_amount: 小单买入卖出金额（万元）
        - buy_elg_amount, sell_elg_amount: 特大单买入卖出金额（万元）

        参数:
            df: moneyflow 格式的 DataFrame
        返回:
            Dict: 评分指标字典
        """
        metrics = {
            "net_flow_5d": 0.0,
            "daily_ratios": [],
            "large_net": 0.0,
            "small_net": 0.0,
        }

        if df is None or df.empty:
            return metrics

        # 5日成交额（用于"净额占成交额比"的相对口径；缺失时保持 0 → 回退绝对阈值）
        if "amount" in df.columns:
            _amt = pd.to_numeric(df["amount"], errors='coerce').fillna(0).sum()
            metrics["amount_5d"] = float(_amt)

        # 主力净流入 = 大单 + 特大单（moneyflow 接口不直接提供，用这个近似）
        if "buy_elg_amount" in df.columns and "sell_elg_amount" in df.columns:
            elg_buy = pd.to_numeric(df["buy_elg_amount"], errors='coerce').fillna(0)
            elg_sell = pd.to_numeric(df["sell_elg_amount"], errors='coerce').fillna(0)
            elg_net = (elg_buy - elg_sell).sum()
        else:
            elg_net = 0.0

        if "buy_lg_amount" in df.columns and "sell_lg_amount" in df.columns:
            lg_buy = pd.to_numeric(df["buy_lg_amount"], errors='coerce').fillna(0)
            lg_sell = pd.to_numeric(df["sell_lg_amount"], errors='coerce').fillna(0)
            lg_net = (lg_buy - lg_sell).sum()
        else:
            lg_net = 0.0

        # 主力净流入 = 大单 + 特大单
        metrics["net_flow_5d"] = float(lg_net + elg_net)

        # 每日大单净流入占比 = (大单 + 特大单) / 成交额
        # moneyflow 接口没有直接的占比字段，需要计算
        if len(df) > 0:
            daily_ratios = []
            for _, row in df.iterrows():
                lg_net = 0.0
                elg_net = 0.0
                if "buy_lg_amount" in row.index and "sell_lg_amount" in row.index:
                    lg_net = float(row["buy_lg_amount"] or 0) - float(row["sell_lg_amount"] or 0)
                if "buy_elg_amount" in row.index and "sell_elg_amount" in row.index:
                    elg_net = float(row["buy_elg_amount"] or 0) - float(row["sell_elg_amount"] or 0)
                main_net = lg_net + elg_net
                # 获取成交额计算占比（如果有的话）
                if "amount" in row.index and row["amount"] > 0:
                    ratio = (main_net / float(row["amount"])) * 100
                    daily_ratios.append(ratio)
                else:
                    daily_ratios.append(0.0)
            metrics["daily_ratios"] = daily_ratios

        # 大单净流入累计（不含特大单，与 moneyflow_ths 保持一致）
        if "buy_lg_amount" in df.columns and "sell_lg_amount" in df.columns:
            large_buy = pd.to_numeric(df["buy_lg_amount"], errors='coerce').fillna(0).sum()
            large_sell = pd.to_numeric(df["sell_lg_amount"], errors='coerce').fillna(0).sum()
            metrics["large_net"] = float(large_buy - large_sell)

        # 小单净流入累计
        if "buy_sm_amount" in df.columns and "sell_sm_amount" in df.columns:
            small_buy = pd.to_numeric(df["buy_sm_amount"], errors='coerce').fillna(0).sum()
            small_sell = pd.to_numeric(df["sell_sm_amount"], errors='coerce').fillna(0).sum()
            metrics["small_net"] = float(small_buy - small_sell)

        return metrics

    def _fetch_north_fund_data(
        self, stock_code: str, score_date: str = None
    ) -> Optional[pd.DataFrame]:
        """
        从 Tushare hk_hold 接口获取北向资金持股数据

        获取评分日期之前半年的北向资金持股数据，用于判断增减持。
        确保回测和实盘使用数据逻辑一致（不会用到未来数据）。

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期（YYYYMMDD 格式），为 None 时使用当前日期
        返回:
            DataFrame: 北向资金持股数据，失败返回 None
        """
        # 评分日期未传时使用当前日期（兼容旧调用）
        if score_date is None:
            score_date = datetime.now().strftime("%Y%m%d")

        # 构建缓存键（包含评分日期，确保回测不重用实盘缓存）
        cache_key = f"north_fund_{stock_code}_{score_date}"
        # 检查缓存
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"命中北向资金缓存: {cache_key}")
            return cached

        # 转换为 Tushare 格式代码
        ts_code = self._convert_ts_code(stock_code)

        # 按评分日期计算查询范围：往前推 180 天
        score_dt = datetime.strptime(score_date, "%Y%m%d")
        end_date = score_dt.strftime("%Y%m%d")
        start_date = (score_dt - timedelta(days=180)).strftime("%Y%m%d")

        try:
            def _remote(f_start, f_end):
                pro = self._get_pro()
                # 调用 hk_hold 接口（区间数据）
                return self._call_tushare_with_retry(
                    pro.hk_hold,
                    ts_code=ts_code,
                    start_date=f_start,
                    end_date=f_end,
                )

            # 本地落地缓存优先（覆盖型），否则直接远程
            if self.data_cache is not None:
                df = self.data_cache.fetch_range("hk_hold", ts_code, start_date, end_date, _remote)
            else:
                df = _remote(start_date, end_date)
            # 检查返回数据
            if df is not None and not df.empty:
                logger.debug(f"获取北向资金数据成功: {stock_code}, {len(df)} 条")
                # 写入缓存
                self._cache.set(cache_key, df)
                return df
            logger.debug(f"北向资金无持股数据: {stock_code}")
            return None
        except Exception as e:
            logger.warning(f"北向资金数据获取失败: {stock_code}, {e}")
            return None

    def _score_main_net_flow(self, net_flow_5d: float) -> float:
        """
        计算主力资金净流入维度得分（5日累计）

        评分标准：
          净额 > 10000万元：100分
          净额 > 5000万元：80分
          净额 > 100万元：60分
          净额 > 0：40分
          净额 == 0（数据缺失）：30分（中性）
          净额 < 0：-20分
          一票否决：净额 < -10000万元 → -100分

        参数:
            net_flow_5d: 5日主力资金净流入金额（万元）
        返回:
            float: 主力净流入维度得分
        """
        # 一票否决条件：净额 < -10000万元
        if net_flow_5d < VETO_MAIN_NET_FLOW_THRESHOLD:
            return VETO_SCORE
        # 净额 > 10000万元：100分
        if net_flow_5d > 10000:
            return 100
        # 净额 > 5000万元：80分
        if net_flow_5d > 5000:
            return 80
        # 净额 > 100万元：60分
        if net_flow_5d > 100:
            return 60
        # 净额 > 0：40分
        if net_flow_5d > 0:
            return 40
        # 净额 == 0（数据缺失或持平）：30分（中性）
        if net_flow_5d == 0:
            return 30
        # 净额 < 0：-20分
        return -20

    def _score_large_ratio(self, daily_ratios: List[float]) -> float:
        """
        计算大单占比维度得分（近5日）

        评分标准（每天独立评分，5日累加）：
          某天流入超过5%：40分
          某天流入超过1%：20分
          某天流入 > 0：10分
          某天流入 < 0：-30分

        参数:
            daily_ratios: 近5日大单净流入占比列表（百分比）
        返回:
            float: 大单占比维度总分
        """
        total = 0.0
        for ratio in daily_ratios:
            # 流入超过5%
            if ratio > 5:
                total += 40
            # 流入超过1%
            elif ratio > 1:
                total += 20
            # 流入 > 0
            elif ratio > 0:
                total += 10
            # 流入 < 0
            elif ratio < 0:
                total += -30
            # ratio == 0 时不加分不减分
        return total

    def _score_north_fund(
        self, stock_code: str, score_date: str = None
    ) -> Tuple[float, str]:
        """
        计算北向资金维度得分

        评分标准：
          没有持股：0分
          有持股基础分：50分
            增持：+50分 = 100分
            减持：-50分 = 0分
            不变：维持 = 50分

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期（YYYYMMDD 格式），透传给数据获取方法
        返回:
            Tuple[float, str]: (北向资金得分, 持股状态)
            状态: "none" / "increase" / "decrease" / "hold"
        """
        # 获取评分日期之前的北向资金数据（确保回测不用未来数据）
        df = self._fetch_north_fund_data(stock_code, score_date=score_date)

        # 没有数据，视为没有持股
        if df is None or df.empty:
            logger.debug(f"北向资金无持股: {stock_code}")
            return 0, "none"

        # 按日期排序（降序），取最近两条记录
        if "trade_date" in df.columns:
            df = df.sort_values("trade_date", ascending=False)
        # 获取最近一期持股量
        latest = df.iloc[0]
        # 获取持股量字段（兼容不同字段名）
        vol_field = "vol" if "vol" in df.columns else "ratio"
        latest_vol = float(latest.get(vol_field, 0))

        # 没有持股量
        if latest_vol <= 0:
            return 0, "none"

        # 有持股，基础分 50
        base_score = 50
        # 如果只有一条记录，无法判断增减持
        if len(df) < 2:
            logger.debug(f"北向资金仅一期数据: {stock_code}")
            return base_score, "hold"

        # 获取上一期持股量
        prev = df.iloc[1]
        prev_vol = float(prev.get(vol_field, 0))

        # 判断增减持
        if latest_vol > prev_vol:
            # 增持：+50分
            logger.debug(f"北向资金增持: {stock_code}")
            return base_score + 50, "increase"
        elif latest_vol < prev_vol:
            # 减持：-50分
            logger.debug(f"北向资金减持: {stock_code}")
            return base_score - 50, "decrease"
        else:
            # 不变：维持
            logger.debug(f"北向资金持平: {stock_code}")
            return base_score, "hold"

    def _score_north_fund_with_data(
        self, stock_code: str, north_df: Optional[pd.DataFrame]
    ) -> Tuple[float, str]:
        """
        使用已获取的北向资金数据计算得分

        这是 _score_north_fund 的优化版本，避免重复获取数据。

        评分标准：
          没有持股：0分
          有持股基础分：50分
            增持：+50分 = 100分
            减持：-50分 = 0分
            不变：维持 = 50分

        参数:
            stock_code: 股票代码（6位数字）
            north_df: 已获取的北向资金数据
        返回:
            Tuple[float, str]: (北向资金得分, 持股状态)
            状态: "none" / "increase" / "decrease" / "hold"
        """
        if north_df is None or north_df.empty:
            logger.debug(f"北向资金无持股: {stock_code}")
            return 0, "none"

        df = north_df.copy()
        
        if "trade_date" in df.columns:
            df = df.sort_values("trade_date", ascending=False)
        latest = df.iloc[0]
        vol_field = "vol" if "vol" in df.columns else "ratio"
        latest_vol = float(latest.get(vol_field, 0))

        if latest_vol <= 0:
            return 0, "none"

        base_score = 50
        if len(df) < 2:
            logger.debug(f"北向资金仅一期数据: {stock_code}")
            return base_score, "hold"

        prev = df.iloc[1]
        prev_vol = float(prev.get(vol_field, 0))

        if latest_vol > prev_vol:
            logger.debug(f"北向资金增持: {stock_code}")
            return base_score + 50, "increase"
        elif latest_vol < prev_vol:
            logger.debug(f"北向资金减持: {stock_code}")
            return base_score - 50, "decrease"
        else:
            logger.debug(f"北向资金持平: {stock_code}")
            return base_score, "hold"

    def _score_direction(self, large_net: float, small_net: float,
                         amount_5d: float = 0.0) -> float:
        """
        计算主力与散户方向维度得分

        评分标准：
          大单净流入 > 0 且 小单净流入 < 0：100分
          大单净流入 > 0 且 小单净流入 >= 0：60分
          大单净流入 < 0 且 小单净流入 > 0：-100分（出货方向；
              是否**否决**由 _is_distribution_veto 结合"占成交额比"判定）
          其他情况：0分

        参数:
            large_net: 大单净流入金额（万元）
            small_net: 小单净流入金额（万元）
        返回:
            float: 主力散户方向得分
        """
        # 大单流入 + 小单流出 = 主力吸筹
        if large_net > 0 and small_net < 0:
            return 100
        # 大单流入 + 小单也流入 = 共同看多
        if large_net > 0 and small_net >= 0:
            return 60
        # 大单流出 + 小单流入 = 出货信号（一票否决）
        if large_net < 0 and small_net > 0:
            return VETO_SCORE
        # 其他情况（大单流出 + 小单也流出等）
        return 0

    def _fetch_moneyflow_and_north_concurrent(
        self, stock_code: str, score_date: str
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """
        并发获取资金流向数据和北向资金数据

        使用 ThreadPoolExecutor 并发调用两个独立的 Tushare API，
        可以显著减少单只股票评分的 API 调用时间。

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期（YYYYMMDD 格式）
        返回:
            Tuple[DataFrame, DataFrame]: (资金流向数据, 北向资金数据)
        """
        formatted_date = self._format_date(score_date)
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_mf = executor.submit(self._fetch_moneyflow_data, stock_code, formatted_date)
            # 北向资金数据也使用评分日期，确保回测不用未来数据
            future_nf = executor.submit(self._fetch_north_fund_data, stock_code, formatted_date)
            
            try:
                df = future_mf.result(timeout=10)
            except Exception as e:
                logger.warning(f"获取资金流向数据超时或失败: {stock_code}, {e}")
                df = None
            
            try:
                north_df = future_nf.result(timeout=10)
            except Exception as e:
                logger.warning(f"获取北向资金数据超时或失败: {stock_code}, {e}")
                north_df = None
        
        return df, north_df

    def _extract_flow_metrics(
        self, df: pd.DataFrame
    ) -> Dict[str, float]:
        """
        从资金流向 DataFrame 中提取评分所需指标

        提取指标：
          - net_flow_5d: 5日主力净流入累计（万元）
          - daily_ratios: 每日大单净流入占比列表
          - large_net: 大单净流入累计（万元）
          - small_net: 小单净流入累计（万元）

        参数:
            df: 资金流向 DataFrame（Tushare 格式）
        返回:
            Dict: 包含各项指标的字典
        """
        metrics = {
            "net_flow_5d": 0.0,
            "daily_ratios": [],
            "large_net": 0.0,
            "small_net": 0.0,
        }

        if df is None or df.empty:
            return metrics

        # 根据数据格式自动选择提取方法
        # moneyflow_ths 接口返回 net_amount 字段
        # moneyflow 接口返回 net_mf_amount 字段，没有 net_amount
        if "net_amount" in df.columns:
            # moneyflow_ths 格式数据
            return self._extract_from_tushare(df)
        else:
            # moneyflow 格式数据（历史数据降级方案）
            return self._extract_from_moneyflow(df)

    def _extract_from_tushare(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        从 Tushare moneyflow_ths 格式数据中提取指标

        参数:
            df: Tushare 格式的资金流向 DataFrame
        返回:
            Dict: 评分指标字典
        """
        metrics = {
            "net_flow_5d": 0.0,
            "daily_ratios": [],
            "large_net": 0.0,
            "small_net": 0.0,
        }

        # 5日成交额（用于"净额占成交额比"的相对口径；缺失时保持 0 → 回退绝对阈值）
        if "amount" in df.columns:
            _amt = pd.to_numeric(df["amount"], errors='coerce').fillna(0).sum()
            metrics["amount_5d"] = float(_amt)

        # 5日主力净额直接使用Tushare已计算好的net_d5_amount字段
        # net_d5_amount是Tushare统一计算的5日主力净额，避免自己求和导致范围不一致
        if "net_d5_amount" in df.columns:
            net_d5_col = pd.to_numeric(df["net_d5_amount"], errors='coerce')
            metrics["net_flow_5d"] = float(net_d5_col.iloc[0])
        elif "net_amount" in df.columns:
            net_amount_col = pd.to_numeric(df["net_amount"], errors='coerce').fillna(0)
            metrics["net_flow_5d"] = float(net_amount_col.sum())
        elif "net_buy_amount" in df.columns:
            net_buy_col = pd.to_numeric(df["net_buy_amount"], errors='coerce').fillna(0)
            metrics["net_flow_5d"] = float(net_buy_col.sum())

        # 每日大单净流入占比
        if "buy_lg_amount_rate" in df.columns:
            metrics["daily_ratios"] = df["buy_lg_amount_rate"].fillna(0).tolist()
        elif "net_buy_rate" in df.columns:
            metrics["daily_ratios"] = df["net_buy_rate"].fillna(0).tolist()

        # 【2026-09-11】出货信号（条件2）直接用接口现成的"净流入占比"字段（%），
        #   无需拿 5 日成交额去算：
        #     buy_lg_amount_rate —— 大单净流入占比（负 = 净流出）
        #     buy_sm_amount_rate —— 小单净流入占比（正 = 净流入）
        #   取近 5 日均值，与"近5日"口径保持一致。
        if "buy_lg_amount_rate" in df.columns:
            metrics["lg_rate_avg"] = float(
                pd.to_numeric(df["buy_lg_amount_rate"],
                              errors='coerce').fillna(0).mean())
        if "buy_sm_amount_rate" in df.columns:
            metrics["sm_rate_avg"] = float(
                pd.to_numeric(df["buy_sm_amount_rate"],
                              errors='coerce').fillna(0).mean())

        # 大单净流入累计
        if "buy_lg_amount" in df.columns and "sell_lg_amount" in df.columns:
            # 计算大单净流入 = 买入 - 卖出
            large_buy = pd.to_numeric(df["buy_lg_amount"], errors='coerce').fillna(0).sum()
            large_sell = pd.to_numeric(df["sell_lg_amount"], errors='coerce').fillna(0).sum()
            metrics["large_net"] = float(large_buy - large_sell)
        elif "buy_lg_amount" in df.columns:
            # 如果只有买入字段，假设为净流入金额
            large_buy_col = pd.to_numeric(df["buy_lg_amount"], errors='coerce').fillna(0)
            metrics["large_net"] = float(large_buy_col.sum())

        # 小单净流入累计
        if "buy_sm_amount" in df.columns and "sell_sm_amount" in df.columns:
            # 计算小单净流入 = 买入 - 卖出
            small_buy = pd.to_numeric(df["buy_sm_amount"], errors='coerce').fillna(0).sum()
            small_sell = pd.to_numeric(df["sell_sm_amount"], errors='coerce').fillna(0).sum()
            metrics["small_net"] = float(small_buy - small_sell)
        elif "buy_sm_amount" in df.columns:
            # 如果只有买入字段，假设为净流入金额
            small_buy_col = pd.to_numeric(df["buy_sm_amount"], errors='coerce').fillna(0)
            metrics["small_net"] = float(small_buy_col.sum())

        return metrics

    def calculate_score(
        self, stock_code: str, score_date: str
    ) -> Tuple[float, MoneyflowDetail]:
        """
        计算指定股票在指定日期的资金面得分

        综合公式：
          资金面得分 = (主力净流入得分 × 0.35)
                     + (大单占比得分 × 0.20)
                     + (北向资金得分 × 0.20)
                     + (主力散户方向得分 × 0.25)

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期，格式 YYYY-MM-DD 或 YYYYMMDD
        返回:
            Tuple[float, MoneyflowDetail]: (资金面得分, 资金面详情对象)
        """
        logger.debug(f"开始计算资金面得分: {stock_code}, 日期: {score_date}")

        # 检查是否为交易日
        from utils.trade_date_utils import is_trading_day
        if not is_trading_day(score_date):
            logger.debug(f"日期 {score_date} 不是交易日，跳过资金面评分")
            detail = MoneyflowDetail()
            return 0, detail

        # 初始化详情对象
        detail = MoneyflowDetail()
        # 统一日期格式为 YYYYMMDD
        formatted_date = self._format_date(score_date)

        # 并发获取资金流向数据和北向资金数据
        df, north_df = self._fetch_moneyflow_and_north_concurrent(stock_code, formatted_date)
        
        # 提取资金流向评分指标
        metrics = self._extract_flow_metrics(df)
        
        # 保存主力净流入数据
        detail.main_net_flow = metrics["net_flow_5d"]

        # 先检查一票否决条件（使用已提取的指标，避免重复获取数据）
        is_veto, veto_reason = self.check_veto(stock_code, formatted_date, metrics)
        if is_veto:
            # 触发一票否决
            detail.veto = True
            detail.veto_reason = veto_reason
            logger.warning(f"股票 {stock_code} 资金面一票否决: {veto_reason}")
            return VETO_SCORE, detail

        # 1. 计算主力净流入得分
        net_flow_5d = metrics["net_flow_5d"]
        detail.main_net_flow = net_flow_5d
        main_score = self._score_main_net_flow(net_flow_5d)
        detail.main_net_flow_score = main_score

        # 检查主力净流入一票否决（与 check_veto 同一套联合判定，避免绕过相对口径）
        _veto, _reason = self._is_main_net_flow_veto(
            net_flow_5d, metrics.get("amount_5d", 0.0),
            metrics.get('lg_rate_avg'))
        if _veto:
            detail.veto = True
            detail.veto_reason = _reason
            logger.warning(f"股票 {stock_code} 主力净流入一票否决: {_reason}")
            return VETO_SCORE, detail
        if main_score == VETO_SCORE:
            # 绝对额超阈但"占成交额比"温和 → 豁免否决，按最低档（净流出）计分
            logger.info(f"股票 {stock_code} 5日主力净额超绝对阈但占比温和，"
                        f"按最低档 -20 计分（不否决）")
            main_score = -20
            detail.main_net_flow_score = main_score

        # 2. 计算大单占比得分
        daily_ratios = metrics["daily_ratios"]
        large_ratio_score = self._score_large_ratio(daily_ratios)
        detail.large_ratio_score = large_ratio_score

        # 3. 计算北向资金得分（使用已获取的 north_df）
        north_score, north_status = self._score_north_fund_with_data(stock_code, north_df)
        detail.north_fund_score = north_score
        detail.north_fund_status = north_status

        # 4. 计算主力散户方向得分（出货信号判定需要 5日成交额）
        large_net = metrics["large_net"]
        small_net = metrics["small_net"]
        amount_5d = metrics.get("amount_5d", 0.0)
        direction_score = self._score_direction(large_net, small_net, amount_5d)
        detail.direction_score = direction_score

        # 检查方向一票否决（出货信号；首选接口占比字段，口径与 check_veto 完全一致）
        _dv, _dreason = self._is_distribution_veto(
            large_net, small_net, amount_5d,
            metrics.get('lg_rate_avg'), metrics.get('sm_rate_avg'))
        if _dv:
            detail.veto = True
            detail.veto_reason = _dreason
            logger.warning(f"股票 {stock_code} 出货信号一票否决: {_dreason}")
            return VETO_SCORE, detail
        if direction_score == VETO_SCORE:
            # 出货方向但量级轻微 → 豁免否决，归入"其他情况"（0 分）
            logger.info(f"股票 {stock_code} 出货方向但量级轻微，按 0 分计（不否决）")
            direction_score = 0
            detail.direction_score = direction_score

        # 计算综合得分（加权求和）
        total_score = (
            main_score * WEIGHT_MAIN_NET_FLOW
            + large_ratio_score * WEIGHT_LARGE_RATIO
            + north_score * WEIGHT_NORTH_FUND
            + direction_score * WEIGHT_DIRECTION
        )
        # 四舍五入保留一位小数
        total_score = round(total_score, 1)

        # 记录最终得分
        logger.debug(
            f"股票 {stock_code} 资金面得分: {total_score} "
            f"(主力={main_score}, 大单={large_ratio_score}, "
            f"北向={north_score}, 方向={direction_score})"
        )
        return total_score, detail

    @staticmethod
    def _is_main_net_flow_veto(net_flow_5d: float,
                               amount_5d: float = 0.0,
                               lg_rate: float = None) -> Tuple[bool, str]:
        """判断"5日主力净额"是否触发一票否决（绝对额 + 相对占比 联合判定）

        相对占比的取值优先级（2026-09-11 实测：moneyflow_ths 接口**没有 amount 列**，
        故"净额/成交额"在真实数据下取不到 → 改用接口现成的占比字段优先）：
          1. 【首选】lg_rate = 接口 buy_lg_amount_rate 均值（大单净流入占比，%）
                否决 ⟺ lg_rate < VETO_NET_FLOW_RATIO_THRESHOLD
          2. 【回退】amount_5d > 0 → 占比 = 净额 / 5日成交额 × 100
          3. 【兜底】两者都取不到 → 按绝对额否决（兼容旧行为）
        """
        try:
            nf = float(net_flow_5d or 0)
        except (TypeError, ValueError):
            nf = 0.0
        if nf >= VETO_MAIN_NET_FLOW_THRESHOLD:
            return False, ""

        th = VETO_NET_FLOW_RATIO_THRESHOLD

        # ---- 1. 首选：接口现成的占比字段（大单净流入占比均值，%）----
        if th is not None and lg_rate is not None:
            try:
                lgr = float(lg_rate)
            except (TypeError, ValueError):
                lgr = None
            if lgr is not None:
                if lgr < th:
                    return True, (f"5日主力净额 {nf:.0f} 万元 < "
                                  f"{VETO_MAIN_NET_FLOW_THRESHOLD}万元，且大单净流入占比 "
                                  f"{lgr:.2f}% < {th}%")
                logger.info(
                    f"5日主力净额 {nf:.0f} 万元超绝对阈，但大单净流入占比 "
                    f"{lgr:.2f}% 未达 {th}%（相对温和）→ 判定为正常波动，不否决")
                return False, ""

        # ---- 2. 回退：净额 / 5日成交额 ----
        try:
            amt = float(amount_5d or 0)
        except (TypeError, ValueError):
            amt = 0.0

        if amt > 0 and th is not None:
            ratio = nf / amt * 100
            if ratio >= th:
                logger.info(
                    f"5日主力净额 {nf:.0f} 万元（占成交额 {ratio:.2f}%）"
                    f"未达相对阈 {th}%（成交额 {amt:.0f} 万元）→ 正常波动，不否决")
                return False, ""
            return True, (f"5日主力净额 {nf:.0f} 万元 < "
                          f"{VETO_MAIN_NET_FLOW_THRESHOLD}万元，且占成交额 "
                          f"{ratio:.2f}% < {th}%")

        # ---- 3. 兜底：绝对额 ----
        return True, (f"5日主力净额 {nf:.0f} 万元 < "
                      f"{VETO_MAIN_NET_FLOW_THRESHOLD}万元")

    @staticmethod
    def _is_distribution_veto(large_net: float, small_net: float,
                              amount_5d: float = 0.0,
                              lg_rate: float = None,
                              sm_rate: float = None) -> Tuple[bool, str]:
        """判断是否触发"出货信号"一票否决

        判定优先级：
          1. 【首选】接口直接提供的"净流入占比"字段（%），**无需自算成交额**：
                 buy_lg_amount_rate（大单净流入占比）< -阈值
                 且 buy_sm_amount_rate（小单净流入占比）>  阈值
          2. 【回退】金额 / 成交额（接口无占比字段时）：
                 大单净流出额/成交额 > 阈值 且 小单净流入额/成交额 > 阈值
          3. 【兜底】纯方向判定：大单净流入 < 0 且 小单净流入 > 0

        参数:
            large_net/small_net: 大单/小单净流入金额（万元）
            amount_5d: 5日成交额（万元，仅回退路径使用）
            lg_rate: 大单净流入占比均值（%，接口 buy_lg_amount_rate）
            sm_rate: 小单净流入占比均值（%，接口 buy_sm_amount_rate）
        """
        try:
            lg = float(large_net or 0)
        except (TypeError, ValueError):
            lg = 0.0
        try:
            sm = float(small_net or 0)
        except (TypeError, ValueError):
            sm = 0.0

        th = VETO_DISTRIBUTION_RATIO_THRESHOLD

        # ---- 1. 首选：接口现成的占比字段（%）----
        if th is not None and lg_rate is not None and sm_rate is not None:
            try:
                lgr = float(lg_rate)
                smr = float(sm_rate)
            except (TypeError, ValueError):
                lgr = smr = None
            if lgr is not None and smr is not None:
                if lgr < -th and smr > th:
                    return True, (f"出货信号：大单净流入占比 {lgr:.2f}% < -{th}% 且 "
                                  f"小单净流入占比 {smr:.2f}% > {th}%")
                logger.info(
                    f"出货信号未达阈值（大单净流入占比 {lgr:.2f}%、小单净流入占比 "
                    f"{smr:.2f}%；需 大单<-{th}% 且 小单>+{th}%）→ 不触发否决")
                return False, ""

        # ---- 2. 回退：金额 / 成交额 ----
        if not (lg < 0 and sm > 0):
            return False, ""

        try:
            amt = float(amount_5d or 0)
        except (TypeError, ValueError):
            amt = 0.0

        if amt > 0 and th is not None:
            lg_ratio = -lg / amt * 100      # 大单净流出占比（正值）
            sm_ratio = sm / amt * 100       # 小单净流入占比（正值）
            if lg_ratio > th and sm_ratio > th:
                return True, (f"出货信号：大单净流出占成交额 {lg_ratio:.2f}% 且 "
                              f"小单净流入占成交额 {sm_ratio:.2f}%"
                              f"（均 > {th}%）")
            logger.info(
                f"出货方向但量级轻微（大单净流出占成交额 {lg_ratio:.2f}%、"
                f"小单净流入占成交额 {sm_ratio:.2f}%，阈值 {th}%）→ 不触发否决")
            return False, ""

        return True, "出货信号：大单净流出且小单净流入"

    def check_veto(
        self, stock_code: str, score_date: str, metrics: dict = None
    ) -> Tuple[bool, str]:
        """
        检查资金面一票否决条件

        一票否决条件（2026-09-11 改为"绝对额/方向 + 占比强度"联合判定）：
          1. 5日主力净额 < -10000 万元 且 大单净流入占比 < -5%
          2. 大单净流出占比 > 1% 且 小单净流入占比 > 1%

        参数:
            stock_code: 股票代码（6位数字）
            score_date: 评分日期（YYYYMMDD 格式）
            metrics: 已提取的指标字典（可选，用于避免重复计算）
        返回:
            Tuple[bool, str]: (是否触发一票否决, 否决原因)
        """
        # 如果没有传入指标，先获取数据并提取指标
        if metrics is None:
            # 统一日期格式
            formatted_date = self._format_date(score_date)
            # 获取资金流向数据
            df = self._fetch_moneyflow_data(stock_code, formatted_date)
            # 提取评分指标
            metrics = self._extract_flow_metrics(df)

        # 条件1：5日主力净额超阈（绝对额 + 相对占比联合判定，避免大成交额错杀；
        #        相对占比首选接口 buy_lg_amount_rate，ths 接口无成交额字段）
        net_flow_5d = metrics["net_flow_5d"]
        amount_5d = metrics.get("amount_5d", 0.0)
        is_veto, reason = self._is_main_net_flow_veto(
            net_flow_5d, amount_5d, metrics.get('lg_rate_avg'))
        if is_veto:
            logger.warning(f"股票 {stock_code} 一票否决: {reason}")
            return True, reason

        # 条件2：出货信号（首选接口占比字段：大单净流入占比 < -1% 且 小单净流入占比 > 1%）
        large_net = metrics["large_net"]
        small_net = metrics["small_net"]
        is_veto, reason = self._is_distribution_veto(
            large_net, small_net, amount_5d,
            metrics.get('lg_rate_avg'), metrics.get('sm_rate_avg'))
        if is_veto:
            logger.warning(f"股票 {stock_code} 一票否决: {reason}")
            return True, reason

        # 未触发一票否决
        return False, ""
