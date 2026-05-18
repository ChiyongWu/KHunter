#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
移除冷却期股票的买入信号

使用方法:
    python scripts/remove_cooling_signals.py
"""

import json
import os


def remove_cooling_signals():
    """移除冷却期股票的买入信号"""
    pool_file = "data/running/buy_candidate_pool.json"
    signals_dir = "data/running"
    
    # 读取股票池
    with open(pool_file, 'r', encoding='utf-8') as f:
        pool_data = json.load(f)
    
    pool = pool_data.get('pool', [])
    
    # 获取冷却股票代码集合
    cooling_stocks = set()
    for item in pool:
        if item.get('is_cooling', False):
            stock = item.get('stock', item)
            stock_code = stock.get('stock_code', '')
            cooling_stocks.add(stock_code)
    
    if not cooling_stocks:
        print("没有冷却股票，无需处理")
        return
    
    print(f"冷却股票 ({len(cooling_stocks)} 只): {', '.join(sorted(cooling_stocks))}")
    print("=" * 60)
    
    # 只处理5月8日的信号文件
    target_date = "2026-05-08"
    signals_file = os.path.join(signals_dir, f"signals_{target_date}.json")
    
    if not os.path.exists(signals_file):
        print(f"信号文件不存在: {signals_file}")
        return
    
    with open(signals_file, 'r', encoding='utf-8') as f:
        signals = json.load(f)
    
    # 过滤掉冷却股票的买入信号
    original_count = len(signals)
    new_signals = [s for s in signals if not (
        s.get('signal_type') == 'buy' and s.get('stock_code', '') in cooling_stocks
    )]
    
    if len(new_signals) < original_count:
        removed = original_count - len(new_signals)
        print(f"【{target_date}】移除 {removed} 个冷却股票买入信号")
        
        with open(signals_file, 'w', encoding='utf-8') as f:
            json.dump(new_signals, f, ensure_ascii=False, indent=2)
        print(f"已恢复 {original_count - removed} 个有效信号")
    else:
        print(f"【{target_date}】没有需要移除的冷却股票买入信号")


if __name__ == "__main__":
    remove_cooling_signals()