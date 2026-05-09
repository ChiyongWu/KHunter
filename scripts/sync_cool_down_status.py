#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量检查股票池中的资金流向冷却状态并补充标记

使用方法:
    python scripts/sync_cool_down_status.py
"""

import json
import sys
import os
import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.moneyflow_scorer import MoneyflowScorer
from utils.trade_date_utils import is_trading_day, get_previous_trading_day


def get_future_trading_day(start_date: str, days: int) -> str:
    """获取指定日期之后的第N个交易日"""
    current_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
    trading_days_found = 0
    
    while trading_days_found < days:
        current_date += datetime.timedelta(days=1)
        if is_trading_day(current_date.strftime('%Y-%m-%d')):
            trading_days_found += 1
    
    return current_date.strftime('%Y-%m-%d')


def sync_cool_down_status():
    """批量检查股票池中的资金流向冷却状态并补充标记"""
    pool_file = "data/running/buy_candidate_pool.json"
    
    if not os.path.exists(pool_file):
        print(f"错误：股票池文件不存在: {pool_file}")
        return
    
    # 读取股票池
    with open(pool_file, 'r', encoding='utf-8') as f:
        pool_data = json.load(f)
    
    pool = pool_data.get('pool', [])
    last_date = pool_data.get('last_date', '')
    
    if not last_date:
        print("错误：股票池文件中没有日期信息")
        return
    
    print(f"股票池日期: {last_date}")
    print(f"股票池股票数量: {len(pool)}")
    print("=" * 60)
    
    date_str = last_date.replace('-', '')
    print(f"检查资金流向日期: {last_date}")
    
    # 配置阈值
    net_flow_threshold = -10000  # 5日主力净额阈值（万元）
    
    scorer = MoneyflowScorer()
    updated_count = 0
    cooling_count = 0
    
    for item in pool:
        stock = item.get('stock', item)
        stock_code = stock.get('stock_code', '')
        stock_name = stock.get('stock_name', '')
        
        if not stock_code:
            continue
        
        # 获取资金流向数据
        try:
            df = scorer._fetch_moneyflow_data(stock_code, date_str)
            if df is None or df.empty:
                continue
            
            # 提取指标
            metrics = scorer._extract_flow_metrics(df)
            net_flow_5d = metrics['net_flow_5d']
            large_net = metrics['large_net']
            small_net = metrics['small_net']
            
            # 判断冷却条件
            condition1 = net_flow_5d < net_flow_threshold  # 5日主力净额 < 阈值
            condition2 = (large_net < 0) and (small_net > 0)  # 大单出+小单进
            
            if condition1 or condition2:
                # 构建原因
                if condition1 and condition2:
                    reason = f"5日主力净额{net_flow_5d:.0f}万元<{net_flow_threshold}万元且大单净流出小单净流入"
                elif condition1:
                    reason = f"5日主力净额{net_flow_5d:.0f}万元<{net_flow_threshold}万元"
                else:
                    reason = "大单净流出且小单净流入（出货信号）"
                
                # 设置冷却状态
                item['is_cooling'] = True
                # 计算冷却结束日期（3个交易日后）
                cool_down_end = get_future_trading_day(last_date, 3)
                item['cool_down_end'] = cool_down_end
                
                print(f"【标记冷却】{stock_code} {stock_name}: {reason}，冷却至 {cool_down_end}")
                updated_count += 1
                cooling_count += 1
            else:
                # 清除冷却状态
                if item.get('is_cooling', False):
                    item['is_cooling'] = False
                    item['cool_down_end'] = None
                    print(f"【清除冷却】{stock_code} {stock_name}: 资金流向正常")
                    updated_count += 1
        
        except Exception as e:
            print(f"【检查失败】{stock_code} {stock_name}: {str(e)}")
            continue
    
    # 保存更新后的股票池
    if updated_count > 0:
        pool_data['updated_at'] = f"{last_date} 资金流向冷却状态批量更新"
        
        with open(pool_file, 'w', encoding='utf-8') as f:
            json.dump(pool_data, f, ensure_ascii=False, indent=2, default=str)
        
        print("=" * 60)
        print(f"更新完成！共检查 {len(pool)} 只股票")
        print(f"更新了 {updated_count} 只股票的冷却状态")
        print(f"其中 {cooling_count} 只标记为冷却中")
    else:
        print("=" * 60)
        print(f"检查完成！共检查 {len(pool)} 只股票")
        print("没有需要更新的冷却状态")


if __name__ == "__main__":
    sync_cool_down_status()