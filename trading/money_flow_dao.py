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
        筛选连续N日净流入股票（按需获取优化版）

        优化策略：
        1. 只获取必要天数的数据（days + 5）
        2. 逐步获取数据，发现候选为空立即停止
        3. 避免一次性获取全部数据再筛选

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

        # 优化：只获取必要天数（days + 5天余量）
        need_days = days + 5
        trade_dates = self.get_trade_dates(end_date, need_days)
        if len(trade_dates) < days:
            logger.warning(f"有效交易日不足: {len(trade_dates)} < {days}")
            return []

        # 升序排列（从早到晚）
        trade_dates = sorted(trade_dates)
        latest_trade_date = trade_dates[-1]
        earliest_trade_date = trade_dates[0]
        logger.info(f"追踪日期范围: {earliest_trade_date} ~ {latest_trade_date} (共{len(trade_dates)}个交易日)")

        # 按需获取数据：逐日获取，发现空集立即停止
        all_data = {}  # {trade_date: {ts_code: net_amount}}
        required_dates = []  # 需要获取详细数据的日期

        # 从最新日期开始逐日获取，直到候选为空
        candidate_stocks = None  # 当前候选股票集合
        continuous_days = 0

        for trade_date in reversed(trade_dates):
            # 获取当日全市场资金流向
            df = self.get_daily_money_flow(trade_date)
            if df.empty:
                all_data[trade_date] = {}
                continue

            # 获取净流入股票
            inflow_df = df[df['net_amount'] > 0]
            today_stocks = inflow_df.set_index('ts_code')['net_amount'].to_dict()
            all_data[trade_date] = today_stocks

            logger.info(f"{trade_date}: 全市场{len(df)}只, 净流入{len(inflow_df)}只")

            if candidate_stocks is None:
                # 第一天：全市场净流入股票作为初始候选
                candidate_stocks = set(today_stocks.keys())
                required_dates = [trade_date]
                continuous_days = 1
                logger.info(f"第1天 {trade_date}: {len(candidate_stocks)} 只候选")
            else:
                # 后续天：与候选取交集
                today_set = set(today_stocks.keys())
                new_candidates = candidate_stocks & today_set

                required_dates.append(trade_date)
                continuous_days += 1

                if len(new_candidates) == 0:
                    # 候选为空，停止获取
                    logger.info(f"第{continuous_days}天 {trade_date}: 候选为空，停止追溯")
                    break
                else:
                    candidate_stocks = new_candidates
                    logger.info(f"第{continuous_days}天 {trade_date}: {len(candidate_stocks)} 只候选")

                # 达到目标天数，停止获取
                if continuous_days >= days:
                    logger.info(f"已达到目标天数 {days}，停止获取")
                    break

        # 检查是否满足连续天数要求
        if continuous_days < days:
            logger.warning(f"实际连续天数 {continuous_days} < 要求 {days}，返回已有结果")
            days = continuous_days

        # 记录每天的候选股票（用于展示）
        daily_candidates = {}
        filtered_stocks = set(all_data[required_dates[0]].keys()) if required_dates else set()

        for i, trade_date in enumerate(required_dates, 1):
            today_stocks = set(all_data.get(trade_date, {}).keys())
            if i == 1:
                filtered_stocks = today_stocks
            else:
                filtered_stocks = filtered_stocks & today_stocks
            daily_candidates[i] = filtered_stocks.copy()

        final_candidates = daily_candidates.get(days, filtered_stocks)
        logger.info(f"最终候选股票: {len(final_candidates)} 只")

        # 获取这些股票的详细信息
        results = []
        for ts_code in final_candidates:
            try:
                # 计算累计净流入
                total_net_amount = sum(all_data.get(d, {}).get(ts_code, 0) for d in required_dates)
                total_buy_lg_amount = 0

                # 获取股票名称和涨跌幅
                stock_df = self.get_stock_money_flow(ts_code, earliest_trade_date, latest_trade_date)
                name = ""
                latest_pct_change = 0
                latest_net_amount = 0

                if not stock_df.empty:
                    latest_row = stock_df.iloc[-1]
                    name = latest_row.get('name', '')
                    latest_pct_change = latest_row.get('pct_change', 0)
                    latest_net_amount = latest_row.get('net_amount', 0)
                    total_buy_lg_amount = stock_df['buy_lg_amount'].sum()

                avg_net_amount = total_net_amount / days if days > 0 else 0

                # 检查日均净流入是否满足要求
                if min_net_amount > 0 and avg_net_amount < min_net_amount:
                    continue

                results.append({
                    'ts_code': ts_code,
                    'name': name,
                    'net_amount_10d': round(total_net_amount, 2),
                    'buy_lg_amount_10d': round(total_buy_lg_amount, 2),
                    'avg_net_amount': round(avg_net_amount, 2),
                    'latest_net_amount': round(latest_net_amount, 2),
                    'latest_pct_change': round(latest_pct_change, 2),
                    'continuous_days': days
                })

            except Exception as e:
                logger.debug(f"处理 {ts_code} 失败: {e}")
                continue

        # 按连续天数和净流入金额排序
        results.sort(key=lambda x: (x['continuous_days'], x['net_amount_10d']), reverse=True)

        logger.info(f"最终结果: {len(results)} 只股票（连续{days}天净流入）")
        for r in results[:10]:
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
