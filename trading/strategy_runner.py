"""
策略运行引擎核心模块（重构版）
实现量化策略的实盘运行功能，连接回测与实盘操作
支持多组合策略选择功能

重构说明：
- 使用模块化设计，职责分离
- 信号管理 -> SignalManager
- 持仓管理 -> PortfolioManager  
- 股票池管理 -> PoolManager
- 交易执行 -> TradeExecutor
- 选股策略 -> StrategySelector
"""

import datetime
import logging
import threading
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from trading.signal_manager import SignalManager
from trading.portfolio_manager import PortfolioManager
from trading.pool_manager import PoolManager
from trading.trade_executor import TradeExecutor
from trading.strategy_selector import StrategySelector

from utils.db_manager import DBManager
from utils.akshare_fetcher import AKShareFetcher
from strategy.strategy_registry import StrategyRegistry
from trading.stock_score_api import calculate_stock_score
from trading.timing_strategies import TimingStrategyFactory
from utils.strategy_name_mapper import get_english_name
from utils.trade_date_utils import is_trading_day, get_previous_trading_day
from utils.trading_time_validator import is_market_closed
from trading.strategy_kelly_loader import KellyCalculator
from trading.strategy_execution_plan import ExecutionPlan, StrategyCombination
from trading.backtest_dao import BacktestDAO

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 策略运行全局锁
_strategy_run_lock = threading.Lock()


