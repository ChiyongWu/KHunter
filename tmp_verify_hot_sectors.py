#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时验证脚本：确认建表/手动触发热门板块落库并检查结果（验证后删除）"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

from utils.db_manager import DBManager


def check_table():
    db = DBManager()
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='sector_hot_rank'")
    print(f"建表检查: {rows}")
    assert rows, "sector_hot_rank 表不存在！"


def fetch_and_check(trade_date):
    from utils.sector_hot_rank_fetcher import SectorHotRankFetcher
    db = DBManager()
    saved = SectorHotRankFetcher(db).fetch_and_store(trade_date)
    print(f"\n写入条数: {saved}")

    rows = db.query(
        "SELECT sector_type, COUNT(*) AS cnt FROM sector_hot_rank WHERE trade_date = ? GROUP BY sector_type",
        (trade_date,)
    )
    for r in rows:
        print(f"{r['sector_type']}: {r['cnt']} 条")

    dup = db.query(
        "SELECT sector_code, COUNT(*) AS cnt FROM sector_hot_rank GROUP BY sector_code, trade_date HAVING cnt > 1"
    )
    print(f"唯一性检查（应无输出）: {dup}")

    top = db.query(
        "SELECT sector_name, sector_type, pct_chg, main_net_flow FROM sector_hot_rank "
        "WHERE trade_date = ? AND sector_type = 'concept' ORDER BY main_net_flow DESC LIMIT 5",
        (trade_date,)
    )
    print("\n概念板块 Top5（按主力净流入降序）:")
    for r in top:
        print(f"  {r['sector_name']}  {r['pct_chg']}%  {r['main_net_flow'] / 1e8:.2f}亿")


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'fetch':
        fetch_and_check(sys.argv[2])
    else:
        check_table()
