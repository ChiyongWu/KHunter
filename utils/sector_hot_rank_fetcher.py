#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热门板块数据采集器

通过 Tushare 通达信接口抓取板块行情（含年初至今涨跌幅），写入 sector_hot_rank 表，
供仪表盘"热门板块"卡片展示最近交易日的热门概念/行业板块。

数据源（每个交易日约 2 次请求）：
- tdx_index  板块清单（idx_type 概念板块/行业板块）
- tdx_daily  板块日行情（pct_change 当日涨跌幅、ytd 年初至今涨跌幅%）
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from utils.stock_data_fetcher import _tushare_limiter
from utils.tushare_client import get_tushare_pro

logger = logging.getLogger(__name__)


class SectorHotRankFetcher:
    """热门板块数据采集与落库"""

    # 通达信 idx_type → 库内 sector_type 映射
    IDX_TYPE_MAP = {
        '概念板块': 'concept',
        '行业板块': 'industry',
    }

    def __init__(self, db_manager):
        """
        参数：
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager

    def fetch_and_store(self, trade_date: str) -> int:
        """
        抓取指定交易日热门板块数据并写入 sector_hot_rank

        参数：
            trade_date: 交易日期，格式 YYYY-MM-DD

        返回：
            写入条数；任一数据源失败返回 0（整体跳过本次落库，数据停留在上一交易日）
        """
        ts_date = trade_date.replace('-', '')
        try:
            df_index = self._fetch_sector_index()
            df_daily = self._fetch_sector_daily(ts_date)

            if df_index is None or df_index.empty \
                    or df_daily is None or df_daily.empty:
                logger.warning(f"热门板块数据不完整（trade_date={trade_date}），跳过本次落库")
                return 0

            return self._merge_and_store(trade_date, df_index, df_daily)
        except Exception as e:
            logger.error(f"热门板块数据落库失败: {trade_date}, {e}")
            return 0

    def _fetch_sector_index(self) -> Optional[pd.DataFrame]:
        """获取通达信板块清单（全量，仅保留概念板块与行业板块）"""
        try:
            _tushare_limiter.wait_if_needed()
            pro = get_tushare_pro()
            if pro is None:
                logger.error("未配置Tushare api_key")
                return None
            df = pro.tdx_index()
            if df is not None and not df.empty and 'idx_type' in df.columns:
                df = df[df['idx_type'].isin(self.IDX_TYPE_MAP.keys())]
            return df
        except Exception as e:
            logger.error(f"获取板块清单失败: {e}")
            return None

    def _fetch_sector_daily(self, ts_date: str) -> Optional[pd.DataFrame]:
        """获取指定交易日全部通达信板块日行情（含 ytd 年初至今涨跌幅）"""
        try:
            _tushare_limiter.wait_if_needed()
            pro = get_tushare_pro()
            if pro is None:
                logger.error("未配置Tushare api_key")
                return None
            return pro.tdx_daily(trade_date=ts_date)
        except Exception as e:
            logger.error(f"获取板块行情失败: {ts_date}, {e}")
            return None

    def _merge_and_store(self, trade_date: str, df_index: pd.DataFrame,
                         df_daily: pd.DataFrame) -> int:
        """按板块代码拼装清单与行情并写入数据库"""
        # 板块代码 → (名称, 类型)
        index_map = {}
        for _, row in df_index.iterrows():
            code = str(row.get('ts_code', '')).strip()
            name = str(row.get('name', '')).strip()
            sec_type = self.IDX_TYPE_MAP.get(str(row.get('idx_type', '')).strip())
            if sec_type is None:
                # 与 _fetch_sector_index 的 idx_type 过滤闭环：类型异常时跳过而非误标
                continue
            if code and name:
                index_map[code] = (name, sec_type)

        # 整批写入使用显式事务：execute_with_retry 不 commit，
        # 若不手动管理会导致悬挂写事务（与 fund_flow_fetcher 落库写法一致）
        if 'pct_chg' in df_daily.columns:
            pct_col = 'pct_chg'
        elif 'pct_change' in df_daily.columns:
            pct_col = 'pct_change'
        else:
            logger.warning(f"板块行情缺少涨跌幅列（trade_date={trade_date}），跳过本次落库")
            return 0

        saved_count = 0
        self.db_manager.begin_transaction()
        try:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for _, row in df_daily.iterrows():
                code = str(row.get('ts_code', '')).strip()
                if code not in index_map:
                    continue
                name, sec_type = index_map[code]
                pct_val = row.get(pct_col)
                pct_chg = float(pct_val) if pd.notna(pct_val) else 0.0
                ytd_val = row.get('ytd')
                ytd_pct_chg = float(ytd_val) if pd.notna(ytd_val) else None

                # 同一交易日重跑按 UNIQUE(trade_date, sector_code) 覆盖更新
                check_sql = """
                    SELECT id FROM sector_hot_rank
                    WHERE trade_date = ? AND sector_code = ?
                """
                existing = self.db_manager.query_one(check_sql, (trade_date, code))
                if existing:
                    update_sql = """
                        UPDATE sector_hot_rank
                        SET sector_name = ?, sector_type = ?, pct_chg = ?,
                            ytd_pct_chg = ?, main_net_flow = NULL, created_date = ?
                        WHERE trade_date = ? AND sector_code = ?
                    """
                    self.db_manager.execute_with_retry(update_sql, (
                        name, sec_type, pct_chg, ytd_pct_chg, now_str,
                        trade_date, code
                    ))
                else:
                    insert_sql = """
                        INSERT INTO sector_hot_rank
                        (trade_date, sector_code, sector_name, sector_type,
                         pct_chg, ytd_pct_chg, main_net_flow, created_date)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    """
                    self.db_manager.execute_with_retry(insert_sql, (
                        trade_date, code, name, sec_type,
                        pct_chg, ytd_pct_chg, now_str
                    ))
                saved_count += 1

            self.db_manager.commit()
            logger.info(f"热门板块数据保存完成: {trade_date}, {saved_count} 条")
        except Exception as e:
            try:
                self.db_manager.rollback()
            except Exception:
                pass
            logger.error(f"热门板块数据写入失败: {trade_date}, {e}")
            return 0

        return saved_count
