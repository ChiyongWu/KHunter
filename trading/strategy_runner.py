"""
策略运行引擎核心模块
实现量化策略的实盘运行功能，连接回测与实盘操作
"""

import sqlite3
import datetime
import logging
import threading
import numpy as np
import pandas as pd
import json
import yaml
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from utils.db_manager import DBManager
from utils.akshare_fetcher import AKShareFetcher
from strategy.strategy_registry import StrategyRegistry
from trading.stock_score_api import calculate_stock_score

from trading.timing_strategies import TimingStrategyFactory
from utils.strategy_name_mapper import get_english_name
from utils.trade_date_utils import is_trading_day, get_previous_trading_day
from utils.trading_time_validator import is_market_closed

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 策略运行全局锁，确保同一时刻只有一个策略运行任务执行
_strategy_run_lock = threading.Lock()


class StrategyRunner:
    """策略运行引擎核心类"""
    
    def __init__(self, *args, **kwargs):
        """初始化策略运行引擎
        
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
        # 自动注册所有策略
        self.strategy_registry.auto_register_from_directory()
        
        # 初始化K线数据获取器
        from utils.stock_data_fetcher import StockDataFetcher
        self.stock_data_fetcher = StockDataFetcher("data")
        from utils.kline_fetcher import KlineFetcher
        self.kline_fetcher = KlineFetcher(self.db_manager, self.stock_data_fetcher)
        
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
        self.timing_strategy_name = None
        self.timing_strategy_params = {}
        
        # 持仓信息
        self.portfolio = {}
        
        # 信号历史
        self.signals = []
        
        # 确保运行目录存在
        self.running_dir = Path("data/running")
        self.running_dir.mkdir(exist_ok=True)
        
        # 加载策略运行配置
        self.config = self._load_config()
        self.take_profit_threshold = self.config.get('take_profit_threshold', 0.15)
        self.stop_loss_threshold = self.config.get('stop_loss_threshold', -0.05)
    
    def _load_config(self) -> Dict:
        """加载策略运行配置
        
        Returns:
            配置字典
        """
        config_path = Path("config/strategy_params.yaml")
        if not config_path.exists():
            logger.warning("策略运行配置文件不存在，使用默认配置")
            return {
                'take_profit_threshold': 0.15,
                'stop_loss_threshold': -0.05,
                'max_position_size': 0.1,
                'min_position_size': 0.01,
                'max_positions': 10,
                'selection_limit': 20,
                'min_score': 70,
                'working_hour': 9,
                'working_minute': 30,
                'check_interval': 60
            }
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                runner_config = config.get('strategy_runner', {})
                logger.info("策略运行配置加载成功")
                return runner_config
        except Exception as e:
            logger.error(f"加载策略运行配置失败: {str(e)}")
            return {
                'take_profit_threshold': 0.15,
                'stop_loss_threshold': -0.05,
                'max_position_size': 0.1,
                'min_position_size': 0.01,
                'max_positions': 10,
                'selection_limit': 20,
                'min_score': 70,
                'working_hour': 9,
                'working_minute': 30,
                'check_interval': 60
            }
    
    def get_working_date(self) -> str:
        """获取当前工作日期
        
        Returns:
            工作日期字符串 (YYYY-MM-DD)
        """
        today = datetime.datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        
        # 检查是否为交易日
        if not is_trading_day(today_str):
            # 非交易日，返回最近的交易日
            working_date = get_previous_trading_day(today_str)
            logger.info(f"今日非交易日，使用前一交易日: {working_date}")
            return working_date
        
        # 检查是否已收盘
        if not is_market_closed():
            # 盘中时间，返回昨日
            working_date = get_previous_trading_day(today_str)
            logger.info(f"当前为盘中时间，使用前一交易日: {working_date}")
            return working_date
        
        # 已收盘，使用今日
        logger.info(f"使用今日作为工作日期: {today_str}")
        return today_str
    
    def check_if_processed(self, date: str) -> bool:
        """检查指定日期是否已处理
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)
            
        Returns:
            是否已处理
        """
        daily_file = self.running_dir / f"daily_{date}.json"
        return daily_file.exists()
    
    def _load_portfolio(self, portfolio_file: str) -> Dict:
        """加载持仓信息
        
        Args:
            portfolio_file: 持仓文件路径
            
        Returns:
            持仓字典
        """
        try:
            with open(portfolio_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('positions', {})
        except Exception as e:
            logger.warning(f"加载持仓文件失败: {str(e)}")
            return {}
    
    def _save_portfolio(self, portfolio: Dict, portfolio_file: str):
        """保存持仓信息
        
        Args:
            portfolio: 持仓字典
            portfolio_file: 持仓文件路径
        """
        try:
            data = {
                'last_updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'positions': portfolio
            }
            with open(portfolio_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"持仓信息已保存到: {portfolio_file}")
        except Exception as e:
            logger.error(f"保存持仓文件失败: {str(e)}")
    
    def _load_signals(self, signals_file: str) -> List:
        """加载信号历史
        
        Args:
            signals_file: 信号文件路径
            
        Returns:
            信号列表
        """
        try:
            with open(signals_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载信号文件失败: {str(e)}")
            return []
    
    def _save_signals(self, signals: List, signals_file: str):
        """保存信号历史
        
        Args:
            signals: 信号列表
            signals_file: 信号文件路径
        """
        try:
            with open(signals_file, 'w', encoding='utf-8') as f:
                json.dump(signals, f, ensure_ascii=False, indent=2)
            logger.info(f"信号已保存到: {signals_file}")
        except Exception as e:
            logger.error(f"保存信号文件失败: {str(e)}")
    
    def _save_daily_record(self, date: str, record: Dict, records_file: str):
        """保存每日记录
        
        Args:
            date: 日期
            record: 每日记录
            records_file: 记录文件路径
        """
        try:
            with open(records_file, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            logger.info(f"每日记录已保存到: {records_file}")
        except Exception as e:
            logger.error(f"保存每日记录失败: {str(e)}")
    
    def _get_stock_data(self, stock_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """获取股票数据
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            股票数据DataFrame
        """
        try:
            # 尝试从缓存获取
            cache_key = f"{stock_code}_{start_date}_{end_date}"
            if cache_key in self.stock_data_cache:
                return self.stock_data_cache[cache_key]
            
            # 从数据库获取
            df = self.stock_data_fetcher.fetch_stock_update(stock_code, start_date, end_date)
            if df is not None and not df.empty:
                self.stock_data_cache[cache_key] = df
                return df
            return None
        except Exception as e:
            logger.error(f"获取股票数据失败 {stock_code}: {str(e)}")
            return None
    
    def _get_stock_name(self, stock_code: str) -> str:
        """获取股票名称
        
        Args:
            stock_code: 股票代码
            
        Returns:
            股票名称
        """
        try:
            if stock_code in self.stock_name_cache:
                return self.stock_name_cache[stock_code]
            
            # 从数据库获取
            query = "SELECT name FROM stock_basic WHERE code = ?"
            result = self.db_manager.query_one(query, (stock_code,))
            if result:
                name = result['name']
                self.stock_name_cache[stock_code] = name
                return name
            return stock_code
        except Exception as e:
            logger.error(f"获取股票名称失败 {stock_code}: {str(e)}")
            return stock_code
    
    def _select_stocks(self, strategy_names: List[str], selection_date: str) -> List[Dict]:
        """从选股结果表查询候选股票
        
        Args:
            strategy_names: 策略名称列表
            selection_date: 选股日期
            
        Returns:
            候选股票列表
        """
        try:
            # 构建查询条件
            if strategy_names:
                placeholders = ','.join(['?'] * len(strategy_names))
                query = f"""
                SELECT stock_code, stock_name, industry, sector, selection_date, selection_price, score
                FROM stock_selection_record
                WHERE is_active = 1 
                AND selection_date >= date(?)
                AND strategy_name IN ({placeholders})
                ORDER BY score DESC
                """
                params = [selection_date] + strategy_names
            else:
                query = """
                SELECT stock_code, stock_name, industry, sector, selection_date, selection_price, score
                FROM stock_selection_record
                WHERE is_active = 1 
                AND selection_date >= date(?)
                ORDER BY score DESC
                """
                params = [selection_date]
            
            # 执行查询
            results = self.db_manager.query(query, tuple(params))
            
            # 转换为字典列表
            stocks = []
            for row in results:
                stock = {
                    'stock_code': row['stock_code'],
                    'stock_name': row['stock_name'],
                    'industry': row['industry'],
                    'sector': row['sector'],
                    'selection_date': row['selection_date'],
                    'selection_price': row['selection_price'],
                    'score': row['score']
                }
                stocks.append(stock)
            
            logger.info(f"从选股结果表查询到 {len(stocks)} 只候选股票")
            return stocks
        except Exception as e:
            logger.error(f"查询候选股票失败: {str(e)}")
            return []
    
    def _update_buy_candidate_pool(self, selected_stocks: List[Dict], trade_date: str):
        """更新可买股票池
        
        Args:
            selected_stocks: 候选股票列表
            trade_date: 交易日期
        """
        try:
            # 过滤条件：评分≥60、非ST/退市、不在持仓中
            filtered_stocks = []
            for stock in selected_stocks:
                # 检查评分
                if stock.get('score', 0) < 60:
                    continue
                
                # 检查是否已持仓
                if stock['stock_code'] in self.portfolio:
                    continue
                
                # TODO [高优先级]: 检查是否为ST/退市股票
                # 参考: doc/策略运行代码与文档差异报告.md - 待办事项
                # 实现方式: 从akshare获取股票状态信息，过滤ST和退市股票
                # 预期行为: 排除ST、*ST、退市股票
                # is_st, is_delisted = check_stock_status(stock_code)
                # if is_st or is_delisted:
                #     continue
                
                filtered_stocks.append(stock)
            
            # 更新可买股票池
            self.buy_candidate_pool = []
            for stock in filtered_stocks:
                candidate = {
                    'stock_code': stock['stock_code'],
                    'stock_name': stock['stock_name'],
                    'score': stock['score'],
                    'added_date': trade_date,
                    'tracking_days': 1,
                    'industry': stock['industry'],
                    'sector': stock['sector']
                }
                self.buy_candidate_pool.append(candidate)
            
            logger.info(f"更新可买股票池，共 {len(self.buy_candidate_pool)} 只股票")
        except Exception as e:
            logger.error(f"更新可买股票池失败: {str(e)}")
    
    def _execute_sell_operations(self, trade_date: str) -> List[Dict]:
        """执行卖出操作
        
        Args:
            trade_date: 交易日期
            
        Returns:
            卖出信号列表
        """
        sell_signals = []
        
        try:
            # 遍历持仓股票
            stocks_to_remove = []
            for stock_code, position in self.portfolio.items():
                # 获取股票数据
                end_date = trade_date
                start_date = (datetime.datetime.strptime(end_date, '%Y-%m-%d') - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
                df = self._get_stock_data(stock_code, start_date, end_date)
                
                if df is None or df.empty:
                    logger.warning(f"获取股票数据失败 {stock_code}，跳过卖出检查")
                    continue
                
                # 调用择时策略判断
                timing_result = self.timing_strategy.get_timing_result(df, position, use_prev_day_signal=False)
                
                # 检查止损止盈
                current_price = df.iloc[-1]['close']
                buy_price = position['buy_price']
                profit_rate = (current_price - buy_price) / buy_price
                
                # 生成卖出信号
                if timing_result.is_sell or profit_rate >= self.take_profit_threshold or profit_rate <= self.stop_loss_threshold:
                    signal = {
                        'id': f"sell_{stock_code}_{trade_date}",
                        'date': trade_date,
                        'stock_code': stock_code,
                        'stock_name': position['stock_name'],
                        'signal_type': 'sell',
                        'quantity': position['quantity'],
                        'price': current_price,
                        'amount': current_price * position['quantity'],
                        'reason': timing_result.message if timing_result.is_sell else 
                                 '止盈' if profit_rate >= self.take_profit_threshold else '止损',
                        'strategy_name': 'N/A',
                        'timing_strategy': self.timing_strategy_name,
                        'executed': False,
                        'executed_date': None
                    }
                    sell_signals.append(signal)
                    stocks_to_remove.append(stock_code)
            
            # 执行卖出操作
            for stock_code in stocks_to_remove:
                del self.portfolio[stock_code]
            
            logger.info(f"执行卖出操作，生成 {len(sell_signals)} 个卖出信号")
        except Exception as e:
            logger.error(f"执行卖出操作失败: {str(e)}")
        
        return sell_signals
    
    def _execute_buy_operations(self, trade_date: str, initial_cash: float, max_stocks: int) -> List[Dict]:
        """执行买入操作
        
        Args:
            trade_date: 交易日期
            initial_cash: 初始资金
            max_stocks: 最大持仓数
            
        Returns:
            买入信号列表
        """
        buy_signals = []
        
        try:
            # 检查可用资金和持仓数量
            current_cash = initial_cash - sum(p['quantity'] * p['buy_price'] for p in self.portfolio.values())
            if current_cash <= 0:
                logger.info("可用资金不足，跳过买入操作")
                return buy_signals
            
            if len(self.portfolio) >= max_stocks:
                logger.info(f"持仓数量已达上限 {max_stocks}，跳过买入操作")
                return buy_signals
            
            # 遍历可买股票池
            for candidate in self.buy_candidate_pool:
                # 检查是否已达到最大持仓数
                if len(self.portfolio) >= max_stocks:
                    break
                
                stock_code = candidate['stock_code']
                
                # 获取股票数据
                end_date = trade_date
                start_date = (datetime.datetime.strptime(end_date, '%Y-%m-%d') - datetime.timedelta(days=60)).strftime('%Y-%m-%d')
                df = self._get_stock_data(stock_code, start_date, end_date)
                
                if df is None or df.empty:
                    logger.warning(f"获取股票数据失败 {stock_code}，跳过买入检查")
                    continue
                
                # 调用择时策略判断
                timing_result = self.timing_strategy.get_timing_result(df, None, current_cash, use_prev_day_signal=False)
                
                # 生成买入信号
                if timing_result.is_buy:
                    # 计算买入数量
                    current_price = df.iloc[-1]['close']
                    buy_amount = min(current_cash * 0.2, 100000)  # 每只股票最多使用20%资金，或10万
                    buy_quantity = int(buy_amount / current_price / 100) * 100  # 按100股整数倍
                    
                    if buy_quantity <= 0:
                        continue
                    
                    signal = {
                        'id': f"buy_{stock_code}_{trade_date}",
                        'date': trade_date,
                        'stock_code': stock_code,
                        'stock_name': candidate['stock_name'],
                        'signal_type': 'buy',
                        'quantity': buy_quantity,
                        'price': current_price,
                        'amount': current_price * buy_quantity,
                        'reason': timing_result.message,
                        'strategy_name': 'N/A',
                        'timing_strategy': self.timing_strategy_name,
                        'executed': False,
                        'executed_date': None
                    }
                    buy_signals.append(signal)
                    
                    # 更新持仓
                    self.portfolio[stock_code] = {
                        'stock_name': candidate['stock_name'],
                        'quantity': buy_quantity,
                        'buy_price': current_price,
                        'buy_date': trade_date,
                        'current_price': current_price,
                        'profit_loss': 0.0,
                        'profit_rate': 0.0,
                        'holding_days': 0,
                        'industry': candidate['industry'],
                        'sector': candidate['sector']
                    }
                    
                    # 更新可用资金
                    current_cash -= current_price * buy_quantity
            
            logger.info(f"执行买入操作，生成 {len(buy_signals)} 个买入信号")
        except Exception as e:
            logger.error(f"执行买入操作失败: {str(e)}")
        
        return buy_signals
    
    def run_strategy(self, strategy_names: List[str], timing_strategy_name: str, config: Dict) -> Dict:
        """运行策略
        
        Args:
            strategy_names: 选股策略列表
            timing_strategy_name: 择时策略名称
            config: 策略运行配置参数
            
        Returns:
            策略运行结果字典
        """
        # 获取策略运行锁，确保同一时刻只有一个策略运行任务执行
        if not _strategy_run_lock.acquire(blocking=False):
            logger.warning("策略运行任务正在执行中，等待...")
            _strategy_run_lock.acquire(blocking=True)
            logger.info("获取策略运行锁，开始执行策略")
        
        try:
            logger.info(f"开始运行策略: 选股策略={strategy_names}, 择时策略={timing_strategy_name}")
            
            # 清空上次的缓存数据
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            self.stock_filtered_cache.clear()
            self.buy_candidate_pool = []
            
            # 获取配置参数
            initial_cash = config.get('initial_cash', 1000000)
            max_stocks = config.get('max_stocks', 5)
            
            # 确定工作日期
            working_date = self.get_working_date()
            logger.info(f"工作日期: {working_date}")
            
            # 检查是否已处理
            if self.check_if_processed(working_date):
                logger.info(f"日期 {working_date} 已处理，直接返回结果")
                # TODO [高优先级]: 加载并返回已处理的完整结果
                # 参考: doc/策略运行代码与文档差异报告.md - 待办事项
                # 实现方式: 从 daily_{date}.json 加载完整运行记录
                # 预期行为: 返回完整的运行结果，包括持仓、信号、股票池信息
                # daily_record = self._load_daily_record(working_date)
                # return {"status": "success", "message": "日期已处理", "data": daily_record}
                return {"status": "success", "message": "日期已处理", "data": {"date": working_date}}
            
            # 计算选股日期范围（近一个月）
            selection_start_date = (datetime.datetime.strptime(working_date, '%Y-%m-%d') - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
            
            # 加载持仓信息
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            self.portfolio = self._load_portfolio(str(portfolio_file))
            
            # 加载信号历史
            signals_file = self.running_dir / f"signals_{working_date}.json"
            self.signals = self._load_signals(str(signals_file))
            
            # 初始化择时策略
            timing_params = config.get('timing_params', {})
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
            self.timing_strategy_name = timing_strategy_name
            self.timing_strategy_params = strategy_params
            
            logger.info(f"初始化择时策略: {timing_strategy_name}")
            
            # 1. 选股
            selected_stocks = self._select_stocks(strategy_names, selection_start_date)
            
            # 2. 处理可买股票池
            self._update_buy_candidate_pool(selected_stocks, working_date)
            
            # 3. 卖出操作
            sell_signals = self._execute_sell_operations(working_date)
            
            # 4. 买入操作
            buy_signals = self._execute_buy_operations(working_date, initial_cash, max_stocks)
            
            # 5. 构建当日记录
            daily_record = {
                "date": working_date,
                "trading_date": working_date,
                "status": "completed",
                "pool_summary": {
                    "stock_count": len(self.buy_candidate_pool) + len(self.portfolio)
                },
                "pool_stocks": [
                    {
                        "code": stock['stock_code'],
                        "name": stock['stock_name'],
                        "score": stock['score'],
                        "days": stock.get('tracking_days', 1),
                        "status": "candidate"
                    } for stock in self.buy_candidate_pool
                ] + [
                    {
                        "code": code,
                        "name": pos['stock_name'],
                        "score": 0,  # 持仓股票不显示评分
                        "days": pos.get('holding_days', 0),
                        "status": "holding"
                    } for code, pos in self.portfolio.items()
                ],
                "buy_signals": buy_signals,
                "sell_signals": sell_signals,
                "portfolio": self.portfolio
            }
            
            # 6. 保存当日记录
            records_file = self.running_dir / f"daily_{working_date}.json"
            self._save_daily_record(working_date, daily_record, str(records_file))
            
            # 7. 保存信号
            signals = sell_signals + buy_signals
            self.signals.extend(signals)
            self._save_signals(self.signals, str(signals_file))
            
            # 8. 保存持仓信息
            self._save_portfolio(self.portfolio, str(portfolio_file))
            
            logger.info(f"策略运行完成: {working_date}")
            return {
                "status": "success", 
                "message": "策略运行完成",
                "data": {
                    "run_date": working_date,
                    "total_signals": len(signals),
                    "buy_signals": len(buy_signals),
                    "sell_signals": len(sell_signals),
                    "final_portfolio": {
                        "position_count": len(self.portfolio)
                    },
                    "timing_strategy": {
                        "name": self.timing_strategy_name,
                        "params": self.timing_strategy_params
                    }
                }
            }
        
        except Exception as e:
            logger.error(f"策略运行失败: {str(e)}")
            return {"status": "failed", "message": str(e)}
        finally:
            _strategy_run_lock.release()
            logger.info("释放策略运行锁")
