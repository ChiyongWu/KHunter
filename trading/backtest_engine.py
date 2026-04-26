"""
回测引擎核心模块
实现量化策略的自动化回测功能
"""

import sqlite3
import datetime
import logging
import threading
import numpy as np
import pandas as pd
from scipy import stats
import json
from pathlib import Path
from typing import List, Dict, Tuple

from utils.db_manager import DBManager
from utils.akshare_fetcher import AKShareFetcher
from strategy.strategy_registry import StrategyRegistry
from trading.stock_score_api import calculate_stock_score
from trading.backtest_scorer import BacktestScoreCalculator

from trading.timing_strategies import TimingStrategyFactory
from utils.strategy_name_mapper import get_english_name

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 回测全局锁，确保同一时刻只有一个回测任务执行，避免日志交错和资源竞争
_backtest_lock = threading.Lock()


class BacktestEngine:
    """回测引擎核心类"""
    
    def __init__(self, *args, **kwargs):
        """初始化回测引擎
        
        Args:
            *args: 可变参数
            **kwargs: 关键字参数
        """
        # 从参数中获取db_path，默认值为"data/stock_selection.db"
        db_path = kwargs.get('db_path', "data/stock_selection.db")
        if args:
            db_path = args[0]
        
        from utils.global_db import get_global_db
        self.db_manager = get_global_db()
        self.akshare_fetcher = AKShareFetcher("data")
        self.strategy_registry = StrategyRegistry()
        # 初始化K线数据获取器
        from utils.stock_data_fetcher import StockDataFetcher
        self.stock_data_fetcher = StockDataFetcher("data")
        from utils.kline_fetcher import KlineFetcher
        self.kline_fetcher = KlineFetcher(self.db_manager, self.stock_data_fetcher)
        
        # 初始化回测专用评分器
        self.score_calculator = BacktestScoreCalculator(db_manager=self.db_manager)

        # 股票数据缓存（性能优化）
        self.stock_data_cache = {}  # {code: df} 完整历史数据
        self.stock_name_cache = {}  # {code: name} 股票名称缓存
        self.stock_filtered_cache = {}  # {code: df} 已过滤ST/退市的股票
        
        # 可买股票池
        self.buy_candidate_pool = []  # 可买股票池，每个元素包含股票信息和加入日期
        
        # 交易日历缓存
        self.trading_calendar_cache = {}  # {date_str: is_open} 交易日历缓存
        self._sorted_trading_dates = []   # 排序后的交易日列表
        
        # 择时策略
        self.timing_strategy = None
        
        # 加载策略支撑位方法配置（从 config/support_methods.yaml）
        self._support_methods_config = self._load_support_methods_config()
        
    def run_backtest(self, strategy_name: str, config: Dict) -> Dict:
        """运行回测
        
        Args:
            strategy_name: 策略名称
            config: 回测配置参数
            
        Returns:
            回测结果字典
        """
        # 获取回测锁，确保同一时刻只有一个回测任务执行
        if not _backtest_lock.acquire(blocking=False):
            logger.warning(f"回测任务正在执行中，策略 {strategy_name} 等待...")
            _backtest_lock.acquire(blocking=True)
            logger.info(f"获取回测锁，开始执行策略: {strategy_name}")
        
        try:
            logger.info(f"开始回测策略: {strategy_name}")
            
            # 清空上次的缓存数据
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            self.stock_filtered_cache.clear()
            self.buy_candidate_pool.clear()
            
            # 初始化择时策略
            timing_strategy_name = config.get('timing_strategy', 'support')
            timing_params = config.get('timing_params', {})
            
            # 修复参数传递：如果timing_params中没有对应策略的配置，尝试直接从config中获取
            strategy_params = timing_params.get(timing_strategy_name, {})
            
            # 特殊处理：如果是海龟策略且config中直接包含海龟参数，合并到策略参数中
            if timing_strategy_name == 'turtle':
                turtle_specific_params = {
                    'n_entry': config.get('n_entry'),
                    'n_exit': config.get('n_exit'),
                    'atr_period': config.get('atr_period'),
                    'entry_atr': config.get('entry_atr'),
                    'add_atr': config.get('add_atr'),
                    'exit_atr': config.get('exit_atr'),
                    'preset': config.get('turtle_preset'),
                    'base_position_amount': config.get('base_position_amount')
                }
                # 只合并非None的参数
                turtle_specific_params = {k: v for k, v in turtle_specific_params.items() if v is not None}
                strategy_params.update(turtle_specific_params)
            
            self.timing_strategy = TimingStrategyFactory.create_strategy(
                timing_strategy_name, strategy_params
            )
            logger.info(f"初始化择时策略: {timing_strategy_name}")
            
            # 存储择时策略名称和参数，用于后续日志记录和结果输出
            self.timing_strategy_name = timing_strategy_name
            self.timing_strategy_params = strategy_params
            
            # 记录海龟策略主要参数
            if timing_strategy_name == 'turtle':
                logger.info(f"海龟策略参数: n_entry={strategy_params.get('n_entry')}, "
                           f"n_exit={strategy_params.get('n_exit')}, "
                           f"atr_period={strategy_params.get('atr_period')}, "
                           f"entry_atr={strategy_params.get('entry_atr')}, "
                           f"add_atr={strategy_params.get('add_atr')}, "
                           f"exit_atr={strategy_params.get('exit_atr')}, "
                           f"preset={strategy_params.get('preset')}")
            

            
            # 1. 获取回测日期范围
            start_date = config.get('start_date')
            end_date = config.get('end_date')
            
            if not start_date or not end_date:
                raise ValueError("回测开始日期和结束日期不能为空")
            
            # 2. 确保策略已注册
            if not self.strategy_registry.strategies:
                self.strategy_registry.auto_register_from_directory("strategy")
            
            # 3. 加载交易日历（在预加载数据之前，先确定交易日）
            self._load_trading_calendar(start_date, end_date)
            
            # 4. 获取回测交易日列表并打印
            date_range = self._get_trading_dates(start_date, end_date)
            if not date_range:
                raise ValueError(f"回测期间 {start_date} ~ {end_date} 没有交易日")
            
            # 5. 预加载所有股票数据到内存（根据策略参数动态计算历史数据天数）
            self._preload_stock_data(start_date, end_date, strategy_name)
            
            # 4. 初始化回测环境
            initial_capital = config.get('initial_capital', 1000000)
            current_capital = initial_capital
            positions = []  # 持仓列表
            trades = []     # 交易记录
            capital_history = [initial_capital]  # 资金历史
            dates = []      # 回测日期列表
            
            # 回测配置：同一只股票最大买入次数
            max_buy_count_per_stock = config.get('max_buy_count_per_stock', 4)
            # 股票累计买入次数计数器 {stock_code: buy_count}
            stock_buy_count = {}
            
            for i, current_date in enumerate(date_range):
                logger.info(f"\n============================================================")
                logger.info(f"处理日期: {current_date}")
                logger.info(f"============================================================")
                
                # 初始化当日买入计数
                daily_buys = 0
                max_daily_buys = config.get('max_daily_buys', 5)
                
                # 记录当日卖出的股票（用于限制当天卖出的股票不买入）
                today_sold_stocks = set()
                
                # 记录当日交易情况
                logger.info(f"当日初始资金: {current_capital:.2f}")
                logger.info(f"当日初始持仓: {len(positions)} 只股票")
                logger.info(f"当日最大买入限制: {max_daily_buys} 只")
                
                # 统一处理逻辑：先处理卖出，再处理买入
                
                # 合并重复持仓（同一股票可能有多条记录）
                if positions and len(positions) > 1:
                    merged = {}
                    for pos in positions:
                        code = pos['stock_code']
                        if code in merged:
                            # 合并：累加数量和金额，重新计算均价
                            merged[code]['quantity'] += pos['quantity']
                            merged[code]['buy_amount'] += pos['buy_amount']
                            merged[code]['buy_price'] = merged[code]['buy_amount'] / merged[code]['quantity']
                        else:
                            merged[code] = pos.copy()
                    new_positions = list(merged.values())
                    if len(new_positions) < len(positions):
                        logger.info(f"合并重复持仓: {len(positions)} -> {len(new_positions)}")
                    positions = new_positions
                
                # 处理卖出（如果有持仓）
                if positions:
                    logger.info(f"开始执行卖出操作，当前持仓数: {len(positions)}")
                    # 记录当前持仓详情
                    logger.info("当前持仓详情:")
                    for i, pos in enumerate(positions):
                        logger.info(f"  {i+1}. {pos['stock_code']} {pos['stock_name']}: 持仓数量={pos['quantity']}, 成本价={pos['buy_price']:.2f}, 持仓金额={pos['buy_amount']:.2f}")
                    
                    positions, sell_records = self._process_sell(positions, current_date, config)
                    logger.info(f"卖出操作完成，卖出 {len(sell_records)} 笔交易，剩余持仓数: {len(positions)}")
                    
                    # 更新资金（卖出资金立即可用）
                    for sell_record in sell_records:
                        current_capital += sell_record['sell_amount']
                        trades.append(sell_record)
                        # 记录当日卖出的股票
                        today_sold_stocks.add(sell_record['stock_code'])
                        logger.info(f"【卖出】股票: {sell_record['stock_code']} {sell_record['stock_name']}, 类型: {sell_record['sell_type']}, 价格: {sell_record['sell_price']:.2f}, 数量: {sell_record['quantity']}, 金额: {sell_record['sell_amount']:.2f}, 收益率: {sell_record['return_rate']:.2f}%")
                    
                    if today_sold_stocks:
                        logger.info(f"当日卖出股票: {list(today_sold_stocks)}")
                    else:
                        logger.info("当日无卖出股票")
                
                # 检查股票池移除条件（在选股之前执行，使用前一日收盘价）
                if self.buy_candidate_pool:
                    logger.info(f"开始检查股票池移除条件，当前股票池数量: {len(self.buy_candidate_pool)}")
                    removed = self._check_pool_removal(current_date, config)
                    if removed:
                        logger.info(f"股票池移除 {len(removed)} 只股票")
                
                # 执行选股获得前一日的选股结果
                selection_date = self._get_previous_trading_day(current_date)
                logger.info(f"执行选股日期: {selection_date}")
                
                # 执行选股、评分、筛选，得到候选股票池
                candidate_stocks = self._select_and_score_stocks(strategy_name, selection_date, config)
                
                # 将新选出的股票加入可买股票池
                new_added = 0
                for stock in candidate_stocks:
                    # 检查是否已经在池中
                    if not any(item['stock']['stock_code'] == stock['stock_code'] for item in self.buy_candidate_pool):
                        # 计算支撑位（加入时直接计算并保存）
                        support_level = self._calculate_support_level(stock, strategy_name, selection_date)
                        # 获取支撑位计算方法
                        support_method = self._get_support_method_for_strategy(strategy_name)
                        
                        self.buy_candidate_pool.append({
                            'stock': stock,
                            'added_date': selection_date,
                            'strategy_name': strategy_name,
                            'support_level': support_level,       # 支撑位价格
                            'support_method': support_method      # 支撑位计算方法
                        })
                        # 记录加入日志（包含支撑位信息）
                        if support_level > 0:
                            logger.info(f"股票 {stock['stock_code']} {stock['stock_name']} 加入可买股票池, "
                                       f"支撑位={support_level:.2f}, 方法={support_method}")
                        else:
                            logger.info(f"股票 {stock['stock_code']} {stock['stock_name']} 加入可买股票池, 支撑位计算失败")
                        new_added += 1
                
                # 处理可买股票池
                logger.info(f"\n当前可买股票池数量: {len(self.buy_candidate_pool)} (新增 {new_added} 只)")
                if self.buy_candidate_pool:
                    logger.info("可买股票池详情:")
                    for i, candidate in enumerate(self.buy_candidate_pool):
                        stock = candidate['stock']
                        added_date = candidate['added_date']
                        support_level = candidate.get('support_level', 0.0)
                        support_method = candidate.get('support_method', 'unknown')
                        # 显示支撑位信息
                        support_info = f"，支撑位={support_level:.2f}({support_method})" if support_level > 0 else "，支撑位=未计算"
                        logger.info(f"  {i+1}. {stock['stock_code']} {stock['stock_name']}: 加入日期={added_date}，评分={stock['score']}{support_info}")
                remaining_candidates = []
                
                # 记录当日已买入的股票代码
                today_bought_stocks = set()
                
                for candidate in self.buy_candidate_pool:
                    stock = candidate['stock']
                    stock_code = stock['stock_code']
                    added_date = candidate['added_date']
                    
                    # 当日已买入的股票：跳过检查，但保留在池中
                    if stock_code in today_bought_stocks:
                        logger.info(f"股票 {stock_code} 当日已买入，继续跟踪")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 检查同一股票最大买入次数
                    current_buy_count = stock_buy_count.get(stock_code, 0)
                    if current_buy_count >= max_buy_count_per_stock:
                        logger.info(f"股票 {stock_code} 已买入{current_buy_count}次，达到最大买入次数{max_buy_count_per_stock}，跳过")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 检查当日最大买入限制
                    if daily_buys >= max_daily_buys:
                        logger.info(f"达到当日最大买入限制: {max_daily_buys}")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 获取股票数据
                    df = self.stock_filtered_cache.get(stock_code)
                    if df is None:
                        logger.warning(f"无法获取股票 {stock_code} 的数据，跳过")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 日期切片：只取到当前日期为止的数据
                    date_str = current_date.strftime('%Y-%m-%d')
                    df_to_date = df[df['date'] <= date_str].copy()
                    if df_to_date.empty:
                        logger.warning(f"股票 {stock_code} 没有可用数据，跳过")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 如果是回测最后一天（今天）且没有当日数据，尝试获取实时数据
                    today = datetime.datetime.now().date()
                    last_data_date = df_to_date['date'].max()
                    if current_date == today and last_data_date < date_str:
                        logger.info(f"股票 {stock_code} 最后数据日期为 {last_data_date}，尝试获取实时数据...")
                        # 获取实时价格
                        try:
                            realtime_price = self.stock_data_fetcher.get_stock_price(stock_code)
                            if realtime_price and realtime_price > 0:
                                # 使用实时价格创建新的K线数据
                                # 获取前一天数据作为参考
                                prev_row = df_to_date[df_to_date['date'] == last_data_date].iloc[-1]
                                prev_close = float(prev_row['close'])
                                # 开盘价使用前一日收盘价（实时价格是当前价，不是开盘价）
                                open_price = prev_close
                                high_price = realtime_price if realtime_price > prev_close else prev_close
                                low_price = realtime_price if realtime_price < prev_close else prev_close
                                # 添加新行
                                new_row = pd.DataFrame([{
                                    'date': date_str,
                                    'open': open_price,
                                    'high': high_price,
                                    'low': low_price,
                                    'close': realtime_price,
                                    'volume': prev_row['volume']  # 用前一天的成交量
                                }])
                                df_to_date = pd.concat([df_to_date, new_row], ignore_index=True)
                                logger.info(f"股票 {stock_code} 添加实时数据: {date_str} 开盘={open_price}, 收盘={realtime_price}")
                        except Exception as e:
                            logger.warning(f"股票 {stock_code} 获取实时数据失败: {str(e)}")
                    
                    # 反转数据为倒序（最新的在前），供策略使用
                    df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
                    
                    # 先检查该股票是否已有持仓（用于策略判断加仓）
                    existing_pos = None
                    for pos in positions:
                        if pos['stock_code'] == stock_code:
                            existing_pos = pos
                            break
                    
                    # 调用策略获取完整信号（策略会根据是否有持仓判断新买入或加仓）
                    result = None
                    if self.timing_strategy:
                        result = self.timing_strategy.get_timing_result(df_to_date, existing_pos, current_capital)
                        timing_name = self.timing_strategy.__class__.__name__
                        logger.info(f"{timing_name}信号: is_buy={result.is_buy}, is_sell={result.is_sell}, "
                                   f"buy_qty={result.buy_quantity}, sell_qty={result.sell_quantity}, "
                                   f"type={result.trade_type}, msg={result.message}")
                    
                    # 判断是否买入
                    is_buy = result.is_buy if result else False
                    if not is_buy:
                        logger.info(f"【未买入】{stock_code} {stock['stock_name']}: 无买入信号")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 获取买入价格（以开盘价为准）
                    buy_price = self._get_stock_price(stock_code, current_date, 'open')
                    logger.info(f"股票 {current_date} {stock_code} {stock['stock_name']} 买入价格: {buy_price}")
                    
                    # 计算买入数量：优先使用策略返回的数量，否则根据配置的买入金额计算
                    if result and result.buy_quantity > 0:
                        quantity = result.buy_quantity
                    else:
                        # 策略未返回有效数量，用配置的买入金额计算
                        config_buy_amount = config.get('buy_amount', 100000)
                        quantity = int(config_buy_amount / buy_price) // 100 * 100
                    
                    if quantity <= 0:
                        logger.info(f"【未买入】{stock_code} {stock['stock_name']}: 计算买入数量为0")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 确保不超过可用资金
                    buy_amount = quantity * buy_price
                    if buy_amount > current_capital:
                        logger.info(f"【未买入】{stock_code} {stock['stock_name']}: 资金不足（需要{buy_amount:.2f}，可用{current_capital:.2f}）")
                        remaining_candidates.append(candidate)
                        continue
                    
                    # 执行买入
                    trade_type = result.trade_type if result else 'new'
                    
                    buy_record = self._execute_buy(stock_code, stock['stock_name'], added_date, current_date,
                                                  buy_price, buy_amount, quantity)
                    buy_record['trade_type'] = trade_type
                    
                    # 处理持仓：existing_pos 已在前面查找过
                    if existing_pos:
                        # 已有持仓，合并（加仓）
                        old_quantity = existing_pos['quantity']
                        old_amount = existing_pos['buy_amount']
                        existing_pos['quantity'] += quantity
                        existing_pos['buy_amount'] += buy_amount
                        # 加权平均买入价
                        existing_pos['buy_price'] = existing_pos['buy_amount'] / existing_pos['quantity']
                        # 更新加仓次数和加仓价格
                        existing_pos['add_count'] = result.add_count if result and hasattr(result, 'add_count') else existing_pos.get('add_count', 0) + 1
                        existing_pos['last_add_price'] = buy_price
                        logger.info(f"【加仓#{existing_pos['add_count']}】{current_date} {stock_code} {stock['stock_name']}: "
                                   f"原数量={old_quantity}, 加仓={quantity}, 合计={existing_pos['quantity']}, "
                                   f"均价={existing_pos['buy_price']:.2f}, 金额={buy_amount}")
                    else:
                        # 新买入：添加到持仓
                        # 保存首次建仓金额，用于后续加仓计算（海龟策略：每次加仓 = 首次建仓 × 50%）
                        base_position_amount = buy_amount
                        positions.append({
                            'stock_code': stock_code,
                            'stock_name': stock['stock_name'],
                            'buy_date': current_date,
                            'buy_price': buy_price,
                            'quantity': quantity,
                            'buy_amount': buy_amount,
                            'base_position_amount': base_position_amount  # 首次建仓金额（用于加仓计算）
                        })
                        logger.info(f"【新买入】{current_date} {stock_code} {stock['stock_name']}: 价格={buy_price}, 数量={quantity}, 金额={buy_amount}, 首次建仓={base_position_amount}")
                    
                    current_capital -= buy_amount
                    trades.append(buy_record)
                    daily_buys += 1
                    today_bought_stocks.add(stock_code)
                    # 更新该股票的累计买入次数
                    stock_buy_count[stock_code] = stock_buy_count.get(stock_code, 0) + 1
                    
                    # 买入成功仍保留在股票池中
                    remaining_candidates.append(candidate)
                
                # 更新可买股票池（保留所有股票，不因买入而移出）
                self.buy_candidate_pool = remaining_candidates
                logger.info(f"处理后可买股票池数量: {len(self.buy_candidate_pool)}")
                
                # 计算当日总资产（可用资金 + 持仓市值）
                total_assets = current_capital
                position_details = []
                for position in positions:
                    # 获取当日收盘价
                    current_price = self._get_stock_price(position['stock_code'], current_date, 'close')
                    position_value = current_price * position['quantity']
                    total_assets += position_value
                    position_details.append({
                        'code': position['stock_code'],
                        'name': position['stock_name'],
                        'price': current_price,
                        'quantity': position['quantity'],
                        'value': position_value
                    })
                
                # 记录每日资产详情
                logger.info(f"\n========== {current_date} 每日资产 ==========")
                logger.info(f"资金余额: {current_capital:.2f}")
                if position_details:
                    logger.info(f"持股清单 ({len(position_details)} 只):")
                    for p in position_details:
                        logger.info(f"  - {p['code']} {p['name']}: 价格={p['price']:.2f}, 数量={p['quantity']}, 市值={p['value']:.2f}")
                else:
                    logger.info(f"持股清单: 空仓")
                logger.info(f"持股市值: {total_assets - current_capital:.2f}")
                logger.info(f"总资产: {total_assets:.2f}")
                logger.info(f"==========================================")
                
                # 记录资金历史（包含持仓市值）
                capital_history.append(total_assets)
                dates.append(current_date)
            
            # 4. 结束结算：计算剩余持仓市值（不创建虚拟卖出记录）
            if positions:
                logger.info("计算剩余持仓市值")
                final_date = date_range[-1]
                for position in positions:
                    # 计算当前市值（最后一日收盘价）
                    current_price = self._get_stock_price(position['stock_code'], final_date, 'close')
                    current_value = current_price * position['quantity']
                    current_capital += current_value
                    logger.info(f"剩余持仓: {position['stock_code']} {position['stock_name']}, "
                               f"买入价={position['buy_price']:.2f}, 当前价={current_price:.2f}, "
                               f"市值={current_value:.2f}")
            
            # 5. 计算绩效指标
            final_capital = current_capital
            performance = self._calculate_performance(trades, initial_capital, final_capital, dates, capital_history)
            
            # 6. 构建回测结果
            backtest_result = {
                'strategy_name': strategy_name,
                'config': config,
                'start_date': start_date,
                'end_date': end_date,
                'initial_capital': initial_capital,
                'final_capital': final_capital,
                'performance': performance,
                'trades': trades,
                'capital_history': capital_history,
                'dates': dates,
                'timing_strategy': {
                    'name': self.timing_strategy_name,
                    'params': self.timing_strategy_params
                }
            }
            
            logger.info(f"回测完成，初始资金: {initial_capital}, 最终资金: {final_capital}, 总收益率: {performance['total_return']:.2f}%")
            
            return backtest_result
            
        except Exception as e:
            logger.error(f"回测失败: {str(e)}")
            raise
        finally:
            # 释放回测锁，允许下一个任务执行
            _backtest_lock.release()
    
    def _load_trading_calendar(self, start_date: str, end_date: str):
        """加载交易日历数据（扩大范围，覆盖前一交易日查找需求）
        
        Args:
            start_date: 回测开始日期 (YYYY-MM-DD)
            end_date: 回测结束日期 (YYYY-MM-DD)
        """
        try:
            import tushare as ts
            
            # 读取Tushare token
            tushare_token = None
            try:
                import json
                with open('config/tushare_config.json', 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    tushare_token = config.get('token') or config.get('api_key')
            except:
                pass
            
            if not tushare_token:
                logger.warning("未找到Tushare token，使用简单的交易日判断（仅过滤周末）")
                return
            
            # 扩大加载范围：往前多加载60天，覆盖_get_previous_trading_day的需求
            from datetime import timedelta
            extended_start = (datetime.datetime.strptime(start_date, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y%m%d')
            end_date_str = end_date.replace('-', '')
            logger.info(f"加载交易日历范围: {extended_start} 至 {end_date_str}")
            
            # 获取交易日历（只获取交易日）
            pro = ts.pro_api(tushare_token)
            df = pro.trade_cal(
                exchange='SSE',
                start_date=extended_start,
                end_date=end_date_str,
                is_open='1'
            )
            
            if df.empty:
                logger.warning("未获取到交易日历数据，使用简单的交易日判断（仅过滤周末）")
                return
            
            # 清空旧缓存
            self.trading_calendar_cache.clear()
            
            # 构建交易日缓存和排序列表
            trading_dates_sorted = []
            for _, row in df.iterrows():
                cal_date = row['cal_date']
                date_str = f"{cal_date[:4]}-{cal_date[4:6]}-{cal_date[6:8]}"
                self.trading_calendar_cache[date_str] = True
                trading_dates_sorted.append(date_str)
            
            # 按日期排序（tushare返回的可能是倒序）
            trading_dates_sorted.sort()
            # 保存排序后的交易日列表，供_get_previous_trading_day使用
            self._sorted_trading_dates = trading_dates_sorted
            
            # 打印前10个和后10个交易日，用于调试
            if trading_dates_sorted:
                logger.info(f"前10个交易日: {trading_dates_sorted[:10]}")
                logger.info(f"后10个交易日: {trading_dates_sorted[-10:]}")
            
            logger.info(f"成功加载交易日历数据，共 {len(self.trading_calendar_cache)} 个交易日")
            
        except Exception as e:
            logger.warning(f"加载交易日历数据失败: {str(e)}，使用简单的交易日判断（仅过滤周末）")
            self.trading_calendar_cache.clear()
            self._sorted_trading_dates = []
    
    def _is_trading_day(self, date: datetime.date) -> bool:
        """判断是否为交易日
        
        Args:
            date: 日期
            
        Returns:
            是否为交易日
        """
        date_str = date.strftime('%Y-%m-%d')
        
        # 如果交易日历缓存已加载，使用缓存判断
        if self.trading_calendar_cache:
            # 缓存中只存了交易日，不在缓存中说明不是交易日
            is_open = date_str in self.trading_calendar_cache
            logger.debug(f"使用交易日历判断日期 {date_str} 是否为交易日: {is_open}")
            return is_open
        
        # 如果没有交易日历数据，使用简单的判断（仅过滤周末）
        is_open = date.weekday() < 5
        logger.debug(f"使用简单判断日期 {date_str} 是否为交易日: {is_open}")
        return is_open
    
    def _get_trading_dates(self, start_date: str, end_date: str) -> List[datetime.date]:
        """获取回测期间的交易日列表
        
        直接从已加载的交易日历缓存中筛选，确保只处理真实交易日。
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            交易日期列表
        """
        # 转换为日期对象
        start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
        end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d').date()
        
        # 如果有排序好的交易日列表，直接筛选
        if hasattr(self, '_sorted_trading_dates') and self._sorted_trading_dates:
            dates = []
            for d_str in self._sorted_trading_dates:
                d = datetime.datetime.strptime(d_str, '%Y-%m-%d').date()
                if start_dt <= d <= end_dt:
                    dates.append(d)
        else:
            # fallback：逐日遍历，仅过滤周末
            dates = []
            current = start_dt
            while current <= end_dt:
                if self._is_trading_day(current):
                    dates.append(current)
                current += datetime.timedelta(days=1)
        
        # 打印回测交易日列表
        logger.info(f"回测交易日: {start_date} 至 {end_date}，共 {len(dates)} 个交易日")
        for d in dates:
            logger.info(f"  交易日: {d.strftime('%Y-%m-%d')}")
        
        return dates
    
    def _get_previous_trading_day(self, date: datetime.date) -> datetime.date:
        """获取前一个交易日
        
        优先从排序好的交易日列表中二分查找，效率更高且准确。
        
        Args:
            date: 当前日期
            
        Returns:
            前一个交易日
        """
        date_str = date.strftime('%Y-%m-%d')
        
        # 优先使用排序好的交易日列表
        if hasattr(self, '_sorted_trading_dates') and self._sorted_trading_dates:
            import bisect
            # 找到date_str在列表中的插入位置
            idx = bisect.bisect_left(self._sorted_trading_dates, date_str)
            # 前一个交易日是idx-1位置的日期
            if idx > 0:
                prev_date_str = self._sorted_trading_dates[idx - 1]
                return datetime.datetime.strptime(prev_date_str, '%Y-%m-%d').date()
        
        # fallback：逐日向前查找
        previous = date - datetime.timedelta(days=1)
        max_attempts = 10
        attempts = 0
        while attempts < max_attempts:
            if self._is_trading_day(previous):
                return previous
            previous -= datetime.timedelta(days=1)
            attempts += 1
        
        logger.warning(f"未找到前一个交易日，返回: {date - datetime.timedelta(days=1)}")
        return date - datetime.timedelta(days=1)
    
    def _get_stock_name(self, code: str) -> str:
        """获取股票名称（优先从缓存获取）
        
        Args:
            code: 股票代码
            
        Returns:
            股票名称，如果未找到则返回"未知"
        """
        # 优先从缓存获取
        if code in self.stock_name_cache:
            return self.stock_name_cache[code]
        
        try:
            cursor = self.db_manager.execute(
                "SELECT name FROM stock_basic WHERE code = ?",
                (code,)
            )
            row = cursor.fetchone()
            if row and row[0]:
                name = row[0]
                self.stock_name_cache[code] = name
                return name
        except Exception as e:
            logger.debug(f"获取股票名称失败 {code}: {str(e)}")
        return "未知"
    
    def _generate_stock_detail_url(self, code: str) -> str:
        """生成股票详情链接
        
        使用与选股结果页面一致的链接格式：
        - 调用 viewStockDetail(code) 函数
        - 该函数会加载 /api/stock/{code} 接口获取股票详情
        
        Args:
            code: 股票代码（6位数字，如 000001）
            
        Returns:
            JavaScript 函数调用字符串
        """
        try:
            # 返回与选股结果页面一致的链接格式
            # 使用 javascript: 协议和 viewStockDetail 函数
            # 格式：javascript:viewStockDetail('000001')
            return f"javascript:viewStockDetail('{code}')" 
            
        except Exception as e:
            logger.debug(f"生成股票详情链接失败 {code}: {str(e)}")
            return f"javascript:viewStockDetail('{code}')"
    
    def _load_support_methods_config(self):
        """加载策略支撑位方法配置
        
        从 config/support_methods.yaml 读取策略与支撑位计算方法的映射关系。
        
        Returns:
            dict: 策略名称 -> 支撑位配置的映射字典
        """
        try:
            # 导入yaml模块
            import yaml
            # 构建配置文件路径
            config_path = Path(__file__).parent.parent / "config" / "support_methods.yaml"
            
            if config_path.exists():
                # 读取yaml配置文件
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                # 提取策略配置部分
                strategies_config = config.get('strategies', {})
                logger.info(f"加载支撑位方法配置: {len(strategies_config)} 个策略")
                return strategies_config
            else:
                logger.warning(f"支撑位配置文件不存在: {config_path}")
                return {}
        except Exception as e:
            logger.warning(f"加载支撑位方法配置失败: {str(e)}")
            return {}
    
    def _get_support_method_for_strategy(self, strategy_name):
        """获取策略的支撑位计算方法
        
        根据策略名称从配置中查找对应的支撑位计算方法。
        
        Args:
            strategy_name: 策略名称（类名）
            
        Returns:
            str: 支撑位计算方法（ma20/key_close_5/key_open/key_close）
        """
        # 从配置中查找策略对应的支撑位方法
        strategy_config = self._support_methods_config.get(strategy_name, {})
        # 配置为字典格式，提取support_method字段
        if isinstance(strategy_config, dict):
            return strategy_config.get('support_method', 'ma20')
        # 配置为字符串格式，直接返回
        elif isinstance(strategy_config, str):
            return strategy_config
        # 未找到配置，返回默认方法
        return 'ma20'
    
    def _calculate_support_level(self, stock, strategy_name, selection_date):
        """计算候选股票的支撑位
        
        在加入股票池时调用，根据策略的支撑位计算方法和关键日计算支撑位。
        参考狩猎场功能（khunter_support_calculator.py）的4种计算方法：
        - ma20: 20日均线
        - key_close_5: 关键日收盘价 × 0.95
        - key_open: 关键日开盘价
        - key_close: 关键日收盘价
        
        Args:
            stock: 股票信息（包含 signal 字段，signal 中包含 key_date）
            strategy_name: 策略名称（类名）
            selection_date: 选股日期
            
        Returns:
            float: 支撑位价格，计算失败返回0.0
        """
        # stock_code: 股票代码，类型str，从stock中获取
        stock_code = stock['stock_code']
        
        # 获取策略对应的支撑位计算方法
        support_method = self._get_support_method_for_strategy(strategy_name)
        
        # 获取K线数据
        df = self.stock_filtered_cache.get(stock_code)
        if df is None:
            logger.debug(f"支撑位计算: {stock_code} 无K线数据")
            return 0.0
        
        # 日期切片：只取到选股日期为止的数据
        date_str = selection_date.strftime('%Y-%m-%d')
        df_to_date = df[df['date'] <= date_str].copy()
        if df_to_date.empty:
            logger.debug(f"支撑位计算: {stock_code} 选股日期 {date_str} 无数据")
            return 0.0
        
        # 确保正序（日期从早到晚）
        if len(df_to_date) > 1 and df_to_date['date'].iloc[0] > df_to_date['date'].iloc[1]:
            df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
        
        # 根据方法计算支撑位
        if support_method == 'ma20':
            # ma20: 20日均线
            if len(df_to_date) >= 20:
                ma20_value = round(df_to_date['close'].tail(20).mean(), 2)
                logger.debug(f"支撑位计算: {stock_code} ma20={ma20_value}")
                return ma20_value
            
        elif support_method in ['key_close_5', 'key_open', 'key_close']:
            # 需要关键日的方法：从信号中提取key_date
            signal = stock.get('signal', {})
            key_date = signal.get('key_date') if isinstance(signal, dict) else None
            
            if key_date:
                # 在K线数据中查找关键日
                key_date_str = str(key_date)[:10]
                key_date_data = df_to_date[df_to_date['date'].astype(str).str[:10] == key_date_str]
                
                if not key_date_data.empty:
                    if support_method == 'key_close_5':
                        # 关键日收盘价 × 0.95
                        support = round(float(key_date_data.iloc[0]['close']) * 0.95, 2)
                        logger.debug(f"支撑位计算: {stock_code} key_close_5={support} (关键日={key_date_str})")
                        return support
                    elif support_method == 'key_open':
                        # 关键日开盘价
                        support = round(float(key_date_data.iloc[0]['open']), 2)
                        logger.debug(f"支撑位计算: {stock_code} key_open={support} (关键日={key_date_str})")
                        return support
                    elif support_method == 'key_close':
                        # 关键日收盘价
                        support = round(float(key_date_data.iloc[0]['close']), 2)
                        logger.debug(f"支撑位计算: {stock_code} key_close={support} (关键日={key_date_str})")
                        return support
                else:
                    logger.debug(f"支撑位计算: {stock_code} 关键日 {key_date_str} 未在K线数据中找到")
            else:
                logger.debug(f"支撑位计算: {stock_code} 策略 {strategy_name} 需要关键日但信号中无key_date")
        
        # fallback: 使用20日均线作为默认支撑位
        if len(df_to_date) >= 20:
            fallback_value = round(df_to_date['close'].tail(20).mean(), 2)
            logger.debug(f"支撑位计算: {stock_code} fallback ma20={fallback_value}")
            return fallback_value
        
        # 无法计算支撑位
        logger.debug(f"支撑位计算: {stock_code} 数据不足，无法计算")
        return 0.0
    
    # 策略移除模式配置（仅针对选股策略）
    # 配置从 config/pool_removal_config.yaml 读取
    # 择时策略（TurtleStrategy、SupportStrategy）不用于选股，不参与股票池移除
    # 所有选股策略都有两个移除条件：破支撑位（始终生效）+ 趋势验证（延迟生效）
    # min_hold_days: 加入股票池多少天后开始趋势验证
    # - 0: 买入后立即验证趋势
    # - N: 加入股票池N天后才验证趋势

    # YAML配置文件缓存
    _pool_removal_config_cache = None

    def _load_pool_removal_config(self) -> Dict[str, Dict]:
        """从YAML配置文件加载股票池移除策略配置
        
        配置文件路径: config/pool_removal_config.yaml
        
        Returns:
            策略名称 -> 配置字典的映射
            
        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: 配置格式错误或无启用的策略
        """
        # 使用类级别缓存，避免重复读取文件
        if BacktestEngine._pool_removal_config_cache is not None:
            return BacktestEngine._pool_removal_config_cache
        
        config_map = {}
        config_path = Path(__file__).parent.parent / "config" / "pool_removal_config.yaml"
        
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f) or {}
        
        strategies = yaml_config.get('removal_strategies', {})
        for name, cfg in strategies.items():
            if cfg.get('is_enabled', True):
                config_map[name] = {
                    'min_hold_days': cfg.get('min_hold_days', 2)
                }
        
        if not config_map:
            raise ValueError("YAML配置无启用的策略")
        
        BacktestEngine._pool_removal_config_cache = config_map
        logger.info(f"从YAML配置加载股票池移除策略: {len(config_map)} 个策略")
        
        return config_map

    def _get_strategy_removal_config(self, strategy_name: str) -> Dict:
        """获取策略的移除配置
        
        从YAML配置文件读取，配置缺失时抛出异常。
        
        Args:
            strategy_name: 策略名称
            
        Returns:
            移除配置字典，包含 min_hold_days
            
        Raises:
            KeyError: 策略未在配置文件中配置
        """
        yaml_config = self._load_pool_removal_config()
        if strategy_name not in yaml_config:
            raise KeyError(f"策略 {strategy_name} 未配置股票池移除参数，请在 config/pool_removal_config.yaml 中添加")
        return yaml_config[strategy_name]

    def _check_pool_removal(self, current_date, config):
        """检查股票池中需要移除的股票
        
        移除条件（满足任一即移除）：
        1. 破支撑位：前一日收盘价 < 支撑位 × 0.98（始终生效）
        2. 不满足上升趋势条件（加入股票池 min_hold_days 天后生效）
        
        趋势验证条件：
        - 收盘价 >= MA10
        - 20日线性回归斜率 > 0
        - 20日R²拟合度 >= 0.3
        
        Args:
            current_date: 当前交易日期
            config: 回测配置
            
        Returns:
            list: 移除的候选列表
        """
        removed_candidates = []
        remaining_candidates = []
        
        # 获取前一个交易日（用于获取收盘价）
        prev_date = self._get_previous_trading_day(current_date)
        prev_date_str = prev_date.strftime('%Y-%m-%d')
        
        for candidate in self.buy_candidate_pool:
            # 提取股票信息
            stock_code = candidate['stock']['stock_code']
            stock_name = candidate['stock']['stock_name']
            strategy_name = candidate.get('strategy_name', '')
            
            # 获取策略的移除配置
            removal_config = self._get_strategy_removal_config(strategy_name)
            min_hold_days = removal_config.get('min_hold_days', 2)
            
            # 计算持有天数
            added_date = candidate.get('added_date')
            if isinstance(added_date, str):
                added_date = datetime.datetime.strptime(added_date, '%Y-%m-%d').date()
            elif not isinstance(added_date, datetime.date):
                added_date = datetime.date.today()
            
            hold_days = (prev_date - added_date).days
            
            # 获取股票数据
            df = self.stock_filtered_cache.get(stock_code)
            if df is None:
                # 无法获取数据，保留在池中
                remaining_candidates.append(candidate)
                continue
            
            # 日期切片：只取到前一日为止的数据
            df_to_date = df[df['date'] <= prev_date_str].copy()
            
            # 需要至少20日数据用于计算
            if len(df_to_date) < 20:
                remaining_candidates.append(candidate)
                continue
            
            # 确保正序（日期从早到晚）
            if df_to_date['date'].iloc[0] > df_to_date['date'].iloc[-1]:
                df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
            
            prev_close = df_to_date.iloc[-1]['close']
            
            # ========== 移除条件判断 ==========
            removal_reasons = []
            should_remove = False
            
            # 条件1: 破支撑位移除（始终生效）
            support_level = candidate.get('support_level', 0.0)
            if support_level > 0 and prev_close > 0:
                if prev_close < support_level * 0.98:
                    should_remove = True
                    drop_pct = (prev_close - support_level) / support_level * 100
                    removal_reasons.append(f"跌破支撑位{support_level:.2f}{drop_pct:.1f}%")
            
            # 条件2: 趋势验证移除（持有 min_hold_days 天后生效）
            if hold_days >= min_hold_days:
                ma10 = df_to_date['close'].tail(10).mean()
                prices = df_to_date['close'].tail(20).values
                x = np.arange(len(prices))
                slope, _, r_value, _, _ = stats.linregress(x, prices)
                r_squared = r_value ** 2
                
                # 判断是否满足上升趋势条件
                trend_ok = (prev_close >= ma10 and slope > 0 and r_squared >= 0.3)
                
                if not trend_ok:
                    should_remove = True
                    if prev_close < ma10:
                        removal_reasons.append(f"收盘价{prev_close:.2f}<MA10{ma10:.2f}")
                    if slope <= 0:
                        removal_reasons.append(f"斜率{slope:.4f}<=0")
                    if r_squared < 0.3:
                        removal_reasons.append(f"R²{r_squared:.4f}<0.3")
            
            # 决定是否移除
            if should_remove:
                removed_candidates.append(candidate)
                logger.info(f"【移除】{current_date} {stock_code} {stock_name}: "
                           f"收盘={prev_close:.2f}, 策略={strategy_name}, 持{hold_days}日, "
                           f"原因: {'; '.join(removal_reasons)}")
            else:
                remaining_candidates.append(candidate)
        
        # 更新股票池
        if removed_candidates:
            logger.info(f"股票池移除: {len(removed_candidates)} 只, "
                       f"剩余: {len(remaining_candidates)} 只")
            self.buy_candidate_pool = remaining_candidates
        
        return removed_candidates
    
    def _preload_stock_data(self, start_date: str, end_date: str, strategy_name: str = None) -> int:
        """预加载所有股票数据到内存（性能优化）
        
        Args:
            start_date: 回测开始日期
            end_date: 回测结束日期
            strategy_name: 策略名称，用于计算需要的历史数据天数
            
        Returns:
            预加载的股票数量
        """
        from datetime import datetime, timedelta
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        
        # 根据策略参数计算需要的历史数据天数
        buffer_days = 60  # 基础缓冲
        required_days = buffer_days
        
        if strategy_name:
            # 获取策略参数
            strategy = self.strategy_registry.get_strategy(strategy_name)
            if strategy and hasattr(strategy, 'params'):
                params = strategy.params
                max_value = 0
                
                # 常见回溯参数名 - 包含所有策略的历史数据需求参数
                lookback_keys = [
                    'lookback_days',                # 多金叉共振、多方炮、阻力位突破、启明星、底部趋势拐点
                    'pattern_days',                 # W底策略
                    'search_days',                  # 预留
                    'resonance_days',               # 预留
                    'limit_up_lookback_days',       # 涨停回马枪、涨停横盘
                    'lowest_point_lookback_days',   # 趋势加速拐点
                    'surge_lookback_days',          # 趋势加速拐点
                    'uptrend_lookback_days',        # 趋势加速拐点
                ]
                period_keys = ['ma_period', 'ma_short_period', 'ma_long_period', 'kdj_n', 'kdj_m1', 'kdj_m2',
                              'macd_short', 'macd_long', 'macd_signal', 'volume_ma_period', 'short_ma_period', 
                              'long_ma_period', 'period', 'min_pattern_days', 'max_break_days']
                
                # 获取回溯天数
                for key in lookback_keys:
                    if key in params:
                        val = params[key]
                        if isinstance(val, (int, float)):
                            max_value = max(max_value, int(val))
                
                # 获取周期参数
                for key in period_keys:
                    if key in params:
                        val = params[key]
                        if isinstance(val, (int, float)):
                            max_value = max(max_value, int(val))
                
                required_days = max_value + buffer_days
                logger.info(f"策略 {strategy_name} 需要 {max_value} 天历史数据 + {buffer_days} 天缓冲")
        
        # 扩展开始日期
        extended_start = (start_dt - timedelta(days=required_days)).strftime('%Y-%m-%d')
        
        logger.info(f"预加载股票数据: {extended_start} ~ {end_date} (原始: {start_date} ~ {end_date}, 加载历史: {required_days}天)")
        
        # 获取所有股票代码
        stock_codes = self.db_manager.list_all_stocks()
        total = len(stock_codes)
        loaded = 0
        skipped = 0
        
        for i, code in enumerate(stock_codes):
            try:
                # 读取股票数据
                df = self.db_manager.read_stock(code)
                
                if df is None or (hasattr(df, 'empty') and df.empty) or len(df) < 60:
                    skipped += 1
                    continue
                
                # 缓存原始数据
                df_copy = df.copy()
                # 统一日期格式为字符串，避免后续比较时类型不一致
                df_copy['date'] = df_copy['date'].dt.strftime('%Y-%m-%d')
                self.stock_data_cache[code] = df_copy
                
                # 获取并缓存股票名称
                name = self._get_stock_name(code)
                
                # 过滤ST股票和退市股票
                invalid = name.startswith('ST') or name.startswith('*ST')
                if not invalid:
                    for kw in ['退', '未知', '退市', '已退']:
                        if kw in name:
                            invalid = True
                            break
                
                if invalid:
                    skipped += 1
                    continue
                
                # 缓存有效股票
                df_filtered = df.copy()
                # 统一日期格式为字符串，避免后续比较时类型不一致
                df_filtered['date'] = df_filtered['date'].dt.strftime('%Y-%m-%d')
                self.stock_filtered_cache[code] = df_filtered
                loaded += 1
                
            except Exception as e:
                logger.debug(f"预加载股票 {code} 失败: {str(e)}")
                skipped += 1
            
            # 每500只显示一次进度
            if (i + 1) % 500 == 0:
                logger.info(f"预加载进度: {i + 1}/{total}, 有效股票: {loaded}, 跳过: {skipped}")
        
        logger.info(f"预加载完成: 有效股票 {loaded}, 跳过 {skipped}, 总计 {total}")
        return loaded
    
    def _execute_selection(self, strategy_name: str, date: datetime.date) -> List[Dict]:
        """执行选股（从缓存读取，使用日期切片）
        
        Args:
            strategy_name: 策略名称
            date: 选股日期
            
        Returns:
            选股结果列表
        """
        try:
            # 确保策略注册表已加载策略
            if not self.strategy_registry.strategies:
                self.strategy_registry.auto_register_from_directory("strategy")
            
            # 获取策略 - 策略注册时使用类名（如ContinuousRisingWithVolumeStrategyV2）
            # 需要先尝试映射为中文名称再转类名
            mapped_name = get_english_name(strategy_name)
            strategy = self.strategy_registry.get_strategy(mapped_name)
            
            if not strategy:
                # 尝试直接用原始名称查找
                strategy = self.strategy_registry.get_strategy(strategy_name)
            
            if not strategy:
                raise ValueError(f"策略 {strategy_name} 不存在")
            
            # 标准化返回格式
            standardized_stocks = []
            
            # 从缓存遍历有效股票
            for code, df in self.stock_filtered_cache.items():
                try:
                    # 日期切片：只取到目标日期为止的数据
                    date_str = date.strftime('%Y-%m-%d')
                    df_to_date = df[df['date'] <= date_str].copy()
                    
                    # 检查数据是否为空（预加载时已检查过至少60条，这里只检查是否有数据）
                    if df_to_date.empty:
                        continue
                    
                    # 反转数据为倒序（最新的在前）
                    # 策略实现假设数据是倒序排列，但数据库返回的是升序排列
                    df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
                    
                    # 获取股票名称
                    name = self.stock_name_cache.get(code, "未知")
                    
                    # 使用标准的 execute_selection 流程，确保指标被正确计算
                    # execute_selection 包含：数据验证 -> 快速过滤 -> 计算指标 -> 选股条件检查
                    # 这样每天的选股结果将根据不同的日期数据而变化
                    signal_list = strategy.execute_selection(df_to_date, code, name)
                    
                    # 处理选股结果
                    if signal_list:
                        for signal in signal_list:
                            # 生成股票详情链接
                            # 支持多个数据源的链接格式
                            stock_detail_url = self._generate_stock_detail_url(code)
                            
                            stock_info = {
                                'stock_code': code,
                                'stock_name': name,
                                'signal': signal,
                                'detail_url': stock_detail_url,  # 添加详情链接
                                'detail_link': f"[{code}]({stock_detail_url})"  # Markdown 格式链接
                            }
                            standardized_stocks.append(stock_info)
                            
                except Exception as e:
                    # 单只股票选股失败不影响整体流程
                    logger.debug(f"股票 {code} 选股失败: {str(e)}")
                    continue
            
            logger.info(f"{strategy_name} 策略在 {date} 选出 {len(standardized_stocks)} 只股票")
            
            return standardized_stocks
            
        except Exception as e:
            logger.error(f"执行选股失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return []
    
    def _score_stocks(self, stocks: List[Dict], strategy_name: str, date: datetime.date) -> List[Dict]:
        """对股票进行评分（回测模式）

        使用 BacktestScoreCalculator 进行高效评分：
        - 技术面得分 = Σ(策略权重 × 命中标志)
        - 综合得分 = 技术面×0.35 + 资金面×0.35 + 基本面×0.10 + 板块×0.10 + 事件×0.10
        - 一票否决：M头策略 + 多死叉共振同时命中 → -100分
        - 技术面否决后立即跳过其他维度计算

        Args:
            stocks: 股票列表（来自选股结果）
            strategy_name: 策略名称（类名）
            date: 评分日期

        Returns:
            带评分的股票列表
        """
        if not stocks:
            return []

        date_str = date.strftime('%Y-%m-%d')

        # 获取策略的中文名称（用于评分）
        strategy = self.strategy_registry.get_strategy(strategy_name)
        strategy_display_name = strategy.name if strategy else strategy_name
        logger.info(f"评分使用的策略名称: {strategy_display_name} (类名: {strategy_name})")

        # 使用回测专用评分器进行批量评分
        scored_stocks = self.score_calculator.calculate_batch_scores(
            stocks=stocks,
            score_date=date_str,
            strategy_name=strategy_display_name
        )

        return scored_stocks

    def _select_and_score_stocks(self, strategy_name: str, date: datetime.date, config: Dict) -> List[Dict]:
        """执行选股、评分、筛选，得到候选股票池
        
        封装选股流程，返回通过评分的候选股票列表。
        
        Args:
            strategy_name: 策略名称
            date: 选股日期
            config: 回测配置
            
        Returns:
            候选股票列表（已评分且通过筛选）
        """
        logger.info(f"开始执行选股，策略: {strategy_name}，择时策略: {self.timing_strategy_name}，日期: {date}")
        
        # 执行选股
        selected_stocks = self._execute_selection(strategy_name, date)
        logger.info(f"选股完成，共选出 {len(selected_stocks)} 只股票")
        
        if not selected_stocks:
            return []
        
        # 评分
        logger.info(f"开始对 {len(selected_stocks)} 只股票进行评分")
        scored_stocks = self._score_stocks(selected_stocks, strategy_name, date)
        
        # 记录每只股票的综合评分
        logger.info("\n股票评分详情:")
        for stock in scored_stocks:
            logger.info(f"  - {stock['stock_code']} {stock['stock_name']}: 综合评分={stock['score']}，否决标志={stock.get('veto_flag', False)}")
        
        # 筛选：去除否决票且评分达标
        score_threshold = config.get('score_threshold', 60)
        candidate_stocks = [
            stock for stock in scored_stocks 
            if not stock.get('veto_flag', False) and stock['score'] >= score_threshold
        ]
        
        logger.info(f"\n筛选后待买入股票数: {len(candidate_stocks)}")
        if candidate_stocks:
            logger.info("待买入股票列表:")
            for stock in candidate_stocks:
                logger.info(f"  - {stock['stock_code']} {stock['stock_name']}: 评分={stock['score']}")
        
        return candidate_stocks
    
    def _get_stock_price(self, stock_code: str, date: datetime.date, price_type: str) -> float:
        """获取股票价格
        
        Args:
            stock_code: 股票代码
            date: 日期
            price_type: 价格类型 (open, close, high, low, prev_close)
            
        Returns:
            价格
        """
        try:
            # 特殊处理：获取前一天收盘价
            if price_type == 'prev_close':
                # 获取前一天的日期
                prev_date = self._get_previous_trading_day(date)
                if prev_date:
                    return self._get_stock_price(stock_code, prev_date, 'close')
                return None
            
            # 1. 优先从本地数据库获取
            date_str = date.strftime('%Y-%m-%d')
            sql = f"""
                SELECT {price_type} FROM stock_kline 
                WHERE code = ? AND date = ?
            """
            result = self.db_manager.query_one(sql, (stock_code, date_str))
            
            if result and result.get(price_type) is not None:
                return float(result[price_type])
            
            # 2. 备选：从tushare获取
            try:
                import tushare as ts
                
                # 读取Tushare token
                tushare_token = None
                try:
                    import json
                    with open('config/tushare_config.json', 'r', encoding='utf-8') as f:
                        config = json.load(f)
                        tushare_token = config.get('token') or config.get('api_key')
                except:
                    pass
                
                if tushare_token:
                    pro = ts.pro_api(tushare_token)
                    df = pro.daily(
                        ts_code=f"{stock_code}.SH" if stock_code.startswith('6') else f"{stock_code}.SZ",
                        start_date=date_str,
                        end_date=date_str
                    )
                    if not df.empty:
                        if price_type == 'open':
                            return float(df.iloc[0]['open'])
                        elif price_type == 'close':
                            return float(df.iloc[0]['close'])
                        elif price_type == 'high':
                            return float(df.iloc[0]['high'])
                        elif price_type == 'low':
                            return float(df.iloc[0]['low'])
            except Exception as e:
                logger.debug(f"Tushare获取价格失败: {str(e)}")
            
            # 3. 备选：使用StockDataFetcher获取实时价格（仅用于收盘价，获取开盘价时不应使用实时价）
            # 注意：实时价格是当前价，不等于开盘价，开盘价只能从数据库获取
            if date == datetime.datetime.now().date() and price_type != 'open':
                price = self.stock_data_fetcher.get_stock_price(stock_code)
                if price:
                    return price
            
            # 4. 备选：从缓存中获取价格（用于回测）
            if stock_code in self.stock_data_cache:
                df = self.stock_data_cache[stock_code]
                date_str = date.strftime('%Y-%m-%d')
                df_date = df[df['date'] == date_str]
                if not df_date.empty:
                    if price_type in df_date.columns:
                        price = df_date.iloc[0][price_type]
                        if price is not None and not pd.isna(price):
                            logger.debug(f"从缓存获取股票 {stock_code} 日期 {date_str} {price_type} 价格: {price}")
                            return float(price)
                else:
                    logger.debug(f"缓存中没有股票 {stock_code} 日期 {date_str} 的数据")
            else:
                logger.debug(f"缓存中没有股票 {stock_code} 的数据")
            
            # 如果所有方法都失败，取前一交易日收盘价
            if stock_code in self.stock_data_cache:
                df = self.stock_data_cache[stock_code]
                # 查找目标日期之前最近的有效收盘价
                df_before = df[df['date'] < date_str].head(1)
                if not df_before.empty and 'close' in df_before.columns:
                    prev_close = df_before.iloc[0]['close']
                    if prev_close is not None and not pd.isna(prev_close):
                        logger.warning(f"股票 {stock_code} 日期 {date_str} 无{price_type}数据，使用前一交易日收盘价: {prev_close}")
                        return float(prev_close)
            
            logger.error(f"无法获取股票 {stock_code} 日期 {date_str} 的任何价格数据")
            return 0.0
            
        except Exception as e:
            logger.warning(f"获取股票 {stock_code} 价格失败: {str(e)}")
            return 10.0
    
    def _execute_buy(self, stock_code: str, stock_name: str, selection_date: datetime.date, 
                     buy_date: datetime.date, buy_price: float, buy_amount: float, 
                     quantity: int) -> Dict:
        """执行买入操作
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            selection_date: 选入日期
            buy_date: 买入日期
            buy_price: 买入价格
            buy_amount: 买入金额
            quantity: 买入数量
            
        Returns:
            买入记录
        """
        # 生成股票详情链接
        stock_detail_url = self._generate_stock_detail_url(stock_code)
        
        return {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'selection_date': selection_date,
            'buy_date': buy_date,
            'buy_price': buy_price,
            'buy_amount': buy_amount,
            'quantity': quantity,
            'sell_date': None,
            'sell_price': None,
            'sell_amount': None,
            'sell_type': None,
            'return_rate': None,
            'profit_loss': None,
            'hold_days': None,
            'detail_url': stock_detail_url
        }
    
    def _process_sell(self, positions: List[Dict], current_date: datetime.date, config: Dict) -> Tuple[List[Dict], List[Dict]]:
        """处理卖出操作
        
        职责划分：
        - 回测引擎负责：T+1检查、止盈止损执行
        - 择时策略负责：买卖信号判断
        
        Args:
            positions: 持仓列表
            current_date: 当前日期
            config: 回测配置
            
        Returns:
            (剩余持仓列表, 卖出记录列表)
        """
        remaining_positions = []
        sell_records = []
        
        # 获取卖出条件参数
        take_profit = config.get('take_profit', 21)  # 止盈21%
        stop_loss = config.get('stop_loss', -7)  # 止损7%
        hold_period = config.get('hold_period', 10)
        
        for position in positions:
            stock_code = position['stock_code']
            stock_name = position['stock_name']
            
            # 计算持有天数（基于交易日）
            buy_date_str = position['buy_date'].strftime('%Y-%m-%d')
            current_date_str = current_date.strftime('%Y-%m-%d')
            trading_days = self._get_trading_dates(buy_date_str, current_date_str)
            hold_days = len(trading_days) - 1
            
            # 获取当日开盘价和收益率
            open_price = self._get_stock_price(stock_code, current_date, 'open')
            return_rate = (open_price - position['buy_price']) / position['buy_price'] * 100
            
            # 根据是否有择时策略决定卖出规则描述
            if self.timing_strategy:
                sell_rule = f"止盈={take_profit}%, 止损={stop_loss}%, 由策略决定卖出"
            else:
                sell_rule = f"止盈={take_profit}%, 止损={stop_loss}%, 持有期={hold_period}天"
            logger.info(f"检查持仓 - {stock_code} {stock_name}: 买入日期={buy_date_str}, "
                       f"持有天数={hold_days}, 收益率={return_rate:.2f}%, {sell_rule}")
            
            # 初始化卖出决策
            sell_type = None
            reduce_quantity = 0
            sell_quantity = position['quantity']
            
            # T+1规则：当天买入的股票不能当天卖出
            if hold_days > 0:
                # 1. 先检查止盈止损（优先级最高）
                if return_rate >= take_profit:
                    sell_type = 'take_profit'
                    logger.info(f"  触发止盈: 收益率 {return_rate:.2f}% >= {take_profit}%")
                elif return_rate <= stop_loss:
                    sell_type = 'stop_loss'
                    logger.info(f"  触发止损: 收益率 {return_rate:.2f}% <= {stop_loss}%")
                
                # 2. 如果未触发止盈止损，调用择时策略获取信号
                if not sell_type and self.timing_strategy:
                    df = self.stock_filtered_cache.get(stock_code)
                    if df is not None:
                        date_str = current_date.strftime('%Y-%m-%d')
                        df_to_date = df[df['date'] <= date_str].copy()
                        if not df_to_date.empty:
                            df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
                            result = self.timing_strategy.get_timing_result(df_to_date, position, 0)
                            
                            if result.is_sell:
                                if result.trade_type == 'reduce':
                                    # 策略要求减仓
                                    reduce_quantity = result.sell_quantity if result.sell_quantity > 0 else position['quantity'] // 2
                                    if reduce_quantity >= position['quantity']:
                                        # 减仓数量>=持仓，执行清仓
                                        sell_type = 'strategy_sell'
                                        logger.info(f"  策略信号: 清仓 - {result.message}")
                                    else:
                                        sell_type = 'strategy_reduce'
                                        logger.info(f"  策略信号: 减仓{reduce_quantity}股 - {result.message}")
                                else:
                                    # 策略要求清仓
                                    sell_type = 'strategy_sell'
                                    sell_quantity = position['quantity']
                                    logger.info(f"  策略信号: 清仓 - {result.message}")
                
                # 3. 持有到期检查（仅无择时策略时生效）
                if not sell_type and not reduce_quantity:
                    if hold_days >= hold_period:
                        if not self.timing_strategy:
                            sell_type = 'hold_expired'
                            logger.info(f"  持有到期: {hold_days}天 >= {hold_period}天")
                
                if not sell_type and not reduce_quantity:
                    logger.info(f"  无卖出信号，继续持有")
            else:
                logger.info(f"  T+1限制，今日不能卖出")
            
            # 执行卖出操作
            if sell_type:
                # 执行清仓
                sell_amount = open_price * sell_quantity
                profit_loss = sell_amount - position['buy_amount'] * (sell_quantity / position['quantity'])
                
                sell_record = self._create_sell_record(position, current_date, open_price, 
                                                       sell_quantity, sell_amount, return_rate, hold_days, sell_type)
                sell_records.append(sell_record)
                logger.info(f"  【卖出】{stock_code}: 类型={sell_type}, 价格={open_price}, "
                           f"数量={sell_quantity}, 金额={sell_amount:.2f}, 收益率={return_rate:.2f}%")
                
            elif reduce_quantity > 0:
                # 执行减仓
                reduce_amount = open_price * reduce_quantity
                remaining_quantity = position['quantity'] - reduce_quantity
                remaining_ratio = remaining_quantity / position['quantity']
                
                reduce_record = self._create_sell_record(position, current_date, open_price,
                                                          reduce_quantity, reduce_amount, return_rate, hold_days, 'strategy_reduce')
                sell_records.append(reduce_record)
                
                # 更新持仓（保留剩余部分）
                position['quantity'] = remaining_quantity
                position['buy_amount'] = position['buy_amount'] * remaining_ratio
                position['buy_price'] = position['buy_amount'] / remaining_quantity if remaining_quantity > 0 else 0
                
                remaining_positions.append(position)
                logger.info(f"  【减仓】{stock_code}: 减仓数量={reduce_quantity}, 剩余数量={remaining_quantity}")
            else:
                # 继续持有
                remaining_positions.append(position)
        
        return remaining_positions, sell_records
    
    def _create_sell_record(self, position: Dict, sell_date: datetime.date, sell_price: float,
                            quantity: int, sell_amount: float, return_rate: float, hold_days: int, 
                            sell_type: str) -> Dict:
        """创建卖出记录
        
        Args:
            position: 持仓信息
            sell_date: 卖出日期
            sell_price: 卖出价格
            quantity: 卖出数量
            sell_amount: 卖出金额
            return_rate: 收益率
            hold_days: 持有天数
            sell_type: 卖出类型
            
        Returns:
            卖出记录字典
        """
        return {
            'stock_code': position['stock_code'],
            'stock_name': position['stock_name'],
            'selection_date': None,
            'buy_date': position['buy_date'],
            'buy_price': position['buy_price'],
            'buy_amount': position['buy_amount'] * (quantity / position['quantity']),
            'quantity': quantity,
            'sell_date': sell_date,
            'sell_price': sell_price,
            'sell_amount': sell_amount,
            'sell_type': sell_type,
            'return_rate': return_rate,
            'profit_loss': sell_amount - position['buy_amount'] * (quantity / position['quantity']),
            'hold_days': hold_days,
            'detail_url': self._generate_stock_detail_url(position['stock_code']),
            'trade_type': 'sell' if quantity >= position['quantity'] else 'reduce'
        }
    
    def _calculate_performance(self, trades: List[Dict], initial_capital: float, 
                              final_capital: float, dates: List[datetime.date], 
                              capital_history: List[float]) -> Dict:
        """计算绩效指标
        
        统计所有 sell_date is not None 的交易（包括已卖出和持仓虚拟交易）
        
        Args:
            trades: 交易记录
            initial_capital: 初始资金
            final_capital: 最终资金
            dates: 回测日期
            capital_history: 资金历史
            
        Returns:
            绩效指标字典
        """
        # 过滤出有卖出日期的交易（包括实际卖出和持仓虚拟卖出）
        completed_trades = [t for t in trades if t.get('sell_date') is not None]
        
        if not completed_trades:
            # 即使没有完成交易，也要计算总收益率
            total_return = ((final_capital / initial_capital) - 1) * 100
            total_return = round(total_return, 2)
            return {
                'total_trades': 0,
                'win_trades': 0,
                'loss_trades': 0,
                'win_rate': 0.0,
                'avg_return': 0.0,
                'total_return': total_return,
                'profit_factor': 0.0,
                'max_return': 0,
                'min_return': 0,
                'max_drawdown': 0.0,
                'sharpe_ratio': 0.0
            }
        
        # 计算基本指标
        total_trades = len(completed_trades)
        win_trades = sum(1 for t in completed_trades if t.get('return_rate', 0) > 0)
        loss_trades = sum(1 for t in completed_trades if t.get('return_rate', 0) < 0)
        win_rate = (win_trades / total_trades) * 100 if total_trades > 0 else 0
        
        returns = [t['return_rate'] for t in completed_trades]
        avg_return = np.mean(returns) if returns else 0
        total_return = ((final_capital / initial_capital) - 1) * 100
        total_return = round(total_return, 2)
        
        # 计算最大和最小单笔收益
        max_return = max(returns) if returns else 0
        min_return = min(returns) if returns else 0
        
        # 计算盈利因子
        winning_returns = [t['return_rate'] for t in completed_trades if t.get('return_rate', 0) > 0]
        losing_returns = [abs(t['return_rate']) for t in completed_trades if t.get('return_rate', 0) < 0]
        total_win = sum(winning_returns) if winning_returns else 0
        total_loss = sum(losing_returns) if losing_returns else 1
        profit_factor = total_win / total_loss if total_loss > 0 else 0
        
        # 计算最大回撤
        capital_array = np.array(capital_history)
        running_max = np.maximum.accumulate(capital_array)
        drawdown = (capital_array - running_max) / running_max * 100
        max_drawdown = abs(np.min(drawdown))
        
        # 计算夏普比率（假设无风险利率为2%）
        daily_returns = []
        for i in range(1, len(capital_history)):
            daily_return = (capital_history[i] - capital_history[i-1]) / capital_history[i-1] * 100
            daily_returns.append(daily_return)
        volatility = np.std(daily_returns) if daily_returns else 0
        risk_free_rate = 2.0 / 252  # 日无风险利率
        excess_returns = [r - risk_free_rate for r in daily_returns]
        sharpe_ratio = np.mean(excess_returns) / volatility * np.sqrt(252) if volatility > 0 else 0
        
        return {
            'total_trades': total_trades,
            'win_trades': win_trades,
            'loss_trades': loss_trades,
            'win_rate': win_rate,
            'avg_return': avg_return,
            'total_return': total_return,
            'max_return': max_return,
            'min_return': min_return,
            'profit_factor': profit_factor,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio
        }
