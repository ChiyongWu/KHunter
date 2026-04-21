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

        # 计算开始日期（向前推days*2天，留有余量）
        start_date_dt = datetime.strptime(end_date, '%Y%m%d') - timedelta(days=days * 2)
        start_date = start_date_dt.strftime('%Y%m%d')

        # 使用Tushare获取真实交易日历
        try:
            df = self.pro.trade_cal(
                exchange='SSE',  # 上海交易所
                start_date=start_date,
                end_date=end_date,
                is_open='1'  # 只获取交易日
            )
            if df is None or df.empty:
                logger.warning(f"获取交易日历失败，使用简单排除周末方式")
                # 降级方案：排除周末
                dates = []
                current = datetime.strptime(end_date, '%Y%m%d')
                while len(dates) < days:
                    if current.weekday() < 5:
                        dates.append(current.strftime('%Y%m%d'))
                    current -= timedelta(days=1)
                return dates

            # trade_cal返回的数据可能是降序的，需要排序后再取
            trade_dates_list = sorted(df['cal_date'].tolist())[-days:]
            logger.info(f"获取到{len(trade_dates_list)}个交易日: {trade_dates_list}")
            return trade_dates_list

        except Exception as e:
            logger.error(f"获取交易日历异常: {e}，使用简单排除周末方式")
            # 降级方案：排除周末
            dates = []
            current = datetime.strptime(end_date, '%Y%m%d')
            while len(dates) < days:
                if current.weekday() < 5:
                    dates.append(current.strftime('%Y%m%d'))
                current -= timedelta(days=1)
            return dates

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

        从end_date往前持续追踪，直到遇到净流出日期，统计真实连续天数

        Args:
            end_date: 结束日期（YYYYMMDD），默认今日
            days: 连续天数要求（最低要求，会持续追踪更多天数）
            min_net_amount: 最小日均净流入(万元)

        Returns:
            股票信息列表
        """
        if self.pro is None:
            return []

        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        # 获取足够多的交易日用于追踪（days * 2 + 缓冲）
        track_days = days * 2 + 10
        trade_dates = self.get_trade_dates(end_date, track_days)
        if not trade_dates:
            logger.warning("无有效交易日")
            return []

        # get_trade_dates返回升序列表（从早到晚），需要反转成降序（从新到旧）
        trade_dates = list(reversed(trade_dates))
        latest_trade_date = trade_dates[0]
        earliest_trade_date = trade_dates[-1]
        logger.info(f"追踪日期范围: {latest_trade_date} ~ {earliest_trade_date} (共{len(trade_dates)}个交易日)")

        # 获取每日资金流向数据
        all_data = {}
        for trade_date in trade_dates:
            df = self.get_daily_money_flow(trade_date)
            if not df.empty:
                inflow_df = df[df['net_amount'] > 0]
                all_data[trade_date] = inflow_df.set_index('ts_code')['net_amount'].to_dict()
                logger.info(f"{trade_date}: 全市场{len(df)}只, 净流入{len(inflow_df)}只")
            else:
                all_data[trade_date] = {}

        # 找出最近日期有净流入的股票作为候选
        candidate_stocks = set(all_data[latest_trade_date].keys())
        logger.info(f"起始日{latest_trade_date}净流入股票: {len(candidate_stocks)}只")

        # 从候选股票中筛选真正连续净流入的
        results = []

        for ts_code in candidate_stocks:
            try:
                # 从最近日期往前追踪连续净流入（trade_dates已是降序）
                continuous_count = 0
                continuous_amount = 0.0

                for trade_date in trade_dates:
                    stock_net = all_data.get(trade_date, {}).get(ts_code)
                    if stock_net is None:
                        # 股票在该日无数据（停牌或未上市），停止追踪
                        break

                    if stock_net > 0:
                        continuous_count += 1
                        continuous_amount += stock_net
                    else:
                        # 遇到净流出或零流入，停止追踪
                        break

                # 检查是否满足最低连续天数要求
                if continuous_count < days:
                    logger.debug(f"{ts_code}: 连续{continuous_count}天 < {days}天要求")
                    continue

                # 计算平均日净流入
                avg_net_amount = continuous_amount / continuous_count if continuous_count > 0 else 0

                # 检查日均净流入是否满足要求
                if min_net_amount > 0 and avg_net_amount < min_net_amount:
                    logger.debug(f"{ts_code}: 日均{avg_net_amount:.0f}万 < {min_net_amount}万要求")
                    continue

                # 获取股票名称和今日涨跌幅
                name = ""
                latest_pct_change = 0
                if latest_trade_date in all_data and ts_code in all_data[latest_trade_date]:
                    # 通过日频数据获取（需要额外查询）
                    pass

                # 获取个股详细数据以获取名称和涨跌幅
                stock_df = self.get_stock_money_flow(ts_code, earliest_trade_date, latest_trade_date)
                if not stock_df.empty:
                    latest_row = stock_df.iloc[-1]  # 最后一条是最新日期
                    name = latest_row.get('name', '')
                    latest_pct_change = latest_row.get('pct_change', 0)
                else:
                    name = ""
                    latest_pct_change = 0

                # 计算追踪期间的累计净流入和大单净流入
                net_amount_total = continuous_amount
                buy_lg_amount_total = 0
                if not stock_df.empty:
                    # 取最新的continuous_count条数据（最后N条）
                    tracked_df = stock_df.tail(continuous_count)
                    net_amount_total = tracked_df['net_amount'].sum()
                    buy_lg_amount_total = tracked_df['buy_lg_amount'].sum()

                results.append({
                    'ts_code': ts_code,
                    'name': name,
                    'net_amount_10d': round(net_amount_total, 2),
                    'buy_lg_amount_10d': round(buy_lg_amount_total, 2),
                    'avg_net_amount': round(avg_net_amount, 2),
                    'latest_net_amount': round(all_data[latest_trade_date].get(ts_code, 0), 2),
                    'latest_pct_change': round(latest_pct_change, 2),
                    'continuous_days': continuous_count
                })

            except Exception as e:
                logger.debug(f"处理 {ts_code} 失败: {e}")
                continue

        # 按连续天数和净流入金额排序
        results.sort(key=lambda x: (x['continuous_days'], x['net_amount_10d']), reverse=True)

        logger.info(f"最终结果: {len(results)} 只股票（连续{days}天以上净流入）")
        for r in results[:5]:
            logger.info(f"  {r['ts_code']}: {r['name']}, 连续{r['continuous_days']}天, 净流入{r['net_amount_10d']:.0f}万")

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