class StrategyRunner(SignalManager, PortfolioManager, PoolManager, TradeExecutor, StrategySelector):
    """策略运行器核心类（重构版）
    
    主要职责：流程调度和协调各个模块
    """
    
    _is_running = False
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 初始化运行目录
        self.running_dir = Path("data/running")
        self.running_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化状态
        self.stock_data_cache = {}
        self.stock_name_cache = {}
        self.stock_filtered_cache = {}
        self.buy_candidate_pool = []
        self.signals = []
        self.portfolio = {}
        self.config = {}
        self.current_total_capital = 0
        self.timing_strategy = None
        self.timing_strategy_name = None
        self.timing_strategy_params = {}
        self._initialized_dates = set()
        self._exdividend_processed_date = None
        self.fund_flow_cool_down_pool = {}  # 资金流向冷却池（代码->冷却结束日期）
        
        # 默认配置
        self._DEFAULT_CONFIG = {
            'initial_capital': 1000000,
            'max_daily_buys': 3,
            'score_threshold': 60,
            'take_profit': 15,
            'stop_loss': -8
        }
    
    def run_strategy(self, strategy_names: List[str], timing_strategy_name: str, config: Dict) -> Dict:
        """运行策略（核心流程调度）
        
        流程：
        1. 获取策略运行锁
        2. 清空缓存
        3. 确定工作日期
        4. 预加载股票数据
        5. 初始化当日数据
        6. 检查是否已处理
        7. 加载持仓和信号
        8. 初始化择时策略
        9. 股票池管理（加载/更新）
        10. 执行卖出操作（生成信号）
        11. 执行买入操作（生成信号）
        12. 保存数据
        
        Args:
            strategy_names: 选股策略列表
            timing_strategy_name: 择时策略名称
            config: 运行配置
            
        Returns:
            运行结果字典
        """
        if not _strategy_run_lock.acquire(blocking=False):
            logger.warning("策略运行任务正在执行中，等待...")
            _strategy_run_lock.acquire(blocking=True)
            logger.info("获取策略运行锁，开始执行策略")
        
        try:
            logger.info(f"开始运行策略: 选股策略={strategy_names}, 择时策略={timing_strategy_name}")
            
            # 清空缓存
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            self.stock_filtered_cache.clear()
            self.buy_candidate_pool = []

            # 合并配置
            self._merge_config(config)
            
            # 确定工作日期
            working_date = self._determine_working_date(config)
            
            # 预加载股票数据
            self._preload_stock_data(working_date)
            
            # 初始化当日数据
            self.initialize_daily_data(working_date)
            
            # 检查是否已处理
            if self.check_if_processed(working_date):
                logger.info(f"日期 {working_date} 已处理，直接返回结果")
                return {"status": "success", "message": "日期已处理", "data": {"date": working_date}}
            
            # 加载持仓和信号
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            signals_file = self.running_dir / f"signals_{working_date}.json"
            
            # 检查策略是否已执行
            if self._check_strategy_executed(strategy_names, working_date, signals_file):
                return {"status": "success", "message": "策略已执行", "data": {"date": working_date}}
            
            # 加载数据
            portfolio_data = self._load_portfolio(str(portfolio_file))
            self.portfolio = portfolio_data.get('positions', {})
            self.signals = self._load_signals(str(signals_file))
            
            # 获取可用资金
            available_cash = self._get_available_cash(portfolio_data, working_date, config)
            logger.info(f"可用资金: ¥{available_cash:,.2f}")
            
            # 初始化择时策略
            self._init_timing_strategy(timing_strategy_name, config)
            
            # 股票池管理
            loaded_pool, is_first_run = self._load_pool_from_file()
            
            if not self.stock_filtered_cache:
                strategy_name = strategy_names[0] if strategy_names else 'default'
                self._preload_stock_data(working_date, strategy_name)
            
            if is_first_run:
                self._init_first_run(strategy_names, config, working_date)
            else:
                self.buy_candidate_pool = loaded_pool
                logger.info(f"从持久化文件加载股票池: {len(self.buy_candidate_pool)} 只股票")
            
            # 更新股票池
            removed_count = self._update_pool(working_date, strategy_names)
            
            # 执行交易操作（生成信号）
            sell_signals = self._execute_sell_operations(working_date, config)
            buy_signals = self._execute_buy_operations(working_date, available_cash, check_capital=False)
            
            # 保存当日数据
            self._save_daily_data(working_date, sell_signals, buy_signals, is_first_run, removed_count)
            
            logger.info(f"T日信号生成完成: {working_date}")
            return self._build_result(working_date, is_first_run, buy_signals, sell_signals)
            
        except Exception as e:
            logger.error(f"策略运行失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "failed", "message": str(e)}
        finally:
            _strategy_run_lock.release()
            logger.info("释放策略运行锁")
    
    def _merge_config(self, config: Dict):
        """合并配置参数"""
        need_backtest_config = (
            not config.get('initial_capital') or 
            not config.get('max_daily_buys') or 
            not config.get('score_threshold') or
            'take_profit' not in config or
            'stop_loss' not in config
        )
        
        if need_backtest_config:
            backtest_config = self._get_backtest_config()
            if backtest_config:
                config = {**backtest_config, **config}
        
        self.config = config
    
    def _determine_working_date(self, config: Dict) -> str:
        """确定工作日期"""
        if config.get('selection_date'):
            working_date = config['selection_date']
            logger.info(f"使用配置的选股日期: {working_date}")
        else:
            now = datetime.datetime.now()
            today = now.strftime('%Y-%m-%d')
            
            if is_trading_day(today):
                if now.hour < 15 or (now.hour == 15 and now.minute < 30):
                    working_date = get_previous_trading_day(today)
                    logger.info(f"【信号生成】当前时间 {now.strftime('%H:%M')} < 15:30，处理前一交易日数据: {working_date}")
                else:
                    working_date = today
                    logger.info(f"【信号生成】当前时间 {now.strftime('%H:%M')} >= 15:30，处理当日数据: {working_date}")
            else:
                working_date = self.get_working_date()
                logger.info(f"【信号生成】今日({today})非交易日，处理最近交易日数据: {working_date}")
        
        return working_date
    
    def _check_strategy_executed(self, strategy_names: List[str], working_date: str, signals_file: Path) -> bool:
        """检查策略是否已执行"""
        if signals_file.exists():
            try:
                with open(signals_file, 'r', encoding='utf-8') as f:
                    signals_data = json.load(f)
                
                current_strategies = set(strategy_names)
                existing_strategies = set()
                signals_list = signals_data if isinstance(signals_data, list) else signals_data.get('signals', [])
                
                for sig in signals_list:
                    if isinstance(sig, dict) and sig.get('strategy_name'):
                        existing_strategies.add(sig['strategy_name'])
                
                if current_strategies.issubset(existing_strategies):
                    logger.info(f"策略 {strategy_names} 在 {working_date} 已执行，跳过")
                    return True
            except Exception:
                pass
        
        return False
    
    def _init_timing_strategy(self, timing_strategy_name: str, config: Dict):
        """初始化择时策略"""
        timing_params = config.get('timing_params', {})
        strategy_params = timing_params.get(timing_strategy_name, {})
        
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
            turtle_specific_params = {k: v for k, v in turtle_specific_params.items() if v is not None}
            strategy_params.update(turtle_specific_params)
        
        self.timing_strategy = TimingStrategyFactory.create_strategy(timing_strategy_name, strategy_params)
        self.timing_strategy_name = timing_strategy_name
        self.timing_strategy_params = strategy_params
        
        logger.info(f"初始化择时策略: {timing_strategy_name}")
    
    def _init_first_run(self, strategy_names: List[str], config: Dict, working_date: str):
        """首次运行初始化"""
        logger.info("首次运行，使用预加载机制初始化股票池...")
        strategy_name = strategy_names[0] if strategy_names else 'ImmortalGuidanceStrategy'
        self._execute_stock_pool_preload(strategy_name, config)
        logger.info(f"首次运行初始化股票池: {len(self.buy_candidate_pool)} 只股票")
    
    def _update_pool(self, working_date: str, strategy_names: List[str]) -> int:
        """更新股票池"""
        logger.info(f"【股票池移除检查】开始检查股票池移除条件，当前股票池数量: {len(self.buy_candidate_pool)}")
        removed = self._check_pool_removal(working_date)
        removed_count = len(removed) if removed else 0
        
        if removed:
            logger.info(f"【股票池移除完成】移除 {removed_count} 只股票，剩余: {len(self.buy_candidate_pool)} 只")
        else:
            logger.info(f"【股票池移除完成】未移除任何股票，股票池数量保持: {len(self.buy_candidate_pool)} 只")
        
        added_count = 0
        for strategy_name in strategy_names:
            new_candidates = self._select_and_score_stocks(strategy_name, working_date, self.config.get('score_threshold', 60))
            for stock in new_candidates:
                if not any(item['stock']['stock_code'] == stock['stock_code'] for item in self.buy_candidate_pool):
                    support_level = self._calculate_support_level(stock, strategy_name, working_date)
                    support_method = self._get_support_method_for_strategy(strategy_name)
                    key_date = stock.get('signal', {}).get('key_date')
                    
                    if key_date:
                        if hasattr(key_date, 'strftime'):
                            key_date = key_date.strftime('%Y-%m-%d')
                        key_date = str(key_date)
                    else:
                        key_date = working_date
                    
                    self.buy_candidate_pool.append({
                        'stock': stock,
                        'added_date': working_date,
                        'key_date': key_date,
                        'strategy_name': strategy_name,
                        'support_level': support_level,
                        'support_method': support_method
                    })
                    added_count += 1
        
        self._save_pool_to_file(self.buy_candidate_pool, working_date)
        return removed_count
    
    def _save_daily_data(self, working_date: str, sell_signals: List, buy_signals: List, is_first_run: bool, removed_count: int):
        """保存当日数据"""
        daily_record = self._build_daily_record(working_date, sell_signals, buy_signals, is_first_run, removed_count)
        
        records_file = self.running_dir / f"daily_{working_date}.json"
        self._save_daily_record(working_date, daily_record, str(records_file))
        
        signals = sell_signals + buy_signals
        self.signals.extend(signals)
        
        signals_file = self.running_dir / f"signals_{working_date}.json"
        self._save_signals(self.signals, str(signals_file))
        # 仅在策略运行时保存一次CSV（用于PTrade集成）
        self._save_signals_csv(self.signals, str(signals_file))
    
    def _build_daily_record(self, working_date: str, sell_signals: List, buy_signals: List, is_first_run: bool, removed_count: int) -> Dict:
        """构建当日记录"""
        added_count = len([s for s in buy_signals])
        
        return {
            "date": working_date,
            "trading_date": working_date,
            "status": "completed",
            "is_first_run": is_first_run,
            "pool_summary": {
                "stock_count": len(self.buy_candidate_pool),
                "removed_count": removed_count,
                "added_count": added_count
            },
            "pool_stocks": [
                {
                    "code": candidate['stock']['stock_code'],
                    "name": candidate['stock']['stock_name'],
                    "score": candidate['stock'].get('score', 0),
                    "days": (datetime.datetime.strptime(working_date, '%Y-%m-%d') -
                            datetime.datetime.strptime(candidate.get('added_date', working_date), '%Y-%m-%d')).days + 1,
                    "added_date": candidate.get('added_date', working_date),
                    "support_level": candidate.get('support_level', 0),
                    "support_method": candidate.get('support_method', ''),
                    "strategy": candidate.get('strategy_name', ''),
                    "status": "candidate",
                    "is_cooling": candidate.get('is_cooling', False),
                    "cool_down_end": candidate.get('cool_down_end', None)
                } for candidate in self.buy_candidate_pool
            ] + [
                {
                    "code": code,
                    "name": pos['stock_name'],
                    "score": 0,
                    "days": pos.get('holding_days', 0),
                    "support_level": 0,
                    "support_method": "",
                    "strategy": "",
                    "status": "holding"
                } for code, pos in self.portfolio.items()
            ],
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "portfolio": self.portfolio
        }
    
    def _build_result(self, working_date: str, is_first_run: bool, buy_signals: List, sell_signals: List) -> Dict:
        """构建运行结果"""
        signals = sell_signals + buy_signals
        
        return {
            "status": "success", 
            "message": "T日信号生成完成",
            "data": {
                "run_date": working_date,
                "is_first_run": is_first_run,
                "pool_count": len(self.buy_candidate_pool),
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
    
    def run_strategies_batch(self, tasks: List[Dict], config: Dict) -> Dict:
        """批量运行策略"""
        logger.info(f"检查执行状态: _is_running={StrategyRunner._is_running}")
        
        if StrategyRunner._is_running:
            return {"status": "failed", "message": "策略正在执行中，请等待当前任务完成"}
        
        timing_strategies = [task.get('timing_strategy', 'support') for task in tasks]
        unique_timing_strategies = list(set(timing_strategies))
        
        if len(unique_timing_strategies) > 1:
            return {"status": "failed", "message": f"检测到多个不同的择时策略: {unique_timing_strategies}"}
        
        StrategyRunner._is_running = True
        
        if not _strategy_run_lock.acquire(blocking=False):
            _strategy_run_lock.acquire(blocking=True)
        
        try:
            logger.info(f"开始批量执行 {len(tasks)} 个策略任务")
            
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            
            results = []
            for task in tasks:
                selection_strategy = task.get('selection_strategy', '')
                timing_strategy = task.get('timing_strategy', 'support')
                
                result = self.run_strategy([selection_strategy], timing_strategy, config)
                results.append({
                    "task": task,
                    "result": result
                })
            
            return {"status": "success", "message": "批量执行完成", "results": results}
            
        except Exception as e:
            logger.error(f"批量执行失败: {str(e)}")
            return {"status": "failed", "message": str(e)}
        finally:
            StrategyRunner._is_running = False
            _strategy_run_lock.release()
    
    def run_plan(self, plan: ExecutionPlan, config: Dict) -> Dict:
        """运行执行计划"""
        logger.info(f"开始执行计划: {plan.name}")
        
        results = []
        for combination in plan.combinations:
            result = self.run_strategy(
                combination.selection_strategies,
                combination.timing_strategy,
                config
            )
            results.append({
                "combination": combination.name,
                "result": result
            })
        
        return {"status": "success", "message": "计划执行完成", "results": results}
    
    def get_working_date(self) -> str:
        """获取工作日期
        
        返回最近的可处理交易日。如果今天是交易日且已收盘（>= 15:30），返回今天；
        否则返回最近的历史交易日。
        """
        now = datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        
        # 检查今天是否是交易日
        if is_trading_day(today):
            # 如果时间在15:30之前，处理前一交易日的数据
            if now.hour < 15 or (now.hour == 15 and now.minute < 30):
                prev_day = get_previous_trading_day(today)
                logger.info(f"【工作日期】今日({today})是交易日但未收盘(当前{now.hour}:{now.minute:02d}<15:30)，使用前一交易日: {prev_day}")
                return prev_day
            else:
                # 15:30及之后，处理当日数据
                logger.info(f"【工作日期】今日({today})是交易日且已收盘(当前{now.hour}:{now.minute:02d}>=15:30)，使用今日作为工作日期")
                return today
        
        # 非交易日，返回最近的历史交易日
        logger.info(f"【工作日期】今日({today})非交易日，获取最近的历史交易日")
        prev_day = get_previous_trading_day(today)
        logger.info(f"【工作日期】最近的交易日: {prev_day}")
        return prev_day
    
    def check_if_processed(self, date: str) -> bool:
        """检查日期是否已处理"""
        daily_file = self.running_dir / f"daily_{date}.json"
        return daily_file.exists()
    
    def get_task_history(self, limit: int = 10) -> list:
        """获取任务历史记录
        
        Args:
            limit: 返回记录数量
            
        Returns:
            任务历史列表
        """
        history_file = self.running_dir / "task_history.json"
        if not history_file.exists():
            return []
        
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
            
            # 按时间倒序排列，返回最新的记录
            history.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return history[:limit]
        except Exception as e:
            logger.error(f"读取任务历史失败: {str(e)}")
            return []
    
    def get_last_task(self) -> dict:
        """获取上次运行的任务配置
        
        Returns:
            上次任务配置，没有则返回None
        """
        history = self.get_task_history(limit=1)
        if history:
            return history[0]
        return None
    
    def initialize_daily_data(self, date: str = None) -> bool:
        """初始化当日数据"""
        if date in self._initialized_dates:
            return True
        
        if date is None:
            date = self.get_working_date()
        
        portfolio_file = self.running_dir / f"portfolio_{date}.json"
        signals_file = self.running_dir / f"signals_{date}.json"
        
        if portfolio_file.exists() and signals_file.exists():
            self._initialized_dates.add(date)
            return True
        
        prev_date = get_previous_trading_day(date)
        prev_portfolio_file = self.running_dir / f"portfolio_{prev_date}.json"
        prev_signals_file = self.running_dir / f"signals_{prev_date}.json"
        
        if prev_portfolio_file.exists():
            portfolio_data = self._load_portfolio(str(prev_portfolio_file))
            # 完整继承持仓数据，包括 cash、initial_capital 等字段
            with open(portfolio_file, 'w', encoding='utf-8') as f:
                json.dump(portfolio_data, f, ensure_ascii=False, indent=2)
            logger.info(f"【数据初始化】从 {prev_date} 完整继承持仓数据（positions, cash, initial_capital等）")
        
        if not signals_file.exists():
            self._save_signals([], str(signals_file))
            logger.info(f"【数据初始化】创建空信号文件: {signals_file}")
        
        self._initialized_dates.add(date)
        return True
    
    def _get_available_cash(self, portfolio_data: Dict, working_date: str, config: Dict) -> float:
        """获取可用资金
        
        Args:
            portfolio_data: 持仓数据
            working_date: 工作日期
            config: 配置信息
            
        Returns:
            可用资金金额
        """
        # 优先从当前持仓文件读取 cash
        if 'cash' in portfolio_data:
            cash = portfolio_data['cash']
            logger.info(f"从当前持仓文件读取可用资金: ¥{cash:,.2f}")
            return cash
        
        # 如果当前文件没有 cash，尝试从前一天继承
        logger.warning(f"当前持仓文件缺少 cash 字段，尝试从前一交易日继承")
        prev_date = get_previous_trading_day(working_date)
        prev_portfolio_file = self.running_dir / f"portfolio_{prev_date}.json"
        
        if prev_portfolio_file.exists():
            try:
                prev_portfolio_data = self._load_portfolio(str(prev_portfolio_file))
                if 'cash' in prev_portfolio_data:
                    cash = prev_portfolio_data['cash']
                    logger.info(f"从前一交易日({prev_date})继承可用资金: ¥{cash:,.2f}")
                    return cash
            except Exception as e:
                logger.error(f"从前一交易日加载持仓失败: {str(e)}")
        
        # 最后 fallback 到 initial_capital
        initial_capital = portfolio_data.get('initial_capital', config.get('initial_capital', 1000000))
        logger.warning(f"无法获取可用资金，使用初始资金: ¥{initial_capital:,.2f}")
        return initial_capital
    
    def _get_backtest_config(self) -> Dict:
        """获取回测配置"""
        try:
            backtest_dao = BacktestDAO()
            return backtest_dao.get_latest_config()
        except Exception as e:
            logger.error(f"获取回测配置失败: {str(e)}")
            return {}
    
    # ========== 以下方法需要从原文件继承或实现 ==========
    
    def _preload_stock_data(self, current_date: str, strategy_name: str = None):
        """预加载股票数据"""
        pass
    
    def _execute_stock_pool_preload(self, strategy_name: str, config: Dict):
        """执行股票池预加载"""
        pass
    
    def _check_pool_removal(self, current_date: str) -> List[Dict]:
        """检查股票池移除条件"""
        pass
    
    def _execute_sell_operations(self, trade_date: str, config: Dict = None) -> List[Dict]:
        """执行卖出操作（生成卖出信号）"""
        pass
    
    def _execute_buy_operations(self, trade_date: str, initial_cash: float, check_capital: bool = False) -> List[Dict]:
        """执行买入操作（生成买入信号）"""
        pass
    
    def _save_daily_record(self, date: str, record: Dict, records_file: str):
        """保存当日记录"""
        pass
    
    def _get_future_trading_day(self, start_date, days: int) -> str:
        """获取未来第N个交易日
        
        Args:
            start_date: 起始日期（date对象或字符串）
            days: 天数
            
        Returns:
            未来第N个交易日的日期字符串（YYYY-MM-DD格式）
        """
        from utils.trade_date_utils import get_trading_days
        
        # 转换为date对象
        if hasattr(start_date, 'strftime'):
            start_dt = start_date
        else:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        
        # 计算目标日期（稍微扩大范围以确保找到交易日）
        from datetime import timedelta
        end_dt = start_dt + timedelta(days=days * 2)
        
        start_str = start_dt.strftime('%Y-%m-%d')
        end_str = end_dt.strftime('%Y-%m-%d')
        
        # 获取交易日列表
        trading_days = get_trading_days(start_str, end_str)
        
        # 找到第N个交易日
        if len(trading_days) > days:
            return trading_days[days]
        
        # 如果找不到足够的交易日，返回最后一天
        return trading_days[-1] if trading_days else start_str
    
    def _check_fund_flow_condition(self, stock_code: str, current_date: str) -> Dict:
        """检查资金流向移除条件
        
        规则：
        - 如果5日主力净额 < -10000万元 或者 大单净流出小单净流入：
          - 如果股票不在冷却池 → 加入冷却池3天，不移除
          - 如果股票已经在冷却池 → 直接移除
        
        Args:
            stock_code: 股票代码
            current_date: 当前日期（可以是字符串或date对象）
            
        Returns:
            包含 should_remove 和 reason 的字典
        """
        from datetime import datetime as dt
        from trading.moneyflow_scorer import MoneyflowScorer
        
        try:
            # 先清理已过期的冷却池条目（出狱逻辑）
            if stock_code in self.fund_flow_cool_down_pool:
                cool_down_end = self.fund_flow_cool_down_pool.get(stock_code, '')
                if cool_down_end:
                    try:
                        # 解析冷却结束日期
                        if hasattr(cool_down_end, 'strftime'):
                            cool_down_end_obj = cool_down_end
                        else:
                            cool_down_end_obj = dt.strptime(cool_down_end, '%Y-%m-%d').date()
                        
                        # 解析当前日期
                        if hasattr(current_date, 'strftime'):
                            current_date_obj_check = current_date
                        else:
                            current_date_obj_check = dt.strptime(current_date, '%Y-%m-%d').date()
                        
                        # 冷却期已过，股票"出狱"
                        if current_date_obj_check > cool_down_end_obj:
                            del self.fund_flow_cool_down_pool[stock_code]
                            logger.info(f"股票 {stock_code}: 资金流向冷却期结束，股票出狱")
                    except Exception as e:
                        logger.debug(f"解析冷却结束日期失败: {cool_down_end}, {e}")
            
            # 统一转换日期格式为字符串
            if hasattr(current_date, 'strftime'):
                date_str = current_date.strftime('%Y%m%d')
            else:
                date_str = str(current_date).replace('-', '')
            
            # 使用资金评分器获取资金流向数据
            scorer = MoneyflowScorer()
            df = scorer._fetch_moneyflow_data(stock_code, date_str)
            
            if df is None or df.empty:
                return {'should_remove': False, 'reason': ''}
            
            # 提取资金流向指标
            metrics = scorer._extract_flow_metrics(df)
            net_flow_5d = metrics['net_flow_5d']
            large_net = metrics['large_net']
            small_net = metrics['small_net']
            
            # 获取配置的阈值
            threshold = int(getattr(self, '_fund_flow_rules', {}).get('net_flow_threshold', -10000))
            
            # 判断条件
            condition1 = net_flow_5d < threshold
            condition2 = (large_net < 0) and (small_net > 0)
            
            if condition1 or condition2:
                # 构建原因描述
                if condition1 and condition2:
                    reason_detail = f"5日主力净额{net_flow_5d:.0f}万元<{threshold}万元且大单净流出小单净流入"
                elif condition1:
                    reason_detail = f"5日主力净额{net_flow_5d:.0f}万元<{threshold}万元"
                else:
                    reason_detail = "大单净流出且小单净流入（出货信号）"
                
                # 获取日期对象
                if hasattr(current_date, 'strftime'):
                    current_date_obj = current_date
                else:
                    current_date_obj = dt.strptime(current_date, '%Y-%m-%d').date()
                
                # 检查是否在资金流向冷却池中
                is_in_cool_down = stock_code in self.fund_flow_cool_down_pool
                
                if is_in_cool_down:
                    # 在冷却池中再次触发条件，直接移除
                    cool_down_end = self.fund_flow_cool_down_pool.get(stock_code, '')
                    reason = f"{stock_code}资金流向异常[{reason_detail}]，且已在冷却池(至{cool_down_end})，直接移除"
                    del self.fund_flow_cool_down_pool[stock_code]
                    logger.warning(reason)
                    return {'should_remove': True, 'reason': reason}
                else:
                    # 不在冷却池，加入冷却池3天
                    cool_down_end = self._get_future_trading_day(current_date_obj, 3)
                    self.fund_flow_cool_down_pool[stock_code] = cool_down_end
                    reason = f"{stock_code}资金流向异常[{reason_detail}]，加入冷却池至{cool_down_end}"
                    logger.warning(reason)
                    return {'should_remove': False, 'reason': reason}
            
            return {'should_remove': False, 'reason': ''}
            
        except Exception as e:
            logger.warning(f"检查资金流向条件失败: {stock_code}, {str(e)}")
            return {'should_remove': False, 'reason': ''}