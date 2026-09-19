# -*- coding: utf-8 -*-
"""
评分数据本地落地缓存模块（方案A：批量落地 + 本地优先）

将评分器消费的 Tushare 原始响应（DataFrame 记录）无损落地到 SQLite，
评分阶段本地优先读取，缺失/过期时回退远程拉取并落库。

设计原则（参考 trading.reduce_plan_cache 的数据层/评分层解耦模式）：
1. 原始数据无损存储 —— 缓存的是 API 原始返回记录（JSON），评分器本地过滤，
   保证本地命中与远程调用的评分结果一致
2. 新鲜度按 fetch_date 管理 —— 历史数据不可变：
   - 快照型（forecast/stk_holdertrade/fina_indicator/ths_member 等全量历史接口）：
     fetch_date >= score_date 即命中（评分日数据均已落地）
   - 覆盖型（moneyflow_ths/moneyflow/hk_hold/block_trade 等区间接口）：
     请求区间被已落地区间覆盖即命中，不足时只增量拉取缺口并按交易日合并
   - 精确键型（top_list/ths_daily/moneyflow_cnt_ths 按交易日接口）：
     历史日期不可变永久缓存；当日数据不缓存（与原"今日跳过缓存"行为一致）
3. 失败不落库 —— 远程拉取失败返回 None 时不写缓存，避免假阴性污染
4. 缓存层任何异常都不影响评分 —— 全部操作兜底 try/except，异常时自动走远程

表结构：
    score_api_cache(
        api_name, cache_key, payload,      -- API 名 / 缓存键 / JSON 记录
        range_start, range_end,            -- 覆盖型接口的已落地区间（快照型为 NULL）
        fetch_date,                        -- 落地日期 YYYYMMDD（新鲜度判断依据）
        updated_at,                        -- 更新时间
        UNIQUE(api_name, cache_key)
    )
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from utils.db_manager import DBManager

# 配置日志记录器
logger = logging.getLogger(__name__)

# 缓存表名
CACHE_TABLE = "score_api_cache"


class BacktestDataCache:
    """
    评分数据本地落地缓存（SQLite）

    提供三种读取模式，统一返回 DataFrame 或 None：
      - fetch_asof : 快照型接口（全量历史，本地按日期过滤）
      - fetch_range: 覆盖型接口（区间数据，增量扩展）
      - fetch_key  : 精确键型接口（按交易日，历史不可变）

    返回值约定：
      - DataFrame（可能为空）: 数据就绪（本地命中或远程拉取成功）
      - None                : 远程拉取失败（不落库，调用方按无数据处理）
    """

    def __init__(self, db_manager: DBManager = None):
        """
        初始化落地缓存

        参数:
            db_manager: 数据库管理器实例，为 None 时创建默认实例
        """
        self.db = db_manager or DBManager()
        self._ensure_tables()
        # 命中统计（用于日志观察效果）：本地命中/远程拉取/落库次数
        self.stats = {"hit": 0, "remote": 0, "persist": 0}
        # 覆盖型接口的区间尾端扩展目标（YYYYMMDD，由回测引擎在启动时设置为回测结束日）：
        # 缺口拉取时一次拉到该日为止，后续评分日窗口右移全部命中，消除逐日增量远程调用
        self.range_extend_end = None
        logger.info("评分数据本地落地缓存初始化完成")

    def _ensure_tables(self):
        """确保缓存表存在（幂等）"""
        self.db.execute(f"""
            CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_name TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                range_start TEXT,
                range_end TEXT,
                fetch_date TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(api_name, cache_key)
            )
        """)
        self.db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_score_api_cache_key "
            f"ON {CACHE_TABLE}(api_name, cache_key)"
        )

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        """当前日期（YYYYMMDD）"""
        return datetime.now().strftime("%Y%m%d")

    @staticmethod
    def _norm_date(date_str: str) -> str:
        """日期归一化为 YYYYMMDD（兼容 YYYY-MM-DD）"""
        return str(date_str).replace("-", "") if date_str else ""

    def _get_row(self, api_name: str, cache_key: str) -> Optional[dict]:
        """读取缓存行（无则 None）"""
        rows = self.db.query(
            f"SELECT payload, range_start, range_end, fetch_date FROM {CACHE_TABLE} "
            f"WHERE api_name = ? AND cache_key = ?",
            (api_name, cache_key),
        )
        return rows[0] if rows else None

    @staticmethod
    def _payload_to_df(payload: str) -> pd.DataFrame:
        """JSON 记录还原为 DataFrame（空记录返回空 DataFrame）"""
        records = json.loads(payload) if payload else []
        return pd.DataFrame(records)

    @staticmethod
    def _df_to_payload(df: pd.DataFrame) -> str:
        """DataFrame 序列化为 JSON 记录"""
        return df.to_json(orient="records", force_ascii=False)

    def _save(self, api_name: str, cache_key: str, payload: str,
              fetch_date: str, range_start: str = None, range_end: str = None):
        """写入/覆盖缓存行（失败仅告警，不影响评分）

        必须走显式事务（begin_transaction → execute → commit）：
        Python sqlite3 对 INSERT 自动开启事务，execute/execute_with_retry 均不 commit，
        若直接写会导致连接长期悬挂持有 SQLite 写锁（其他连接 database is locked），
        且数据不落盘（跨进程不可见）。
        begin/commit 支持嵌套：若调用方已处于事务中，仅在外层事务结束时统一提交。
        """
        owned = self.db._transaction_count == 0
        try:
            self.db.begin_transaction()
            self.db.execute(
                f"INSERT OR REPLACE INTO {CACHE_TABLE} "
                f"(api_name, cache_key, payload, range_start, range_end, fetch_date, updated_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (api_name, cache_key, payload, range_start, range_end, fetch_date),
            )
            self.db.commit()
            self.stats["persist"] += 1
        except Exception as e:
            # 仅在事务由本方法发起时回滚，避免误伤外层事务
            if owned:
                try:
                    self.db.rollback()
                except Exception:
                    pass
            logger.warning(f"评分缓存落库失败（忽略，走远程数据）: {api_name}/{cache_key}, {e}")

    # ------------------------------------------------------------
    # 模式一：快照型接口（全量历史，本地按日期过滤）
    # ------------------------------------------------------------

    def fetch_asof(self, api_name: str, cache_key: str, score_date: str,
                   remote_fn: Callable[[], Optional[pd.DataFrame]]) -> Optional[pd.DataFrame]:
        """
        快照型读取：数据为"截至 fetch_date 的全量历史"，评分日 <= fetch_date 即有效。

        适用：forecast / stk_holdertrade / repurchase / stk_shock /
             fina_indicator / ths_member / ths_index 等全量历史接口。
        调用方拿到 DataFrame 后自行按 ann_date/end_date 过滤评分日，
        与原"远程拉全量再过滤"的逻辑完全一致。

        参数:
            api_name:   API 名称（缓存命名空间）
            cache_key:  缓存键（通常为 ts_code）
            score_date: 评分日期（YYYYMMDD）
            remote_fn:  远程拉取函数（返回 DataFrame，失败返回 None）
        返回:
            DataFrame（可能为空）或 None（远程失败）
        """
        try:
            score_date = self._norm_date(score_date) or self._today()
            row = self._get_row(api_name, cache_key)
            # 新鲜度：落地日不早于评分日（历史公告均已包含，本地过滤不漏不超）
            if row is not None and row.get("fetch_date", "") >= score_date:
                self.stats["hit"] += 1
                return self._payload_to_df(row["payload"])

            # 未命中或过期：远程拉取
            df = remote_fn()
            if df is None:
                # 远程失败：不落库（沿用"无缓存"状态，避免假阴性污染）
                return None
            self.stats["remote"] += 1
            self._save(api_name, cache_key, self._df_to_payload(df), self._today())
            return df
        except Exception as e:
            logger.warning(f"快照缓存读取异常（回退远程）: {api_name}/{cache_key}, {e}")
            return remote_fn()

    # ------------------------------------------------------------
    # 模式二：覆盖型接口（区间数据，增量扩展）
    # ------------------------------------------------------------

    def fetch_range(self, api_name: str, cache_key: str,
                    start_date: str, end_date: str,
                    remote_fn: Callable[[str, str], Optional[pd.DataFrame]],
                    merge_key: str = "trade_date") -> Optional[pd.DataFrame]:
        """
        覆盖型读取：按已落地区间 [range_start, range_end] 管理覆盖度。

        适用：moneyflow_ths / moneyflow / hk_hold / block_trade 等区间接口。
        - 请求区间被覆盖 → 本地过滤直接返回（历史数据不可变）
        - 未覆盖 → 只拉缺口（通常为向后扩展一天），按 merge_key 合并后落库
        - remote_fn(start, end) 负责实际远程调用

        参数:
            api_name:  API 名称
            cache_key: 缓存键（通常为 ts_code）
            start_date: 请求起始日期（YYYYMMDD）
            end_date:   请求结束日期（YYYYMMDD）
            remote_fn:  远程拉取函数 fn(start_date, end_date) -> DataFrame/None
            merge_key:  行去重合并键字段名（默认 trade_date）
        返回:
            DataFrame（过滤到请求区间，可能为空）或 None（远程失败）
        """
        try:
            start_date = self._norm_date(start_date)
            end_date = self._norm_date(end_date)
            if not start_date or not end_date:
                return remote_fn(start_date, end_date)

            row = self._get_row(api_name, cache_key)
            if row is not None and row.get("range_start") and row.get("range_end"):
                cov_start, cov_end = row["range_start"], row["range_end"]
                # 区间被覆盖：当日数据（end_date >= 今天）要求当日落地，其余历史不可变
                today = self._today()
                if start_date >= cov_start and end_date <= cov_end:
                    if end_date < today or row.get("fetch_date", "") >= today:
                        self.stats["hit"] += 1
                        df = self._payload_to_df(row["payload"])
                        return self._filter_df_range(df, merge_key, start_date, end_date)

                # 未覆盖：计算缺口拉取范围
                if cov_start <= start_date <= cov_end and end_date > cov_end:
                    # 向后扩展：只拉已覆盖上界之后的缺口
                    fetch_start = self._shift_days(cov_end, 1)
                elif start_date > cov_end:
                    # 区间不连续：放弃旧覆盖，整体重拉
                    cov_start, cov_end, old_records = None, None, []
                    fetch_start = start_date
                else:
                    # 向前扩展（少见）：整体重拉请求区间
                    fetch_start = start_date
                # 缺口拉取上界扩展：一次拉到回测结束日（不超过今天），
                # 后续评分日窗口右移全部命中，消除逐日增量远程调用
                fetch_end = self._extend_end(end_date)
            else:
                cov_start, cov_end = None, None
                old_records = []
                fetch_start = start_date
                fetch_end = self._extend_end(end_date)

            # 远程拉缺口
            df_new = remote_fn(fetch_start, fetch_end)
            if df_new is None:
                return None
            self.stats["remote"] += 1

            # 合并旧记录（若有）并扩展覆盖区间
            new_records = json.loads(self._df_to_payload(df_new))
            if cov_start is not None:
                old_records = json.loads(row["payload"])
            merged = self._merge_records(old_records, new_records, merge_key)
            new_cov_start = min(start_date, cov_start) if cov_start else start_date
            new_cov_end = max(fetch_end, cov_end) if cov_end else fetch_end
            self._save(api_name, cache_key, json.dumps(merged, ensure_ascii=False),
                       self._today(), new_cov_start, new_cov_end)

            df = pd.DataFrame(merged)
            return self._filter_df_range(df, merge_key, start_date, end_date)
        except Exception as e:
            logger.warning(f"覆盖缓存读取异常（回退远程）: {api_name}/{cache_key}, {e}")
            return remote_fn(start_date, end_date)

    def _extend_end(self, end_date: str) -> str:
        """缺口拉取上界：扩展到 range_extend_end（回测结束日），但不早于请求上界、不晚于今天"""
        limit = self.range_extend_end
        if limit:
            return max(end_date, min(limit, self._today()))
        return end_date

    @staticmethod
    def _shift_days(date_str: str, days: int) -> str:
        """日期平移 N 天（YYYYMMDD）"""
        dt = datetime.strptime(date_str, "%Y%m%d")
        return (dt + timedelta(days=days)).strftime("%Y%m%d")

    @staticmethod
    def _merge_records(old_records: list, new_records: list, merge_key: str) -> list:
        """按 merge_key 合并记录（新记录覆盖同键旧记录）"""
        if not merge_key:
            return list(old_records) + list(new_records)
        merged = {str(r.get(merge_key)): r for r in old_records if isinstance(r, dict)}
        for r in new_records:
            if isinstance(r, dict):
                merged[str(r.get(merge_key))] = r
        # 按合并键排序，保持时间升序稳定输出
        return [merged[k] for k in sorted(merged.keys())]

    @staticmethod
    def _filter_df_range(df: pd.DataFrame, date_col: str,
                         start_date: str, end_date: str) -> pd.DataFrame:
        """按日期列过滤 DataFrame 到请求区间（无该列时原样返回）"""
        if df is None or df.empty or date_col not in df.columns:
            return df
        dates = df[date_col].astype(str).str.replace("-", "")
        return df[(dates >= start_date) & (dates <= end_date)]

    # ------------------------------------------------------------
    # 模式三：精确键型接口（按交易日，历史不可变）
    # ------------------------------------------------------------

    def fetch_key(self, api_name: str, cache_key: str, trade_date: str,
                  remote_fn: Callable[[], Optional[pd.DataFrame]]) -> Optional[pd.DataFrame]:
        """
        精确键型读取：以 (api_name, cache_key) 精确缓存单次 API 响应。

        适用：top_list / ths_daily / moneyflow_cnt_ths 等按 trade_date 查询的接口。
        - 历史日期（< 今天）：数据不可变，永久缓存（含确认空结果）
        - 当日数据：不读持久缓存，直接远程（与原"今日跳过缓存"行为一致）

        参数:
            api_name:   API 名称
            cache_key:  缓存键（通常为 f"{ts_code}_{trade_date}" 或 trade_date）
            trade_date: 数据交易日（YYYYMMDD，用于历史/当日判定）
            remote_fn:  远程拉取函数
        返回:
            DataFrame（可能为空）或 None（远程失败）
        """
        try:
            trade_date = self._norm_date(trade_date)
            if trade_date and trade_date < self._today():
                row = self._get_row(api_name, cache_key)
                if row is not None:
                    self.stats["hit"] += 1
                    return self._payload_to_df(row["payload"])
            # 当日或未缓存：远程拉取
            df = remote_fn()
            if df is None:
                return None
            self.stats["remote"] += 1
            self._save(api_name, cache_key, self._df_to_payload(df), self._today())
            return df
        except Exception as e:
            logger.warning(f"精确键缓存读取异常（回退远程）: {api_name}/{cache_key}, {e}")
            return remote_fn()
