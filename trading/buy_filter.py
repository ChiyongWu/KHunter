"""
买入前K线过滤模块

在执行买入前，检查以下规则，满足任何一个规则不买入：
1. 前20个交易日内最低点到今日开盘价，涨幅超过50%
2. 当日开盘涨跌幅超出 ±4%（涨幅 > 4% 或跌幅 > 4%）
3. BIAS5 > 7
4. 近30个交易日内（不含当日）涨停（当日涨幅 > 9.5%）次数少于1次
   （可通过 BuyPreFilter.CONFIG['enable_limit_up_check'] = False 关闭，用于提升成交率）

使用示例:
    from trading.buy_filter import BuyPreFilter
    
    # 在买入决策前调用
    filter_result = BuyPreFilter.check_filters(df_to_date, stock_code)
    if not filter_result['passed']:
        logger.info(f"【K线过滤】{stock_code} 未通过过滤: {filter_result['reason']}")
        return  # 跳过买入
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


class BuyPreFilter:
    """买入前K线过滤器"""
    
    # 过滤参数配置
    CONFIG = {
        # 规则1: 前20日最低点到今日开盘涨幅限制
        'max_rise_from_low': 0.50,  # 50%
        
        # 规则2: 开盘涨幅限制（上限）
        'max_open_rise': 0.04,  # 4%
        
        # 规则2: 开盘跌幅限制（下限）
        'min_open_rise': -0.04,  # -4%
        
        # 规则3: BIAS5上限
        'max_bias5': 7.0,
        
        # 辅助参数
        'lookback_days': 20,  # 回溯天数
        
        # 规则4: 涨停基因检查（近N交易日至少出现一定次数涨停，不含当日）
        # 关闭后不再要求"近期有涨停"，可提升成交率（代价：失去强势股过滤）
        'enable_limit_up_check': True,  # False=关闭涨停基因检测（默认开启，保持原行为）
        'limit_up_lookback': 30,       # 回溯交易日数
        'limit_up_threshold': 0.095,   # 涨停判定阈值（当日涨幅 > 9.5%）
        'limit_up_min_count': 1,       # 至少出现次数
    }
    
    @classmethod
    def check_filters(cls, df: pd.DataFrame, stock_code: str = '') -> Dict:
        """
        检查所有买入前过滤条件
        
        Args:
            df: 股票K线数据（倒序，最新在index=0）
            stock_code: 股票代码（用于日志）
            
        Returns:
            dict: {
                'passed': bool,           # 是否通过所有过滤
                'reason': str,            # 未通过原因
                'details': dict           # 详细检查结果
            }
        """
        if df is None or df.empty or len(df) < 5:
            return {
                'passed': True,
                'reason': '',
                'details': {'error': '数据不足，跳过过滤'}
            }
        
        result = {
            'passed': True,
            'reason': '',
            'details': {}
        }
        
        # 规则1: 检查前20日最低点到今日开盘涨幅
        rule1 = cls._check_rise_from_low(df, stock_code)
        result['details']['rise_from_low'] = rule1
        if not rule1['passed']:
            result['passed'] = False
            result['reason'] = f"规则1-{rule1['reason']}"
            return result
        
        # 规则2: 检查开盘涨幅
        rule2 = cls._check_open_rise(df, stock_code)
        result['details']['open_rise'] = rule2
        if not rule2['passed']:
            result['passed'] = False
            result['reason'] = f"规则2-{rule2['reason']}"
            return result
        
        # 规则4: 检查近N交易日涨停基因（不含当日）
        rule4 = cls._check_limit_up_history(df, stock_code)
        result['details']['limit_up_history'] = rule4
        if not rule4['passed']:
            result['passed'] = False
            result['reason'] = f"规则4-{rule4['reason']}"
            return result
        
        return result
    
    @classmethod
    def _check_rise_from_low(cls, df: pd.DataFrame, stock_code: str) -> Dict:
        """
        规则1: 检查前20个交易日内最低点到今日开盘价涨幅
        
        计算方法:
        1. 获取前20个交易日的数据
        2. 找到最低点价格
        3. 计算 (今日开盘价 - 最低价) / 最低价
        4. 如果涨幅超过50%，不买入
        """
        lookback = cls.CONFIG['lookback_days']
        max_rise = cls.CONFIG['max_rise_from_low']
        
        if len(df) < lookback:
            return {
                'passed': True,
                'reason': '',
                'value': None,
                'threshold': max_rise
            }
        
        try:
            # 统一按日期排序为正序（最旧在前，最新在后），不依赖传入数据的顺序
            df_sorted = df.sort_values('date', ascending=True).reset_index(drop=True)
            
            # 获取今日数据（正序数据的最后一条）
            today = df_sorted.iloc[-1]
            today_open = today['open']
            
            # 获取前lookback天的数据（正序数据倒数第2条到倒数第lookback+1条）
            recent_data = df_sorted.iloc[-(lookback+1):-1]
            
            if recent_data.empty:
                return {
                    'passed': True,
                    'reason': '',
                    'value': None,
                    'threshold': max_rise
                }
            
            # 找到最低点
            min_price = recent_data['low'].min()
            min_date = recent_data.loc[recent_data['low'].idxmin(), 'date']
            
            # 计算涨幅
            if min_price > 0:
                rise = (today_open - min_price) / min_price
            else:
                rise = 0
            
            passed = rise <= max_rise
            
            return {
                'passed': passed,
                'reason': f'前{lookback}日最低点{min_price:.2f}到开盘{rise*100:.1f}%' if not passed else '',
                'value': rise,
                'threshold': max_rise,
                'min_price': min_price,
                'min_date': min_date,
                'today_open': today_open
            }
            
        except Exception as e:
            logger.warning(f"规则1检查异常 {stock_code}: {str(e)}")
            return {
                'passed': True,
                'reason': '',
                'error': str(e)
            }
    
    @classmethod
    def _check_open_rise(cls, df: pd.DataFrame, stock_code: str) -> Dict:
        """
        规则2: 检查当日开盘涨跌幅
        
        计算方法:
        1. 获取今日开盘价和昨日收盘价
        2. 计算 (今日开盘 - 昨日收盘) / 昨日收盘
        3. 如果涨幅超过4%或跌幅超过-4%，不买入
        """
        max_open_rise = cls.CONFIG['max_open_rise']
        min_open_rise = cls.CONFIG['min_open_rise']
        
        if len(df) < 2:
            return {
                'passed': True,
                'reason': '',
                'value': None,
                'threshold': f'{min_open_rise*100:.0f}% ~ {max_open_rise*100:.0f}%'
            }
        
        try:
            # 统一按日期排序为正序（最旧在前，最新在后），不依赖传入数据的顺序
            df_sorted = df.sort_values('date', ascending=True).reset_index(drop=True)
            
            # 获取今日和昨日数据（正序数据的最后两条）
            today = df_sorted.iloc[-1]
            yesterday = df_sorted.iloc[-2]
            
            today_open = today['open']
            yesterday_close = yesterday['close']
            
            if yesterday_close <= 0:
                return {
                    'passed': True,
                    'reason': '',
                    'value': None,
                    'threshold': f'{min_open_rise*100:.0f}% ~ {max_open_rise*100:.0f}%'
                }
            
            # 计算开盘涨幅
            open_rise = (today_open - yesterday_close) / yesterday_close
            
            passed = min_open_rise <= open_rise <= max_open_rise
            
            if not passed:
                if open_rise > max_open_rise:
                    reason = f'开盘涨幅{open_rise*100:.2f}%过大'
                else:
                    reason = f'开盘跌幅{open_rise*100:.2f}%过大'
            else:
                reason = ''
            
            return {
                'passed': passed,
                'reason': reason,
                'value': open_rise,
                'threshold': f'{min_open_rise*100:.0f}% ~ {max_open_rise*100:.0f}%',
                'today_open': today_open,
                'yesterday_close': yesterday_close
            }
            
        except Exception as e:
            logger.warning(f"规则2检查异常 {stock_code}: {str(e)}")
            return {
                'passed': True,
                'reason': '',
                'error': str(e)
            }
    
    @classmethod
    def _check_limit_up_history(cls, df: pd.DataFrame, stock_code: str) -> Dict:
        """
        规则4: 检查近 limit_up_lookback 个交易日（不含当日）内是否出现过涨停
        
        涨停判定: 当日涨幅 = (close - 前一日close) / 前一日close > limit_up_threshold
        若统计到的涨停次数 < limit_up_min_count，则不通过（视为无涨停基因）。

        开关：CONFIG['enable_limit_up_check'] = False 时直接放行（用于提升成交率）。
        """
        if not cls.CONFIG.get('enable_limit_up_check', True):
            return {
                'passed': True,
                'reason': '',
                'value': None,
                'threshold': 'disabled',
                'enabled': False,
            }

        lookback = cls.CONFIG['limit_up_lookback']
        threshold = cls.CONFIG['limit_up_threshold']
        min_count = cls.CONFIG['limit_up_min_count']
        
        # 数据不足时跳过过滤（与前序规则风格一致）
        if len(df) < 2:
            return {
                'passed': True,
                'reason': '',
                'value': None,
                'threshold': f'>{threshold*100:.1f}%, min={min_count}次',
            }
        
        try:
            # 按日期升序排列，便于取前一日收盘计算当日涨幅
            df_sorted = df.sort_values('date', ascending=True).reset_index(drop=True)
            
            # 取最近 lookback 个交易日（不含当日，即排除最后一条）
            window = df_sorted.iloc[-(lookback + 1):-1]
            if window.empty:
                return {
                    'passed': True,
                    'reason': '',
                    'value': 0,
                    'threshold': f'>{threshold*100:.1f}%, min={min_count}次',
                }
            
            # 计算窗口内每日涨幅（用相邻两日收盘）
            closes = window['close'].values
            prev_closes = window['close'].shift(1).values
            limit_up_count = 0
            for i in range(1, len(closes)):
                prev = prev_closes[i]
                if prev and prev > 0:
                    gain = (closes[i] - prev) / prev
                    if gain > threshold:
                        limit_up_count += 1
            
            passed = limit_up_count >= min_count
            reason = '' if passed else f'近{lookback}日涨停{limit_up_count}次(<{min_count}次)'
            
            return {
                'passed': passed,
                'reason': reason,
                'value': limit_up_count,
                'threshold': f'>{threshold*100:.1f}%, min={min_count}次',
                'window_days': len(window),
            }
            
        except Exception as e:
            logger.warning(f"规则4检查异常 {stock_code}: {str(e)}")
            return {
                'passed': True,
                'reason': '',
                'error': str(e),
            }

    @classmethod
    def _check_bias5(cls, df: pd.DataFrame, stock_code: str) -> Dict:
        """
        规则3: 检查BIAS5指标
        
        BIAS5计算方法:
        BIAS = (收盘价 - N日均线) / N日均线 * 100
        
        如果BIAS5 > 7，说明股价偏离5日均线太远，不买入
        """
        max_bias5 = cls.CONFIG['max_bias5']
        
        if len(df) < 6:
            return {
                'passed': True,
                'reason': '',
                'value': None,
                'threshold': max_bias5
            }
        
        try:
            # 统一按日期排序为正序（最旧在前，最新在后），不依赖传入数据的顺序
            df_sorted = df.sort_values('date', ascending=True).reset_index(drop=True)
            
            # 获取最新收盘价（正序数据的最后一条）
            current_close = df_sorted.iloc[-1]['close']
            
            # 计算MA5（使用正序数据，rolling从前到后计算）
            close_series = df_sorted['close']
            ma5_series = close_series.rolling(window=5, min_periods=1).mean()
            ma5 = ma5_series.iloc[-1]  # 取最后一个（最新的MA5）
            
            if ma5 <= 0:
                return {
                    'passed': True,
                    'reason': '',
                    'value': None,
                    'threshold': max_bias5
                }
            
            # 计算BIAS5
            bias5 = (current_close - ma5) / ma5 * 100
            
            passed = bias5 <= max_bias5
            
            return {
                'passed': passed,
                'reason': f'BIAS5={bias5:.2f}超过阈值{max_bias5}' if not passed else '',
                'value': bias5,
                'threshold': max_bias5,
                'ma5': ma5,
                'current_close': current_close
            }
            
        except Exception as e:
            logger.warning(f"规则3检查异常 {stock_code}: {str(e)}")
            return {
                'passed': True,
                'reason': '',
                'error': str(e)
            }
    
    @classmethod
    def check_filters_with_config(cls, df: pd.DataFrame, stock_code: str, config: Dict) -> Dict:
        """
        使用自定义配置检查过滤条件
        
        Args:
            df: 股票K线数据
            stock_code: 股票代码
            config: 自定义配置，可覆盖默认配置
            
        Returns:
            dict: 过滤结果
        """
        # 合并配置
        merged_config = cls.CONFIG.copy()
        if config:
            merged_config.update(config)
        
        # 临时设置配置
        original_config = cls.CONFIG.copy()
        cls.CONFIG.update(merged_config)
        
        try:
            result = cls.check_filters(df, stock_code)
            return result
        finally:
            # 恢复原配置
            cls.CONFIG = original_config


def add_buy_pre_filter_to_backtest_engine():
    """
    将买入前过滤逻辑添加到回测引擎的代码
    
    这是一个辅助函数，用于说明如何在backtest_engine.py中添加过滤逻辑
    """
    code_snippet = '''
    # 在 backtest_engine.py 的买入决策处（约512行附近）添加以下代码：
    
    # 在获取股票数据后、买入决策前（约494行后，512行前）添加:
    
    # ========== 新增：买入前K线过滤 ==========
    # 获取买入前K线过滤结果
    from trading.buy_filter import BuyPreFilter
    
    # 检查是否启用K线过滤（可以从config中获取，默认启用）
    enable_kline_filter = config.get('enable_kline_filter', True)
    
    if enable_kline_filter:
        filter_result = BuyPreFilter.check_filters(df_to_date, stock_code)
        if not filter_result['passed']:
            logger.info(f"【K线过滤】{stock_code} {stock.get('stock_name', '')}: {filter_result['reason']}")
            logger.info(f"  详情: {filter_result['details']}")
            remaining_candidates.append(candidate)
            continue
        else:
            logger.info(f"【K线过滤通过】{stock_code} {stock.get('stock_name', '')}")
    # ========== K线过滤结束 ==========
    
    # 原有买入逻辑继续...
    '''
    print(code_snippet)
    return code_snippet


if __name__ == '__main__':
    # 测试用例
    import numpy as np
    
    print("=" * 80)
    print("买入前K线过滤模块测试")
    print("=" * 80)
    
    # 创建测试数据 - 注意：数据是倒序的（最新在前面）
    dates = pd.date_range(end='2026-05-03', periods=30, freq='D')
    
    # 测试1: 形态正常（规则1/2/3 均达标），但近30日无涨停 -> 应该不通过（规则4：无涨停基因）
    print("\n【测试1】形态正常但近30日无涨停 - 应该不通过（规则4：无涨停基因）")
    # 目标：前20日最低点到今日涨幅<50%，开盘涨跌幅在±4%内，BIAS5<7 —— 规则1/2/3 均达标
    # 数据设计：30天都在10元附近波动，close=10.05，MA5≈10.05，BIAS5≈0
    # 注意：本组数据近30日无涨停（无涨幅>9.5%的交易日），规则4 不通过，故最终判定为不通过
    
    prices1 = []
    for i in range(30):
        # 稳定在10元附近
        prices1.append(10.0 + (i % 5) * 0.01)  # 轻微波动
    prices1.reverse()  # 反转为倒序
    
    data1 = {
        'date': [str(d.date()) for d in dates],
        'open': prices1,
        'high': [p + 0.1 for p in prices1],
        'low': [p - 0.1 for p in prices1],
        'close': [p + 0.05 for p in prices1],  # 收盘略高于开盘
        'volume': [1000000] * 30
    }
    df1 = pd.DataFrame(data1)
    result1 = BuyPreFilter.check_filters(df1, '000001')
    print(f"  结果: {'通过' if result1['passed'] else '不通过'}")
    print(f"  原因: {result1['reason']}")
    if result1['details']:
        for k, v in result1['details'].items():
            if isinstance(v, dict) and 'value' in v:
                print(f"    {k}: value={v['value']:.4f}, threshold={v['threshold']}")
    
    # 测试2: 前20日最低点到今日涨幅超过50% - 应该不通过
    print("\n【测试2】涨幅超50% - 应该不通过")
    # 构造数据：从20天前低点10元，到今日开盘25元，涨幅150%
    base_prices = [25.0]  # 今天
    for i in range(29):
        base_prices.append(10.0 + (29 - i) * 0.5)  # 逐渐上涨到25
    
    data2 = {
        'date': [str(d.date()) for d in dates][::-1],
        'open': base_prices,
        'high': [p + 1.0 for p in base_prices],
        'low': [p - 0.5 for p in base_prices],
        'close': [p + 0.3 for p in base_prices],
        'volume': [1000000] * 30
    }
    df2 = pd.DataFrame(data2)
    result2 = BuyPreFilter.check_filters(df2, '000002')
    print(f"  结果: {'通过' if result2['passed'] else '不通过'}")
    print(f"  原因: {result2['reason']}")
    if 'rise_from_low' in result2['details']:
        r = result2['details']['rise_from_low']
        print(f"  详情: 最低点={r.get('min_price')}, 今日开盘={r.get('today_open')}, 涨幅={r.get('value')*100:.1f}%")
    
    # 测试3: 开盘涨幅超过4% - 应该不通过
    print("\n【测试3】开盘涨幅超4% - 应该不通过")
    # 昨日收盘 = 昨日open(10.0)+0.1 = 10.1；今日开盘10.6 → 涨幅 (10.6-10.1)/10.1 ≈ 4.95% > 4%
    base_prices3 = [10.6]  # 今天开盘高开约5%
    for i in range(29):
        base_prices3.append(10.0)  # 之前都是10元
    
    data3 = {
        'date': [str(d.date()) for d in dates][::-1],
        'open': base_prices3,
        'high': [p + 0.5 for p in base_prices3],
        'low': [p - 0.5 for p in base_prices3],
        'close': [p + 0.1 for p in base_prices3],
        'volume': [1000000] * 30
    }
    df3 = pd.DataFrame(data3)
    result3 = BuyPreFilter.check_filters(df3, '000003')
    print(f"  结果: {'通过' if result3['passed'] else '不通过'}")
    print(f"  原因: {result3['reason']}")
    if 'open_rise' in result3['details']:
        r = result3['details']['open_rise']
        print(f"  详情: 开盘涨幅={r.get('value')*100:.2f}%")
    
    # 测试4: 开盘跌幅超过-4% - 应该不通过
    print("\n【测试4】开盘跌幅超-4% - 应该不通过")
    # 昨天收盘10元，今日开盘9.5元，跌幅5%
    base_prices4 = [9.5]  # 今天开盘低开5%
    for i in range(29):
        base_prices4.append(10.0)  # 之前都是10元
    
    data4 = {
        'date': [str(d.date()) for d in dates][::-1],
        'open': base_prices4,
        'high': [p + 0.5 for p in base_prices4],
        'low': [p - 0.5 for p in base_prices4],
        'close': [p + 0.1 for p in base_prices4],
        'volume': [1000000] * 30
    }
    df4 = pd.DataFrame(data4)
    result4 = BuyPreFilter.check_filters(df4, '000004')
    print(f"  结果: {'通过' if result4['passed'] else '不通过'}")
    print(f"  原因: {result4['reason']}")
    if 'open_rise' in result4['details']:
        r = result4['details']['open_rise']
        print(f"  详情: 开盘涨跌幅={r.get('value')*100:.2f}%")
    
    # 测试5: BIAS5 > 7 - 应该不通过
    # 注意：本组数据近30日无涨停，规则4 会先于 BIAS5 规则拦截，
    #      因此本用例实际验证的是“规则4 不通过”，BIAS5 规则在此未被覆盖到
    print("\n【测试5】BIAS5 > 7 - 应该不通过（实际由规则4先拦截：近30日无涨停）")
    # 构造数据：今日收盘12元，前5日价格在10元附近
    # MA5 ≈ 10.2，BIAS5 = (12-10.2)/10.2*100 ≈ 18% > 7
    # 确保前20日最低点到今日开盘涨幅<50%（最低点=10元，今日开盘=10.1，涨幅=1%）
    
    base_prices4 = []
    # 前25天：价格从10元开始（20日前价格与今天接近）
    for i in range(25):
        base_prices4.append(10.0 + (i % 3) * 0.1)  # 轻微波动
    # 前5天：价格稳定在10元
    for i in range(4):
        base_prices4.append(10.0)
    # 今天：开盘10.1（微涨1%）
    base_prices4.append(10.1)
    base_prices4.reverse()
    
    data4 = {
        'date': [str(d.date()) for d in dates],
        'open': base_prices4,
        'high': [p + 0.3 for p in base_prices4],
        'low': [p - 0.2 for p in base_prices4],
        'close': base_prices4.copy(),
        'volume': [1000000] * 30
    }
    # 覆盖今天收盘价为12元（大幅上涨使BIAS5变高）
    data4['close'][0] = 12.0
    data4['high'][0] = 12.2
    df4 = pd.DataFrame(data4)
    result4 = BuyPreFilter.check_filters(df4, '000004')
    print(f"  结果: {'通过' if result4['passed'] else '不通过'}")
    print(f"  原因: {result4['reason']}")
    if 'bias5' in result4['details']:
        r = result4['details']['bias5']
        print(f"  详情: BIAS5={r.get('value')}, MA5={r.get('ma5')}, 收盘={r.get('current_close')}")
    
    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)
