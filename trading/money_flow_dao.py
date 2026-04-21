# -*- coding: utf-8 -*-
"""
资金流向数据访问层
用于获取同花顺个股资金流向数据
"""
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


class MoneyFlowDAO:
    """资金流向数据访问层"""

    def __init__(self):
        """初始化资金流向DAO"""
        try:
            import tushare as ts
            self.tushare = ts
            self.pro = ts.pro_api()
        except Exception as e:
            logger.error(f"Tushare初始化失败: {e}")
            self.pro = None

    def get_trade_dates(self, end_date: str = None, days: int = 10) -> List[str]:
        """
        获取指定日期前N个交易日列表

        Args:
            end_date: 结束日期（YYYYMMDD），默认今日
            days: 交易日数量

        Returns:
            交易日列表（按日期升序）
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        dates = []
        current = datetime.strptime(end_date, '%Y%m%d')

        while len(dates) < days:
            # 排除周末
            if current.weekday() < 5:
                dates.append(current.strftime('%Y%m%d'))
            current -= timedelta(days=1)

        return dates  # 返回升序列表

    def get_daily_money_flow(self, trade_date: str) -> pd.DataFrame:
        """
        获取单日全市场资金流向数据

        Args:
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            资金流向DataFrame
        """
        if self.pro is None:
            return pd.DataFrame()

        try:
            df = self.pro.moneyflow_ths(trade_date=trade_date)
            if df is None or df.empty:
                return pd.DataFrame()
            return df
        except Exception as e:
            logger.error(f"获取 {trade_date} 资金流向失败: {e}")
            return pd.DataFrame()

    def get_stock_money_flow(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取单只股票指定期间资金流向

        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            资金流向DataFrame
        """
        if self.pro is None:
            return pd.DataFrame()

        try:
            df = self.pro.moneyflow_ths(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date
            )
            if df is None or df.empty:
                return pd.DataFrame()
            return df.sort_values('trade_date')  # 按日期升序
        except Exception as e:
            logger.error(f"获取 {ts_code} 资金流向失败: {e}")
            return pd.DataFrame()

    def select_continuous_inflow_stocks(
        self,
        end_date: str = None,
        days: int = 10,
        min_net_amount: float = 0
    ) -> List[Dict]:
        """
        筛选连续N日净流入股票

        Args:
            end_date: 结束日期（YYYYMMDD），默认今日
            days: 连续天数要求
            min_net_amount: 最小日均净流入(万元)

        Returns:
            股票信息列表
        """
        if self.pro is None:
            return []

        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        # 获取交易日列表
        trade_dates = self.get_trade_dates(end_date, days)
        if not trade_dates:
            logger.warning("无有效交易日")
            return []

        logger.info(f"查询日期范围: {trade_dates[0]} ~ {trade_dates[-1]}")

        # 获取每日资金流向数据
        all_data = {}
        for trade_date in trade_dates:
            df = self.get_daily_money_flow(trade_date)
            if not df.empty:
                # 筛选净流入 > 0 的股票
                inflow_df = df[df['net_amount'] > 0]
                all_data[trade_date] = set(inflow_df['ts_code'].tolist())
                logger.info(f"{trade_date}: 全市场{len(df)}只, 净流入{len(inflow_df)}只")
            else:
                all_data[trade_date] = set()

        # 取交集：所有日期都有净流入的股票
        common_stocks = None
        for i, trade_date in enumerate(trade_dates):
            if i == 0:
                common_stocks = all_data.get(trade_date, set()).copy()
            else:
                common_stocks = common_stocks & all_data.get(trade_date, set())

        if not common_stocks:
            logger.info("无连续净流入股票")
            return []

        logger.info(f"初步筛选: {len(common_stocks)} 只股票在{len(trade_dates)}日内均有净流入")

        # 获取详细数据并计算真实连续天数
        results = []
        # latest_date应该是最近日期（列表第一个），而非最远日期（列表最后一个）
        latest_date = trade_dates[-1]  # 这是日期范围的结束端点
        start_date = trade_dates[0]    # 这是日期范围的起始端点（最近日期）

        for ts_code in common_stocks:
            try:
                # 获取这只股票的详细数据（从最早到最近）
                df = self.get_stock_money_flow(ts_code, latest_date, start_date)
                if df.empty:
                    continue

                # 计算10日累计净流入和大单净流入
                net_amount_10d = df['net_amount'].sum()
                buy_lg_amount_10d = df['buy_lg_amount'].sum()
                avg_net_amount = net_amount_10d / len(df) if len(df) > 0 else 0

                # 获取最新一条数据（升序排列后的最后一条，即最近日期）
                latest_row = df.iloc[-1:]

                latest_net_amount = latest_row.iloc[0]['net_amount']
                latest_pct_change = latest_row.iloc[0]['pct_change']
                name = latest_row.iloc[0]['name']

                # 计算真实连续天数：从最新日期往前追溯
                continuous_days = 0
                expected_dates = set(trade_dates)  # 交易日集合
                df_sorted = df.sort_values('trade_date', ascending=False)
                for _, row in df_sorted.iterrows():
                    trade_dt = str(row['trade_date'])
                    if trade_dt not in expected_dates:
                        continue
                    if row['net_amount'] <= 0:
                        break
                    continuous_days += 1

                # 检查日均净流入是否满足要求
                if min_net_amount > 0 and avg_net_amount < min_net_amount:
                    continue

                results.append({
                    'ts_code': ts_code,
                    'name': name,
                    'net_amount_10d': round(net_amount_10d, 2),
                    'buy_lg_amount_10d': round(buy_lg_amount_10d, 2),
                    'avg_net_amount': round(avg_net_amount, 2),
                    'latest_net_amount': round(latest_net_amount, 2),
                    'latest_pct_change': round(latest_pct_change, 2),
                    'continuous_days': continuous_days
                })

            except Exception as e:
                logger.debug(f"处理 {ts_code} 失败: {e}")
                continue

        # 按10日累计净流入排序
        results.sort(key=lambda x: x['net_amount_10d'], reverse=True)

        logger.info(f"最终结果: {len(results)} 只股票")
        return results


# 测试
if __name__ == '__main__':
    dao = MoneyFlowDAO()

    print("=" * 60)
    print("测试：筛选连续10日净流入股票")
    print("=" * 60)

    results = dao.select_continuous_inflow_stocks(end_date='20260420', days=10)

    print(f"\n找到 {len(results)} 只连续10日净流入股票:\n")
    print(f"{'序号':<4} {'代码':<12} {'名称':<10} {'连续天数':>6} {'10日净流入':>12} {'大单净流入':>12} {'日均净流入':>10}")
    print("-" * 80)

    for i, r in enumerate(results[:20], 1):
        print(f"{i:<4} {r['ts_code']:<12} {r['name']:<10} {r['continuous_days']:>6} {r['net_amount_10d']:>12,.0f} {r['buy_lg_amount_10d']:>12,.0f} {r['avg_net_amount']:>10,.0f}")

    if len(results) > 20:
        print(f"\n... 还有 {len(results) - 20} 只")
