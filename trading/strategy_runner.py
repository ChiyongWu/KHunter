"""
策略运行引擎核心模块
实现量化策略的实盘运行功能，连接回测与实盘操作
支持多组合策略选择功能
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
from typing import List, Dict, Tuple, Optional, Any
from scipy import stats

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

# 策略运行全局锁，确保同一时刻只有一个策略运行任务执行
_strategy_run_lock = threading.Lock()

# 信号执行全局锁，确保信号执行是串行的
_signal_execution_lock = threading.Lock()

# 股票池持久化文件路径
POOL_PERSIST_FILE = "data/running/buy_candidate_pool.json"


def calculate_trading_cost(stock_code: str, price: float, quantity: int, is_buy: bool, config: dict) -> dict:
    """计算交易成本（佣金、印花税、过户费、滑点）
    
    Args:
        stock_code: 股票代码
        price: 交易价格
        quantity: 交易数量
        is_buy: 是否为买入操作
        config: 交易成本配置
        
    Returns:
        成本明细字典
    """
    trading_config = config.get('trading', {})
    
    # 获取配置参数
    commission_rate = trading_config.get('commission_rate', 0.00015)  # 默认0.015%
    min_commission = trading_config.get('min_commission', 5)            # 最低佣金5元
    stamp_tax_rate = trading_config.get('stamp_tax_rate', 0.001)       # 印花税0.1%
    transfer_fee_rate = trading_config.get('transfer_fee_rate', 0.00001)  # 过户费0.001%
    
    slippage_config = trading_config.get('slippage', {})
    slippage_enabled = slippage_config.get('enabled', True)
    buy_slippage = slippage_config.get('buy_slippage', 0.01)   # 默认买入滑点+1%
    sell_slippage = slippage_config.get('sell_slippage', 0.005) # 默认卖出滑点-0.5%
    
    # 判断是否为沪市股票（6开头）
    is_shanghai = stock_code.startswith('6')
    
    # 计算成交金额
    original_amount = price * quantity
    
    # 计算滑点调整后的价格
    if slippage_enabled:
        if is_buy:
            slippage_rate = buy_slippage
        else:
            slippage_rate = sell_slippage
        adjusted_price = price * (1 + slippage_rate if is_buy else 1 - slippage_rate)
    else:
        slippage_rate = 0
        adjusted_price = price
    
    # 滑点成本
    slippage_cost = abs(adjusted_price - price) * quantity
    
    # 调整后的成交金额
    adjusted_amount = adjusted_price * quantity
    
    # 佣金（双向收取）
    commission = adjusted_amount * commission_rate
    commission = max(commission, min_commission)  # 最低佣金保底
    
    # 过户费（仅沪市股票，双向收取）
    transfer_fee = 0
    if is_shanghai:
        transfer_fee = adjusted_amount * transfer_fee_rate
    
    # 印花税（仅卖出时收取）
    stamp_tax = 0
    if not is_buy:
        stamp_tax = adjusted_amount * stamp_tax_rate
    
    # 总成本
    total_cost = slippage_cost + commission + transfer_fee + stamp_tax
    
    # 买入成本 = 成交金额 + 所有费用
    # 卖出成本 = 滑点成本 + 佣金 + 过户费 + 印花税
    if is_buy:
        total_cost = slippage_cost + commission + transfer_fee
    else:
        total_cost = slippage_cost + commission + transfer_fee + stamp_tax
    
    return {
        'original_price': price,
        'adjusted_price': round(adjusted_price, 3),
        'slippage_rate': slippage_rate,
        'slippage_cost': round(slippage_cost, 2),
        'commission': round(commission, 2),
        'transfer_fee': round(transfer_fee, 2) if is_shanghai else 0,
        'stamp_tax': round(stamp_tax, 2) if not is_buy else 0,
        'total_cost': round(total_cost, 2),
        'is_shanghai': is_shanghai,
        'original_amount': round(original_amount, 2),
        'adjusted_amount': round(adjusted_amount, 2)
    }


class StrategyRunner:
    """策略运行引擎核心类"""
    
    # 类级别：防止并发执行标志
    _is_running = False
    
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
        
        # 加载策略运行配置（必须在使用配置之前）
        self.config = self._load_config()
        
        # 当前总资金（从配置中获取初始资金）
        self.current_total_capital = self.config.get('initial_capital', 300000.0)
        
        # 持仓信息
        self.portfolio = {}
        
        # 信号历史
        self.signals = []
        
        # 确保运行目录存在（使用绝对路径）
        self.running_dir = Path(__file__).resolve().parent.parent / "data/running"
        self.running_dir.mkdir(exist_ok=True)
        
        self.take_profit_threshold = self.config.get('take_profit_threshold', 0.15)
        self.stop_loss_threshold = self.config.get('stop_loss_threshold', -0.05)
        
        # 初始化标志，避免重复初始化
        self._initialized_dates = set()
        
        # 加载回测评分器
        from trading.backtest_scorer import BacktestScoreCalculator
        self.score_calculator = BacktestScoreCalculator(db_manager=self.db_manager)
        
        # 加载股票池移除配置
        self._pool_removal_config = self._load_pool_removal_config()

        # 加载支撑位方法配置
        self._support_methods_config = self._load_support_methods_config()

        # 初始化预加载管理器（复用过回测引擎的预加载机制）
        from trading.preload_manager import PreloadManager
        self.preload_manager = PreloadManager(self)
        
        # ========== 新增：移动止损、亏损冷却期、连续亏损限制相关状态 ==========
        # 移动止损：持仓期间最高收益率
        self.position_highest_profit = {}  # {stock_code: highest_profit}
        
        # 连续亏损计数：记录每只股票的连续亏损次数（冷却状态已合并到股票池中）
        self.consecutive_loss_count = {}  # {stock_code: consecutive_loss_count}
    
    def _load_pool_removal_config(self) -> Dict:
        """加载股票池移除策略配置
        
        Returns:
            策略名称 -> 配置字典的映射
        """
        try:
            config_path = Path(__file__).parent.parent / "config" / "pool_removal_config.yaml"
            if not config_path.exists():
                logger.warning(f"股票池移除配置文件不存在: {config_path}")
                return {}
            
            with open(config_path, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
            
            config_map = {}
            strategies = yaml_config.get('removal_strategies', {})
            strategy_count = 0  # 统计实际的策略数量
            for name, cfg in strategies.items():
                if cfg.get('is_enabled', True):
                    strategy_count += 1
                    config_map[name] = {
                        'min_hold_days': cfg.get('min_hold_days', 2),
                        'display_name': cfg.get('display_name', '')
                    }
                    display_name = cfg.get('display_name', '')
                    if display_name:
                        config_map[display_name] = config_map[name]
                        if display_name.endswith('策略'):
                            config_map[display_name[:-2]] = config_map[name]
            
            logger.info(f"加载股票池移除策略: {strategy_count} 个策略")
            return config_map
        except Exception as e:
            logger.warning(f"加载股票池移除配置失败: {str(e)}")
            return {}
    
    def _load_support_methods_config(self) -> Dict:
        """加载策略支撑位方法配置
        
        Returns:
            策略名称 -> 支撑位配置字典
        """
        try:
            config_path = Path(__file__).parent.parent / "config" / "support_methods.yaml"
            if not config_path.exists():
                logger.warning(f"支撑位方法配置文件不存在: {config_path}")
                return {}
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            
            strategies_config = config.get('strategies', {})
            logger.info(f"加载支撑位方法配置: {len(strategies_config)} 个策略")
            return strategies_config
        except Exception as e:
            logger.warning(f"加载支撑位方法配置失败: {str(e)}")
            return {}

    def _load_task_history(self) -> List[Dict]:
        """加载任务历史记录
        
        Returns:
            任务历史列表
        """
        try:
            history_file = self.running_dir / "task_history.json"
            if history_file.exists():
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    logger.info(f"加载任务历史记录: {len(history)} 条")
                    return history
            return []
        except Exception as e:
            logger.warning(f"加载任务历史记录失败: {str(e)}")
            return []

    def _save_task_history(self, history: List[Dict]):
        """保存任务历史记录
        
        Args:
            history: 任务历史列表
        """
        try:
            history_file = self.running_dir / "task_history.json"
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            logger.info(f"保存任务历史记录: {len(history)} 条")
        except Exception as e:
            logger.error(f"保存任务历史记录失败: {str(e)}")

    def save_task_record(self, task_config: Dict) -> Dict:
        """保存任务运行记录
        
        Args:
            task_config: 任务配置，包含 strategies、initial_capital 等
            
        Returns:
            操作结果
        """
        try:
            record = {
                'id': datetime.datetime.now().strftime('%Y%m%d_%H%M%S'),
                'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strategies': task_config.get('strategies', []),
                'initial_capital': task_config.get('initial_capital', 300000),
                'mode': task_config.get('mode', 'realtime'),
                'status': 'completed'
            }
            
            # 只保留上次执行的任务，不累积历史记录
            history = [record]
            
            self._save_task_history(history)
            
            return {'success': True, 'record': record}
        except Exception as e:
            logger.error(f"保存任务记录失败: {str(e)}")
            return {'success': False, 'error': str(e)}

    def get_task_history(self, limit: int = 10) -> List[Dict]:
        """获取任务历史记录
        
        Args:
            limit: 返回记录数量限制
            
        Returns:
            任务历史列表
        """
        history = self._load_task_history()
        return history[:limit]

    def get_last_task(self) -> Dict:
        """获取上次运行的任务配置
        
        Returns:
            上次任务配置，如果没有则返回空策略列表
        """
        history = self._load_task_history()
        if history:
            last_record = history[0]
            return {
                'strategies': last_record.get('strategies', []),
                'initial_capital': last_record.get('initial_capital', 300000),
                'mode': last_record.get('mode', 'realtime')
            }
        return None
    
    def _get_support_method_for_strategy(self, strategy_name: str) -> str:
        """获取策略的支撑位计算方法
        
        Args:
            strategy_name: 策略名称
            
        Returns:
            支撑位计算方法（ma20/key_close_5/key_open/key_close）
        """
        strategy_config = self._support_methods_config.get(strategy_name, {})
        if isinstance(strategy_config, dict):
            return strategy_config.get('support_method', 'ma20')
        elif isinstance(strategy_config, str):
            return strategy_config
        return 'ma20'
    
    def _get_strategy_removal_config(self, strategy_name: str) -> Dict:
        """获取策略的移除配置
        
        Args:
            strategy_name: 策略名称
            
        Returns:
            移除配置字典
        """
        if strategy_name in self._pool_removal_config:
            return self._pool_removal_config[strategy_name]
        
        if not strategy_name.endswith('策略'):
            with_strategy = strategy_name + '策略'
            if with_strategy in self._pool_removal_config:
                return self._pool_removal_config[with_strategy]
        
        if strategy_name.endswith('策略'):
            without_strategy = strategy_name[:-2]
            if without_strategy in self._pool_removal_config:
                return self._pool_removal_config[without_strategy]
        
        # 默认配置
        return {'min_hold_days': 2}
    
    # ==================== 股票池持久化方法 ====================
    
    def _load_pool_from_file(self, working_date: str = None) -> Tuple[List[Dict], bool]:
        """从文件加载股票池
        
        Args:
            working_date: 当前工作日期，用于检查是否当天已经执行过
            
        Returns:
            (股票池列表, 是否需要重新执行)
            - 需要重新执行的情况：文件不存在
            - 不需要重新执行的情况：文件存在（直接继承，不重新执行选股）
        """
        pool_file = Path(POOL_PERSIST_FILE)
        if not pool_file.exists():
            logger.info("股票池文件不存在，将初始化")
            return [], True
        
        try:
            with open(pool_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            pool = data.get('pool', [])
            last_date = data.get('last_date', '')
            
            logger.info(f"从文件加载股票池: {len(pool)} 只股票，上次运行日期: {last_date}")
            
            # 用户需求：直接继承 buy_candidate_pool.json，不重建
            # 无论日期是否匹配，只要文件存在就直接加载
            # 需要重新执行选股的情况只有文件不存在时
            return pool, False
        except Exception as e:
            logger.error(f"加载股票池文件失败: {str(e)}")
            return [], True
    
    def _save_pool_to_file(self, pool: List[Dict], date: str):
        """保存股票池到文件
        
        Args:
            pool: 股票池列表
            date: 当前日期
        """
        try:
            # 确保每个候选股票都有冷却状态标记（已合并到股票池中）
            for item in pool:
                # 如果没有冷却状态标记，初始化默认值
                if 'is_cooling' not in item:
                    item['is_cooling'] = False
                if 'cool_down_end' not in item:
                    item['cool_down_end'] = None
            
            # 打印股票池清单
            logger.info(f"========== 股票池清单 ({len(pool)} 只) ==========")
            for i, item in enumerate(pool, 1):
                stock = item.get('stock', item)
                code = stock.get('stock_code', 'N/A')
                name = stock.get('stock_name', 'N/A')
                score = stock.get('score', 0)
                added_date = item.get('added_date', 'N/A')
                support = item.get('support_level', 0)
                strategy = item.get('strategy_name', 'N/A')
                is_cooling = item.get('is_cooling', False)
                cool_down_end = item.get('cool_down_end', 'N/A')
                cooling_info = f" | 冷却中" if is_cooling else ""
                if is_cooling and cool_down_end:
                    cooling_info = f" | 冷却中(至{cool_down_end})"
                logger.info(f"  {i}. {code} {name} | 评分: {score:.1f} | 入池: {added_date} | 支撑: ¥{support:.2f} | 策略: {strategy}{cooling_info}")
            logger.info("=" * 50)
            
            data = {
                'last_date': date,
                'pool': pool,
                'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(POOL_PERSIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            
            logger.info(f"股票池已保存: {len(pool)} 只股票")
        except Exception as e:
            logger.error(f"保存股票池文件失败: {str(e)}")
    
    # ==================== 预加载股票数据 ====================
    
    def _preload_stock_data(self, current_date: str, strategy_name: str = None):
        """预加载所有股票数据到内存
        
        Args:
            current_date: 当前日期
            strategy_name: 策略名称，用于计算需要的历史数据天数
        """
        from datetime import datetime, timedelta
        current_dt = datetime.strptime(current_date, '%Y-%m-%d')
        
        # 根据策略参数计算需要的历史数据天数
        buffer_days = 60
        required_days = buffer_days
        
        if strategy_name:
            strategy = self.strategy_registry.get_strategy(strategy_name)
            if strategy and hasattr(strategy, 'params'):
                params = strategy.params
                max_value = 0
                
                lookback_keys = [
                    'lookback_days', 'pattern_days', 'limit_up_lookback_days',
                    'lowest_point_lookback_days', 'surge_lookback_days', 'uptrend_lookback_days'
                ]
                period_keys = ['ma_period', 'ma_short_period', 'ma_long_period', 'kdj_n',
                             'macd_short', 'macd_long', 'macd_signal', 'volume_ma_period']
                
                for key in lookback_keys + period_keys:
                    if key in params:
                        val = params[key]
                        if isinstance(val, (int, float)):
                            max_value = max(max_value, int(val))
                
                required_days = max_value + buffer_days
        
        # 扩展开始日期
        extended_start = (current_dt - timedelta(days=required_days)).strftime('%Y-%m-%d')
        logger.info(f"预加载股票数据: {extended_start} ~ {current_date} (历史: {required_days}天)")
        
        # 获取所有股票代码
        stock_codes = self.db_manager.list_all_stocks()
        total = len(stock_codes)
        loaded = 0
        skipped = 0
        
        for i, code in enumerate(stock_codes):
            try:
                df = self.db_manager.read_stock(code)
                
                if df is None or (hasattr(df, 'empty') and df.empty) or len(df) < 60:
                    skipped += 1
                    continue
                
                # 缓存原始数据
                df_copy = df.copy()
                df_copy['date'] = df_copy['date'].dt.strftime('%Y-%m-%d')
                self.stock_data_cache[code] = df_copy
                
                # 获取股票名称
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
                df_filtered['date'] = df_filtered['date'].dt.strftime('%Y-%m-%d')
                self.stock_filtered_cache[code] = df_filtered
                loaded += 1
                
            except Exception as e:
                logger.debug(f"预加载股票 {code} 失败: {str(e)}")
                skipped += 1
            
            if (i + 1) % 500 == 0:
                logger.info(f"预加载进度: {i + 1}/{total}, 有效股票: {loaded}, 跳过: {skipped}")
        
        logger.info(f"预加载完成: 有效股票 {loaded}, 跳过 {skipped}, 总计 {total}")

    # ==================== 初始股票池预加载 ====================

    def _execute_stock_pool_preload(self, strategy_name: str, config: Dict):
        """Execute initial stock pool preload (first run)

        Reuse PreloadManager mechanism, keep consistent with backtest engine.
        Note: Only preload data, do not execute stock selection strategy

        Args:
            strategy_name: Stock selection strategy name
            config: Configuration parameters
        """
        preload_enabled = config.get('preload_enabled', True)
        
        # User requirement: Do not preload stock selection, but data preload is needed
        if not preload_enabled:
            logger.info("Preload function is disabled")
            return
        
        # Only preload data if not already loaded (avoid duplicate preload)
        if self.stock_filtered_cache:
            logger.info("股票数据已预加载，跳过重复预加载")
            return
        
        logger.info("-------------------- Preload Stock Data --------------------")
        working_date = self.get_working_date()
        self._preload_stock_data(working_date, strategy_name)
        logger.info("Stock data preload completed, no preload stock selection")

    # ==================== 选股和评分 ====================
    
    def _execute_selection(self, strategy_name: str, current_date: str) -> List[Dict]:
        """执行选股（从缓存读取，使用日期切片）
        
        Args:
            strategy_name: 策略名称
            current_date: 选股日期
            
        Returns:
            选股结果列表
        """
        try:
            # 确保策略已注册
            if not self.strategy_registry.strategies:
                self.strategy_registry.auto_register_from_directory()
            
            # 获取策略
            from utils.strategy_name_mapper import get_english_name
            mapped_name = get_english_name(strategy_name)
            strategy = self.strategy_registry.get_strategy(mapped_name)
            
            if not strategy:
                strategy = self.strategy_registry.get_strategy(strategy_name)
            
            if not strategy:
                raise ValueError(f"策略 {strategy_name} 不存在")
            
            standardized_stocks = []
            
            # 从缓存遍历有效股票
            for code, df in self.stock_filtered_cache.items():
                try:
                    # 日期切片
                    df_to_date = df[df['date'] <= current_date].copy()
                    
                    if df_to_date.empty:
                        continue
                    
                    # 反转数据为倒序
                    if len(df_to_date) > 1 and df_to_date['date'].iloc[0] < df_to_date['date'].iloc[-1]:
                        df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
                    
                    # 获取股票名称
                    name = self.stock_name_cache.get(code, "未知")
                    
                    # 执行选股（传递当前日期用于停牌检查）
                    signal_list = strategy.execute_selection(df_to_date, code, name, selection_date=current_date)
                    
                    if signal_list:
                        for signal in signal_list:
                            stock_info = {
                                'stock_code': code,
                                'stock_name': name,
                                'signal': signal,
                                'detail_url': f"javascript:viewStockDetail('{code}')"
                            }
                            standardized_stocks.append(stock_info)
                
                except Exception as e:
                    logger.debug(f"股票 {code} 选股失败: {str(e)}")
                    continue
            
            logger.info(f"{strategy_name} 策略在 {current_date} 选出 {len(standardized_stocks)} 只股票")
            return standardized_stocks
            
        except Exception as e:
            logger.error(f"执行选股失败: {str(e)}")
            return []
    
    def _score_stocks(self, stocks: List[Dict], strategy_name: str, current_date: str) -> List[Dict]:
        """对股票进行评分
        
        Args:
            stocks: 股票列表
            strategy_name: 策略名称
            current_date: 评分日期
            
        Returns:
            带评分的股票列表
        """
        if not stocks:
            return []
        
        # 获取策略的中文名称
        strategy = self.strategy_registry.get_strategy(strategy_name)
        strategy_display_name = strategy.name if strategy else strategy_name
        
        # 使用回测评分器进行批量评分
        scored_stocks = self.score_calculator.calculate_batch_scores(
            stocks=stocks,
            score_date=current_date,
            strategy_name=strategy_display_name
        )
        
        return scored_stocks
    
    def _select_and_score_stocks(self, strategy_name: str, current_date: str, score_threshold: int = 60) -> List[Dict]:
        """执行选股、评分、筛选，得到候选股票池
        
        Args:
            strategy_name: 策略名称
            current_date: 当前日期
            score_threshold: 评分阈值
            
        Returns:
            候选股票列表
        """
        logger.info(f"开始执行选股，策略: {strategy_name}，日期: {current_date}")
        
        # 执行选股
        selected_stocks = self._execute_selection(strategy_name, current_date)
        logger.info(f"选股完成，共选出 {len(selected_stocks)} 只股票")
        
        if not selected_stocks:
            return []
        
        # 评分
        logger.info(f"开始对 {len(selected_stocks)} 只股票进行评分")
        scored_stocks = self._score_stocks(selected_stocks, strategy_name, current_date)
        
        # 筛选：去除否决票且评分达标
        candidate_stocks = [
            stock for stock in scored_stocks 
            if not stock.get('veto_flag', False) and stock['score'] >= score_threshold
        ]
        
        logger.info(f"筛选后待买入股票数: {len(candidate_stocks)}")
        return candidate_stocks
    
    # ==================== 股票池移除检查 ====================
    
    def _calculate_support_level(self, stock: Dict, strategy_name: str, current_date: str) -> float:
        """计算候选股票的支撑位
        
        Args:
            stock: 股票信息
            strategy_name: 策略名称
            current_date: 当前日期
            
        Returns:
            支撑位价格
        """
        stock_code = stock['stock_code']
        support_method = self._get_support_method_for_strategy(strategy_name)
        
        df = self.stock_filtered_cache.get(stock_code)
        if df is None:
            return 0.0
        
        df_to_date = df[df['date'] <= current_date].copy()
        if df_to_date.empty:
            return 0.0
        
        if len(df_to_date) > 1 and df_to_date['date'].iloc[0] > df_to_date['date'].iloc[1]:
            df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
        
        if support_method == 'ma20':
            if len(df_to_date) >= 20:
                return round(df_to_date['close'].tail(20).mean(), 2)
            
        elif support_method in ['key_close_5', 'key_open', 'key_close']:
            signal = stock.get('signal', {})
            key_date = signal.get('key_date') if isinstance(signal, dict) else None
            
            if key_date:
                key_date_str = str(key_date)[:10]
                key_date_data = df_to_date[df_to_date['date'].astype(str).str[:10] == key_date_str]
                
                if not key_date_data.empty:
                    if support_method == 'key_close_5':
                        return round(float(key_date_data.iloc[0]['close']) * 0.95, 2)
                    elif support_method == 'key_open':
                        return round(float(key_date_data.iloc[0]['open']), 2)
                    elif support_method == 'key_close':
                        return round(float(key_date_data.iloc[0]['close']), 2)
        
        # fallback: 使用20日均线
        if len(df_to_date) >= 20:
            return round(df_to_date['close'].tail(20).mean(), 2)
        
        return 0.0
    
    def _check_pool_removal(self, current_date: str) -> List[Dict]:
        """检查股票池中需要移除的股票
        
        移除条件：
        1. 破支撑位：前一日收盘价 < 支撑位 × 0.98
        2. 不满足上升趋势条件（持有 min_hold_days 天后生效）
            - 收盘价 >= MA10
            - 20日线性回归斜率 > 0
            - 20日R²拟合度 >= 0.3
        
        Args:
            current_date: 当前交易日期
            
        Returns:
            移除的候选列表
        """
        from utils.trade_date_utils import get_previous_trading_day
        
        removed = []
        remaining = []
        
        # 获取前一个交易日
        prev_date = get_previous_trading_day(current_date)
        prev_date_str = prev_date.strftime('%Y-%m-%d') if isinstance(prev_date, datetime.datetime) else prev_date
        
        for candidate in self.buy_candidate_pool:
            stock_code = candidate['stock']['stock_code']
            stock_name = candidate['stock']['stock_name']
            strategy_name = candidate.get('strategy_name', '')
            
            # 获取移除配置
            removal_config = self._get_strategy_removal_config(strategy_name)
            min_hold_days = removal_config.get('min_hold_days', 2)
            
            # 计算持有天数
            added_date = candidate.get('added_date', '')
            if added_date:
                if isinstance(added_date, datetime.datetime):
                    added_date = added_date.strftime('%Y-%m-%d')
                try:
                    added_dt = datetime.datetime.strptime(added_date, '%Y-%m-%d')
                    if isinstance(prev_date, datetime.datetime):
                        hold_days = (prev_date - added_dt).days
                    else:
                        prev_dt = datetime.datetime.strptime(prev_date_str, '%Y-%m-%d')
                        hold_days = (prev_dt - added_dt).days
                except:
                    hold_days = 0
            else:
                hold_days = 0
            
            # 获取股票数据
            df = self.stock_filtered_cache.get(stock_code)
            if df is None:
                remaining.append(candidate)
                continue
            
            # 判断是否为当日新加入的股票
            is_today_added = (added_date == current_date) if added_date else False
            
            if is_today_added:
                # 当日新加入的股票：使用当日收盘价进行支撑位判断
                # 注意：缓存数据是倒序排列的（最新日期在前面）
                df_to_date = df[df['date'] <= current_date].copy()
                if len(df_to_date) < 20:
                    remaining.append(candidate)
                    continue
                price_for_check = df_to_date.iloc[0]['close']  # 最新数据在 iloc[0]
            else:
                # 非当日加入的股票：使用前一日收盘价
                df_to_date = df[df['date'] <= prev_date_str].copy()
                if len(df_to_date) < 20:
                    remaining.append(candidate)
                    continue
                price_for_check = df_to_date.iloc[0]['close']  # 最新数据在 iloc[0]
            
            # 用于趋势判断的数据（需要至少20天）
            trend_df = df[df['date'] <= current_date].copy()
            
            if trend_df['date'].iloc[0] > trend_df['date'].iloc[-1]:
                trend_df = trend_df.iloc[::-1].reset_index(drop=True)
            
            # 移除判断
            removal_reasons = []
            should_remove = False
            
            # 条件1: 破支撑位移除
            support_level = candidate.get('support_level', 0.0)
            if support_level > 0 and price_for_check > 0:
                if price_for_check < support_level * 0.98:
                    should_remove = True
                    drop_pct = (price_for_check - support_level) / support_level * 100
                    removal_reasons.append(f"跌破支撑位{support_level:.2f}{drop_pct:.1f}%")
            
            # 条件2: 趋势验证移除（需要至少20天数据）
            if hold_days >= min_hold_days and len(trend_df) >= 20:
                ma10 = trend_df['close'].tail(10).mean()
                prices = trend_df['close'].tail(20).values
                x = np.arange(len(prices))
                slope, _, r_value, _, _ = stats.linregress(x, prices)
                r_squared = r_value ** 2
                
                trend_ok = (price_for_check >= ma10 and slope > 0 and r_squared >= 0.3)
                
                if not trend_ok:
                    should_remove = True
                    if price_for_check < ma10:
                        removal_reasons.append(f"收盘价{price_for_check:.2f}<MA10{ma10:.2f}")
                    if slope <= 0:
                        removal_reasons.append(f"斜率{slope:.4f}<=0")
                    if r_squared < 0.3:
                        removal_reasons.append(f"R²{r_squared:.4f}<0.3")
            
            if should_remove:
                removed.append(candidate)
                logger.info(f"【移除】{current_date} {stock_code} {stock_name}: "
                           f"收盘={price_for_check:.2f}, 策略={strategy_name}, 持{hold_days}日, "
                           f"原因: {'; '.join(removal_reasons)}")
            else:
                remaining.append(candidate)
        
        if removed:
            logger.info(f"股票池移除: {len(removed)} 只, 剩余: {len(remaining)} 只")
            self.buy_candidate_pool = remaining
        
        return removed
    
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
    
    def _get_backtest_config(self) -> Dict:
        """获取回测配置
        
        从数据库获取最新的回测配置，用于策略运行的默认参数。
        与回测引擎保持一致，使用相同的配置源。
        
        Returns:
            回测配置字典，包含 initial_capital, max_daily_buys, score_threshold 等参数
        """
        try:
            backtest_dao = BacktestDAO()
            configs = backtest_dao.get_all_configs()
            
            if configs and len(configs) > 0:
                # 使用最新的配置
                config = configs[0]
                logger.info(f"获取到回测配置: config_name={config.get('config_name')}")
                return {
                    'initial_capital': config.get('initial_capital', 300000),
                    'max_daily_buys': config.get('max_daily_buys', 8),
                    'score_threshold': config.get('score_threshold', 60),
                    'buy_amount': config.get('buy_amount', 100000),
                    'stop_loss': config.get('stop_loss', -5),
                    'take_profit': config.get('take_profit', 15),
                    'hold_period': config.get('hold_period', 10),
                    'support_level_method': config.get('support_level_method', 'ma20'),
                    'timing_strategy': config.get('timing_strategy', 'support'),
                    'temp_limit_mode': config.get('temp_limit_mode', 'both')
                }
            else:
                logger.warning("未找到回测配置，使用默认参数")
                return {
                    'initial_capital': 300000,
                    'max_daily_buys': 8,
                    'score_threshold': 60,
                    'buy_amount': 100000,
                    'stop_loss': -5,
                    'take_profit': 15,
                    'hold_period': 10,
                    'support_level_method': 'ma20',
                    'timing_strategy': 'support',
                    'temp_limit_mode': 'both'
                }
        except Exception as e:
            logger.error(f"获取回测配置失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {}
    
    def _check_cool_down(self, stock_code: str, current_date: str) -> bool:
        """检查股票是否在冷却期内（从股票池的属性中检查）

        Args:
            stock_code: 股票代码
            current_date: 当前日期字符串
            
        Returns:
            True表示在冷却期内，False表示不在冷却期
        """
        # 遍历股票池，查找该股票
        for candidate in self.buy_candidate_pool:
            stock = candidate.get('stock', candidate)
            code = stock.get('stock_code', '')
            if code == stock_code:
                is_cooling = candidate.get('is_cooling', False)
                cool_down_end = candidate.get('cool_down_end', None)
                
                if not is_cooling or not cool_down_end:
                    return False
                
                # 检查冷却是否过期
                cool_down_end_date = datetime.datetime.strptime(cool_down_end, '%Y-%m-%d').date()
                current_date_obj = datetime.datetime.strptime(current_date, '%Y-%m-%d').date()
                
                if current_date_obj <= cool_down_end_date:
                    return True
                else:
                    # 冷却期结束，更新状态
                    candidate['is_cooling'] = False
                    candidate['cool_down_end'] = None
                    return False
        
        return False

    def _update_stock_cool_down_status(self, stock_code: str, is_cooling: bool, cool_down_end: str = None):
        """更新股票池中的冷却状态
        
        Args:
            stock_code: 股票代码
            is_cooling: 是否在冷却期
            cool_down_end: 冷却结束日期（is_cooling=True时必填）
        """
        for candidate in self.buy_candidate_pool:
            stock = candidate.get('stock', candidate)
            code = stock.get('stock_code', '')
            if code == stock_code:
                candidate['is_cooling'] = is_cooling
                candidate['cool_down_end'] = cool_down_end
                return

    def _is_stock_cooling(self, stock_code: str) -> bool:
        """检查股票是否正在冷却（快速检查，不更新状态）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            True表示正在冷却，False表示不在冷却期
        """
        for candidate in self.buy_candidate_pool:
            stock = candidate.get('stock', candidate)
            code = stock.get('stock_code', '')
            if code == stock_code:
                return candidate.get('is_cooling', False)
        return False

    def _get_future_trading_day(self, start_date: str, days: int) -> str:
        """获取指定日期之后的第N个交易日
        
        Args:
            start_date: 起始日期字符串
            days: 往后多少个交易日
            
        Returns:
            目标交易日字符串
        """
        # 粗略估计：每个交易日约1天（忽略周末）
        # 实际实现可以从交易日历获取
        current_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
        estimated_days = int(days * 1.4)
        future_date = current_date + datetime.timedelta(days=estimated_days)
        return future_date.strftime('%Y-%m-%d')
    
    def _has_kline_data(self, date: str) -> bool:
        """检查指定日期是否有K线数据
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)
            
        Returns:
            True表示有K线数据，False表示没有
        """
        try:
            # 从数据库检查是否有当日K线数据
            from utils.db_manager import DBManager
            db_manager = DBManager()
            
            # 将日期格式转换为数据库存储的格式 (YYYY-MM-DD -> YYYYMMDD)
            date_db = date.replace('-', '')
            
            # 查询是否有当日的股票数据
            sql = f"""
                SELECT COUNT(*) FROM stock_kline 
                WHERE date = '{date_db}' 
                LIMIT 1
            """
            result = db_manager.query(sql)
            
            return result[0]['COUNT(*)'] > 0 if result else False
        except Exception as e:
            logger.warning(f"检查K线数据失败: {str(e)}")
            return False
    
    def get_working_date(self) -> str:
        """获取当前工作日期
        
        判断逻辑（根据设计文档）：
        1. 如果当前是交易日 AND 当前时间 > 收盘时间(15:00): 处理日期 = 今日
        2. 如果当前是交易日 AND 当前时间 <= 收盘时间: 处理日期 = 昨日
        3. 否则（非交易日）: 处理日期 = 最近一个交易日
        
        Returns:
            工作日期字符串 (YYYY-MM-DD)
        """
        today = datetime.datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        
        # 优先检查是否是交易日
        if is_trading_day(today_str):
            # 检查是否已收盘
            if is_market_closed():
                # 已收盘，使用今日
                logger.info(f"当日是交易日且已收盘，使用今日作为工作日期: {today_str}")
                return today_str
            else:
                # 未收盘，使用前一交易日
                working_date = get_previous_trading_day(today_str)
                logger.info(f"当日是交易日但未收盘，使用前一交易日: {working_date}")
                return working_date
        
        # 不是交易日，返回前一交易日
        working_date = get_previous_trading_day(today_str)
        logger.info(f"当日不是交易日，使用前一交易日: {working_date}")
        return working_date
    
    def _get_working_date_for_test(self, date_str: str) -> str:
        """测试用方法：获取指定日期的工作日期"""
        if self._has_kline_data(date_str):
            return date_str
        return get_previous_trading_day(date_str)
    
    def check_if_processed(self, date: str) -> bool:
        """检查指定日期是否已处理
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)
            
        Returns:
            是否已处理
        """
        # 检查每日报告文件或信号文件是否存在
        daily_file = self.running_dir / f"daily_{date}.json"
        signals_file = self.running_dir / f"signals_{date}.json"
        return daily_file.exists() or signals_file.exists()
    
    def initialize_daily_data(self, date: str = None) -> bool:
        """初始化当日数据
        
        当检测到新的交易日时，自动从前一交易日继承持仓数据来初始化当日数据。
        如果当日数据文件已存在，则跳过初始化。
        如果前一交易日没有数据，则继续向前查找更早的交易日，直到找到有数据的那天。
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认为当前工作日期
            
        Returns:
            是否成功初始化
        """
        # 如果未指定日期，使用当前工作日期
        if date is None:
            date = self.get_working_date()
        
        logger.info(f"【数据初始化】开始检查并初始化 {date} 的数据")
        
        # 检查是否已经初始化过
        if date in self._initialized_dates:
            logger.debug(f"【数据初始化】{date} 已经初始化过，跳过")
            return False
        
        # 检查当日持仓文件是否已存在
        portfolio_file = self.running_dir / f"portfolio_{date}.json"
        signals_file = self.running_dir / f"signals_{date}.json"
        trades_file = self.running_dir / f"trades_{date}.json"
        
        # 如果当日数据文件已存在，说明已经初始化过
        if portfolio_file.exists():
            logger.info(f"【数据初始化】{date} 的持仓文件已存在，跳过初始化")
            self._initialized_dates.add(date)
            return False
        
        # 查找有数据的最近交易日（向前查找最多30天）
        current_date = date
        days_looked = 0
        max_days = 30
        found_date = None
        days_between = 0
        
        while days_looked < max_days:
            # 获取前一交易日
            current_date = get_previous_trading_day(current_date)
            days_looked += 1
            
            # 检查该交易日是否有持仓数据
            prev_portfolio_file = self.running_dir / f"portfolio_{current_date}.json"
            if prev_portfolio_file.exists():
                found_date = current_date
                days_between = days_looked
                break
        
        if found_date:
            logger.info(f"【数据初始化】{date} 的持仓文件不存在，从最近有数据的交易日 {found_date} 继承数据（间隔 {days_between} 个交易日）")
            
            # 加载找到的交易日的持仓数据
            prev_data = self._load_portfolio(str(prev_portfolio_file))
            prev_positions = prev_data.get('positions', {})
            prev_cash = prev_data.get('cash', 300000)
            prev_initial_capital = prev_data.get('initial_capital', 300000)
            
            # 更新持仓的持有天数（加上间隔的交易日数）
            updated_positions = {}
            for code, pos in prev_positions.items():
                updated_pos = pos.copy()
                updated_pos['holding_days'] = pos.get('holding_days', 0) + days_between
                updated_pos['profit_loss'] = 0.0
                updated_pos['profit_rate'] = 0.0
                updated_positions[code] = updated_pos
            
            # 保存当日持仓文件
            self.current_total_capital = prev_cash
            self.initial_capital = prev_initial_capital
            
            # 计算总资产（可用资金 + 持仓市值）
            total_assets = prev_cash
            for code, pos in updated_positions.items():
                # 使用持仓中的当前价格
                position_price = pos.get('current_price', pos.get('buy_price', 0))
                total_assets += pos.get('quantity', 0) * position_price
            self.current_total_assets = total_assets
            
            self._save_portfolio(updated_positions, str(portfolio_file))
            logger.info(f"【数据初始化】成功从 {found_date} 继承持仓数据，共 {len(updated_positions)} 只股票，总资产: ¥{total_assets:.2f}")
        else:
            # 找不到有数据的交易日，使用初始资金初始化
            self.current_total_capital = self.config.get('initial_capital', 300000)
            self.initial_capital = self.current_total_capital
            self._save_portfolio({}, str(portfolio_file))
            logger.info(f"【数据初始化】未找到有数据的历史交易日，使用初始资金 {self.current_total_capital} 初始化")
        
        # 创建空的信号文件（如果不存在）
        if not signals_file.exists():
            self._save_signals([], str(signals_file))
            logger.info(f"【数据初始化】创建空信号文件: {signals_file}")
        
        # 创建空的交易记录文件（如果不存在）
        if not trades_file.exists():
            with open(trades_file, 'w', encoding='utf-8') as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            logger.info(f"【数据初始化】创建空交易记录文件: {trades_file}")
        
        # 标记为已初始化
        self._initialized_dates.add(date)
        
        logger.info(f"【数据初始化】{date} 的数据初始化完成")
        return True
    
    def _load_portfolio(self, portfolio_file: str) -> Dict:
        """加载持仓信息
        
        Args:
            portfolio_file: 持仓文件路径
            
        Returns:
            包含 cash 和 positions 的字典
        """
        try:
            if not Path(portfolio_file).exists():
                return {'cash': 300000, 'positions': {}}
            with open(portfolio_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cash = data.get('cash', 300000)
                initial_capital = data.get('initial_capital', 300000)
                positions = data.get('positions', {})
                # 恢复可用资金和初始资金
                self.current_total_capital = cash
                self.initial_capital = initial_capital
                return {'cash': cash, 'initial_capital': initial_capital, 'positions': positions}
        except Exception as e:
            logger.warning(f"加载持仓文件失败: {str(e)}")
            return {'cash': 300000, 'positions': {}}
    
    def _save_portfolio(self, portfolio: Dict, portfolio_file: str):
        """保存持仓信息
        
        Args:
            portfolio: 持仓字典
            portfolio_file: 持仓文件路径
        """
        try:
            data = {
                'last_updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'cash': self.current_total_capital if hasattr(self, 'current_total_capital') else 300000,
                'initial_capital': self.initial_capital if hasattr(self, 'initial_capital') else 300000,
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
            if not Path(signals_file).exists():
                return []
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
    
    def _save_trade_record(self, trade_record: Dict):
        """保存交易记录
        
        Args:
            trade_record: 交易记录字典，包含：
                - trade_id: 交易ID
                - trade_date: 交易日期
                - signal_id: 关联的信号ID
                - stock_code: 股票代码
                - stock_name: 股票名称
                - trade_type: 交易类型(buy/sell)
                - quantity: 交易数量
                - price: 交易价格
                - amount: 交易金额
                - fee: 交易费用
                - total_cost: 总成本（买入）/净收入（卖出）
                - executed_time: 执行时间
                - strategy_name: 策略名称
        """
        try:
            # 交易记录文件按日期命名
            trade_date = trade_record.get('trade_date', self.get_working_date())
            trade_file = self.running_dir / f"trades_{trade_date}.json"
            
            # 读取现有记录
            trades = []
            if trade_file.exists():
                with open(trade_file, 'r', encoding='utf-8') as f:
                    trades = json.load(f)
            
            # 添加新记录
            trades.append(trade_record)
            
            # 保存
            with open(trade_file, 'w', encoding='utf-8') as f:
                json.dump(trades, f, ensure_ascii=False, indent=2)
            
            logger.info(f"交易记录已保存到: {trade_file}")
        except Exception as e:
            logger.error(f"保存交易记录失败: {str(e)}")
    
    def execute_signal(self, signal_id: str) -> Dict:
        """执行指定的信号（串行执行，确保原子操作）
        
        Args:
            signal_id: 信号ID
            
        Returns:
            执行结果字典
        """
        # 获取信号执行锁，确保串行执行
        _signal_execution_lock.acquire()
        try:
            logger.info(f"获取信号执行锁，开始执行信号: {signal_id}")
            
            # 先从内存中查找信号（前端已从文件加载所有信号到内存）
            signal = None
            for s in self.signals:
                if s.get('id') == signal_id:
                    signal = s
                    break
            
            # 记录调试信息
            if not signal:
                logger.debug(f"【调试】内存中未找到信号 {signal_id}，当前内存信号数量: {len(self.signals)}")
                if len(self.signals) > 0:
                    logger.debug(f"【调试】内存中的信号ID列表: {[s.get('id') for s in self.signals]}")
            
            # 如果内存中找不到，尝试从文件加载（Web界面执行时可能未初始化内存信号）
            if not signal:
                # 从信号ID中提取日期（格式: buy_688549_2026-05-06）
                signal_date = None
                parts = signal_id.split('_')
                if len(parts) >= 3:
                    # 最后一个部分应该是日期
                    date_part = parts[-1]
                    if len(date_part) == 10 and date_part.count('-') == 2:
                        signal_date = date_part
                
                # 如果从信号ID中提取不到日期，使用工作日期
                if not signal_date:
                    signal_date = self.get_working_date()
                
                logger.debug(f"【调试】尝试从文件加载信号，信号日期: {signal_date}")
                signals_file = self.running_dir / f"signals_{signal_date}.json"
                if signals_file.exists():
                    file_signals = self._load_signals(str(signals_file))
                    logger.info(f"从文件加载信号，当前信号数量: {len(file_signals)}")
                    for s in file_signals:
                        if s.get('id') == signal_id:
                            signal = s
                            # 添加到内存中以便后续查找
                            self.signals.append(s)
                            break
                else:
                    logger.debug(f"【调试】信号文件不存在: {signals_file}")
            
            if not signal:
                return {"success": False, "error": f"未找到信号: {signal_id}"}
            
            # 检查信号是否已执行
            if signal.get('executed'):
                return {"success": False, "error": f"信号已执行: {signal_id}"}
            
            # 立即标记信号为已执行，防止并发执行
            signal['executed'] = True
            signal['executed_date'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            signal_type = signal.get('signal_type', 'buy')
            stock_code = signal.get('stock_code')
            quantity = signal.get('quantity', 0)
            price = signal.get('price', 0)
            amount = signal.get('amount', quantity * price)
            
            # 计算交易费用（与回测一致）
            is_buy = (signal_type == 'buy')
            fee_result = calculate_trading_cost(stock_code, price, quantity, is_buy, self.config)
            total_cost = fee_result.get('total_cost', 0)
            
            # 获取当前可用资金（current_total_capital 直接表示可用资金）
            current_cash = self.current_total_capital if hasattr(self, 'current_total_capital') else 300000
            
            if signal_type == 'buy':
                # 计算实际需要支付的总金额（参考回测引擎逻辑）
                # adjusted_amount: 包含滑点的成交金额
                # commission: 佣金
                # transfer_fee: 过户费（买入时支付）
                # stamp_tax: 印花税（买入时不收取）
                adjusted_amount = fee_result.get('adjusted_amount', amount)
                commission = fee_result.get('commission', 0)
                transfer_fee = fee_result.get('transfer_fee', 0)
                total_deduct = adjusted_amount + commission + transfer_fee
                
                # 检查可用资金
                if current_cash < total_deduct:
                    return {"success": False, "error": f"可用资金不足: 需要¥{total_deduct:.2f}（金额¥{adjusted_amount:.2f} + 佣金¥{commission:.2f} + 过户费¥{transfer_fee:.2f}），可用¥{current_cash:.2f}"}
                
                # 执行买入
                if stock_code not in self.portfolio:
                    # 使用调整后的价格作为买入成本（包含滑点）
                    buy_price = fee_result.get('adjusted_price', price)
                    buy_fee = commission + transfer_fee  # 买入时不包含印花税
                    self.portfolio[stock_code] = {
                        'stock_name': signal.get('stock_name', ''),
                        'quantity': quantity,
                        'buy_price': buy_price,
                        'buy_date': signal.get('date', self.get_working_date()),
                        'current_price': price,
                        'buy_amount': adjusted_amount,
                        'buy_fee': buy_fee,
                        'total_cost': total_deduct,
                        'profit_loss': 0.0,
                        'profit_rate': 0.0,
                        'holding_days': 0,
                        'industry': '',
                        'sector': '',
                        'selection_score': signal.get('score', 0),
                        'support_level': signal.get('support_level', 0),
                        'strategy_name': signal.get('strategy_name', 'N/A')
                    }
                    # 扣减资金（买入金额 + 佣金 + 过户费）
                    if hasattr(self, 'current_total_capital'):
                        self.current_total_capital -= total_deduct
                    
                    # 保存交易记录
                    trade_record = {
                        'trade_id': f"trade_{signal_id}",
                        'trade_date': signal.get('date', self.get_working_date()),
                        'signal_id': signal_id,
                        'stock_code': stock_code,
                        'stock_name': signal.get('stock_name', ''),
                        'trade_type': 'buy',
                        'quantity': quantity,
                        'price': buy_price,
                        'amount': adjusted_amount,
                        'fee': buy_fee,
                        'total_cost': total_deduct,
                        'executed_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'strategy_name': signal.get('strategy_name', 'N/A')
                    }
                    self._save_trade_record(trade_record)
                    
                    logger.info(f"买入成功: {stock_code} x {quantity} @ ¥{buy_price:.2f}（佣金¥{commission:.2f} + 过户费¥{transfer_fee:.2f}），资金扣减¥{total_deduct:.2f}")
                else:
                    # 加仓：已有持仓，合并计算（参考回测引擎逻辑）
                    existing_pos = self.portfolio[stock_code]
                    old_quantity = existing_pos['quantity']
                    old_amount = existing_pos['buy_amount']
                    old_cost = existing_pos.get('total_cost', 0)
                    
                    # 使用调整后的价格作为买入成本（包含滑点）
                    buy_price = fee_result.get('adjusted_price', price)
                    buy_fee = commission + transfer_fee  # 买入时不包含印花税
                    
                    # 更新持仓数量和金额
                    existing_pos['quantity'] += quantity
                    existing_pos['buy_amount'] += adjusted_amount
                    existing_pos['total_cost'] = old_cost + total_deduct
                    # 加权平均买入价
                    existing_pos['buy_price'] = existing_pos['buy_amount'] / existing_pos['quantity']
                    # 更新最后加仓日期和价格
                    existing_pos['last_add_date'] = signal.get('date', self.get_working_date())
                    existing_pos['last_add_price'] = buy_price
                    # 更新加仓次数
                    existing_pos['add_count'] = existing_pos.get('add_count', 0) + 1
                    
                    # 扣减资金（买入金额 + 佣金 + 过户费）
                    if hasattr(self, 'current_total_capital'):
                        self.current_total_capital -= total_deduct
                    
                    # 保存交易记录
                    trade_record = {
                        'trade_id': f"trade_{signal_id}",
                        'trade_date': signal.get('date', self.get_working_date()),
                        'signal_id': signal_id,
                        'stock_code': stock_code,
                        'stock_name': existing_pos.get('stock_name', ''),
                        'trade_type': 'buy',
                        'quantity': quantity,
                        'price': buy_price,
                        'amount': adjusted_amount,
                        'fee': buy_fee,
                        'total_cost': total_deduct,
                        'executed_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'strategy_name': signal.get('strategy_name', 'N/A'),
                        'is_addition': True  # 标记为加仓
                    }
                    self._save_trade_record(trade_record)
                    
                    add_count = existing_pos['add_count']
                    logger.info(f"【加仓#{add_count}】{stock_code} x {quantity} @ ¥{buy_price:.2f}（佣金¥{commission:.2f} + 过户费¥{transfer_fee:.2f}），原数量={old_quantity}, 加仓={quantity}, 合计={existing_pos['quantity']}, 均价={existing_pos['buy_price']:.2f}")
            
            elif signal_type == 'sell':
                # 执行卖出
                if stock_code in self.portfolio:
                    # 计算卖出收入（考虑费用）
                    sell_price = fee_result.get('adjusted_price', price)
                    sell_amount = sell_price * quantity - total_cost
                    del self.portfolio[stock_code]
                    # 增加资金（卖出收入）- 直接增加到可用资金
                    if hasattr(self, 'current_total_capital'):
                        self.current_total_capital += sell_amount
                    logger.info(f"卖出成功: {stock_code} x {quantity} @ ¥{sell_price:.2f}（收入¥{sell_amount:.2f}，费用¥{total_cost:.2f}）")
                    
                    # 保存交易记录
                    trade_record = {
                        'trade_id': f"trade_{signal_id}",
                        'trade_date': signal.get('date', self.get_working_date()),
                        'signal_id': signal_id,
                        'stock_code': stock_code,
                        'stock_name': signal.get('stock_name', ''),
                        'trade_type': 'sell',
                        'quantity': quantity,
                        'price': sell_price,
                        'amount': sell_price * quantity,
                        'fee': total_cost,
                        'total_cost': sell_amount,
                        'executed_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'strategy_name': signal.get('strategy_name', 'N/A')
                    }
                    self._save_trade_record(trade_record)
                else:
                    return {"success": False, "error": f"未持有股票: {stock_code}"}
            
            # 保存更新后的信号和持仓
            working_date = self.get_working_date()
            signals_file = self.running_dir / f"signals_{working_date}.json"
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            self._save_signals(self.signals, str(signals_file))
            self._save_portfolio(self.portfolio, str(portfolio_file))
            
            return {"success": True, "message": f"{signal_type}信号执行成功", "fee_details": fee_result}
            
        except Exception as e:
            logger.error(f"执行信号失败: {str(e)}")
            return {"success": False, "error": str(e)}
        finally:
            # 释放信号执行锁
            _signal_execution_lock.release()
            logger.info(f"释放信号执行锁，信号执行完成: {signal_id}")
    
    def ignore_signal(self, signal_id: str) -> Dict:
        """忽略指定的信号
        
        Args:
            signal_id: 信号ID
            
        Returns:
            操作结果字典
        """
        try:
            logger.info(f"忽略信号: {signal_id}")
            
            # 查找信号
            signal = None
            for s in self.signals:
                if s.get('id') == signal_id:
                    signal = s
                    break
            
            if not signal:
                return {"success": False, "error": f"未找到信号: {signal_id}"}
            
            # 标记信号为已忽略
            signal['ignored'] = True
            signal['ignored_date'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # 保存更新后的信号
            working_date = self.get_working_date()
            signals_file = self.running_dir / f"signals_{working_date}.json"
            self._save_signals(self.signals, str(signals_file))
            
            return {"success": True, "message": "信号已忽略"}
            
        except Exception as e:
            logger.error(f"忽略信号失败: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def execute_pending_signals(self, trade_date: str = None) -> Dict:
        """执行所有待执行的信号（T+1日盘中调用）
        
        流程：
        1. 根据时间确定处理日期（15:30前处理T日，15:30后处理当日）
        2. 加载股票池、资金信息、持仓信息、交易信号
        3. 执行卖出信号
        4. 执行买入信号
        5. 保存持仓文件（重新计算资金）、交易文件、信号文件
        
        Args:
            trade_date: 交易日期，默认根据当前时间自动确定
            
        Returns:
            执行结果字典
        """
        try:
            if trade_date is None:
                # 根据当前时间确定处理日期
                now = datetime.datetime.now()
                today = now.strftime('%Y-%m-%d')
                
                # 判断是否是交易日
                if self._is_trading_day(today):
                    # 交易日：15:30之前处理T日（前一交易日）数据，15:30之后处理当日数据
                    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
                        # 15:30之前，处理前一交易日数据
                        trade_date = self._get_previous_trading_day(today)
                        logger.info(f"【T+1执行】当前时间 {now.strftime('%H:%M')} < 15:30，处理前一交易日数据: {trade_date}")
                    else:
                        # 15:30之后，处理当日数据
                        trade_date = today
                        logger.info(f"【T+1执行】当前时间 {now.strftime('%H:%M')} >= 15:30，处理当日数据: {trade_date}")
                else:
                    # 非交易日，使用最近的交易日
                    trade_date = self.get_working_date()
                    logger.info(f"【T+1执行】今日({today})非交易日，处理最近交易日数据: {trade_date}")
            else:
                logger.info(f"【T+1执行】使用指定日期: {trade_date}")
            
            logger.info(f"【T+1执行】开始执行待处理信号: {trade_date}")
            
            # 1. 加载数据
            portfolio_file = self.running_dir / f"portfolio_{trade_date}.json"
            signals_file = self.running_dir / f"signals_{trade_date}.json"
            
            # 加载持仓信息
            portfolio_data = self._load_portfolio(str(portfolio_file))
            self.portfolio = portfolio_data.get('positions', {})
            
            # 加载信号
            self.signals = self._load_signals(str(signals_file))
            
            # 加载股票池
            self.buy_candidate_pool = self._load_pool_from_file()[0]
            
            # 统计待执行的信号
            pending_buy_signals = [s for s in self.signals if s.get('signal_type') == 'buy' and not s.get('executed') and not s.get('ignored')]
            pending_sell_signals = [s for s in self.signals if s.get('signal_type') in ('sell', 'strategy_sell') and not s.get('executed') and not s.get('ignored')]
            
            logger.info(f"【T+1执行】待执行买入信号: {len(pending_buy_signals)}，待执行卖出信号: {len(pending_sell_signals)}")
            
            # 2. 先执行卖出信号
            executed_sells = 0
            for signal in pending_sell_signals:
                result = self.execute_signal(signal.get('id'))
                if result.get('success'):
                    executed_sells += 1
                    logger.info(f"【T+1执行】卖出成功: {signal.get('stock_code')}")
            
            # 3. 再执行买入信号
            executed_buys = 0
            for signal in pending_buy_signals:
                result = self.execute_signal(signal.get('id'))
                if result.get('success'):
                    executed_buys += 1
                    logger.info(f"【T+1执行】买入成功: {signal.get('stock_code')}")
            
            # 4. 保存最终结果
            self._save_signals(self.signals, str(signals_file))
            self._save_portfolio({'cash': self.current_total_capital, 'positions': self.portfolio}, str(portfolio_file))
            
            logger.info(f"【T+1执行】执行完成: 卖出 {executed_sells}，买入 {executed_buys}")
            
            return {
                "success": True,
                "message": f"信号执行完成",
                "data": {
                    "date": trade_date,
                    "executed_sells": executed_sells,
                    "executed_buys": executed_buys,
                    "final_cash": self.current_total_capital,
                    "position_count": len(self.portfolio)
                }
            }
            
        except Exception as e:
            logger.error(f"执行待处理信号失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}
    
    def _save_daily_record(self, date: str, record: Dict, records_file: str):
        """保存每日记录（仅生成 Markdown 报告，不保存 JSON）
        
        Args:
            date: 日期
            record: 每日记录
            records_file: 记录文件路径（已废弃，仅保留参数兼容性）
        """
        try:
            # 只生成文字版每日报告（Markdown格式），不保存 JSON 文件
            self._generate_daily_report(date, record)
        except Exception as e:
            logger.error(f"保存每日记录失败: {str(e)}")
    
    def _generate_daily_report(self, date: str, record: Dict):
        """生成文字版每日报告（Markdown格式）
        
        Args:
            date: 日期
            record: 每日记录数据
        """
        try:
            report_lines = []
            
            # 标题
            report_lines.append(f"# 策略执行日报 - {date}")
            report_lines.append("")
            
            # 执行概览
            report_lines.append("## 📊 执行概览")
            report_lines.append("")
            report_lines.append(f"- **执行日期**: {record.get('trading_date', date)}")
            report_lines.append(f"- **执行状态**: {'✅ 已完成' if record.get('status') == 'completed' else '❌ 未完成'}")
            report_lines.append("")
            
            # 股票池
            candidates = [s for s in record.get('pool_stocks', []) if s.get('status') == 'candidate']
            removed_count = record['pool_summary'].get('removed_count', 0)
            added_count = record['pool_summary'].get('added_count', 0)
            report_lines.append("## 📋 股票池")
            report_lines.append("")
            if candidates:
                report_lines.append(f"股票池合计 **{len(candidates)}** 只股票 今天移除 **{removed_count}** 只，新增加 **{added_count}** 只：")
                report_lines.append("")
                report_lines.append("| 股票代码 | 股票名称 | 评分 | 策略 | 支撑位 | 入池日期 |")
                report_lines.append("|----------|----------|------|------|--------|----------|")
                for stock in candidates:
                    report_lines.append(f"| {stock['code']} | {stock['name']} | {stock['score']} | {self._get_strategy_name(stock['strategy'])} | {stock['support_level']} | {stock.get('days', 1) > 1 and stock.get('added_date', '') or '今日'} |")
            else:
                report_lines.append("暂无候选股票")
            report_lines.append("")
            
            # 交易信号
            buy_signals = record.get('buy_signals', [])
            sell_signals = record.get('sell_signals', [])
            
            if buy_signals or sell_signals:
                report_lines.append("## 📈 交易信号")
                report_lines.append("")
                
                if buy_signals:
                    report_lines.append(f"### 买入信号 ({len(buy_signals)}个)")
                    report_lines.append("")
                    for signal in buy_signals:
                        status = "⏳ 待执行" if not signal.get('executed') else "✅ 已执行"
                        report_lines.append(f"- **{signal['stock_code']} {signal['stock_name']}**: {signal['reason']}")
                        report_lines.append(f"  - 价格: ¥{signal['price']} | 数量: {signal['quantity']}股 | 金额: ¥{signal['amount']:.2f}")
                        report_lines.append(f"  - 策略: {self._get_strategy_name(signal['strategy_name'])} | {status}")
                    report_lines.append("")
                
                if sell_signals:
                    report_lines.append(f"### 卖出信号 ({len(sell_signals)}个)")
                    report_lines.append("")
                    for signal in sell_signals:
                        status = "⏳ 待执行" if not signal.get('executed') else "✅ 已执行"
                        report_lines.append(f"- **{signal['stock_code']} {signal['stock_name']}**: {signal['reason']}")
                        report_lines.append(f"  - 价格: ¥{signal['price']} | 数量: {signal['quantity']}股 | 金额: ¥{signal['amount']:.2f}")
                        report_lines.append(f"  - 策略: {self._get_strategy_name(signal['strategy_name'])} | {status}")
                    report_lines.append("")
            else:
                report_lines.append("## 📈 交易信号")
                report_lines.append("")
                report_lines.append("今日无交易信号")
                report_lines.append("")
            
            # 持仓状态
            portfolio = record.get('portfolio', {})
            report_lines.append("## 📦 持仓状态")
            report_lines.append("")
            if portfolio:
                total_value = 0
                total_profit = 0
                report_lines.append(f"当前持有 **{len(portfolio)}** 只股票：")
                report_lines.append("")
                report_lines.append("| 股票代码 | 股票名称 | 持仓数量 | 成本价 | 现价 | 盈亏 | 持有天数 |")
                report_lines.append("|----------|----------|----------|--------|------|------|----------|")
                for code, pos in portfolio.items():
                    profit_rate = pos.get('profit_rate', 0)
                    profit_color = "green" if profit_rate >= 0 else "red"
                    profit_sign = "+" if profit_rate >= 0 else ""
                    report_lines.append(f"| {code} | {pos['stock_name']} | {pos['quantity']} | ¥{pos['buy_price']} | ¥{pos['current_price']} | <span style='color:{profit_color}'>{profit_sign}{pos['profit_loss']:.2f}</span> | {pos.get('holding_days', 0)} |")
                    total_value += pos['quantity'] * pos['current_price']
                    total_profit += pos.get('profit_loss', 0)
                report_lines.append("")
                report_lines.append(f"- **持仓总市值**: ¥{total_value:.2f}")
                report_lines.append(f"- **持仓总盈亏**: {'+' if total_profit >= 0 else ''}{total_profit:.2f}")
            else:
                report_lines.append("暂无持仓")
            report_lines.append("")
            
            # 策略执行结果
            task_results = record.get('task_results', [])
            if task_results:
                report_lines.append("## 📋 策略执行详情")
                report_lines.append("")
                for task in task_results:
                    report_lines.append(f"- **{self._get_strategy_name(task['selection_strategy'])}**")
                    report_lines.append(f"  - 选出: {task['selected_count']}只 | 新增: {task['new_added']}只 | 池内: {task['pool_count']}只")
                    report_lines.append(f"  - 状态: {'✅ 成功' if task['status'] == 'success' else '❌ 失败'}")
                report_lines.append("")
            
            # 生成时间
            report_lines.append(f"---")
            report_lines.append(f"*报告生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
            
            # 保存报告
            report_file = self.running_dir / f"daily_{date}.md"
            with open(report_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            logger.info(f"文字版日报已保存到: {report_file}")
            
        except Exception as e:
            logger.error(f"生成每日报告失败: {str(e)}")
    
    def _get_strategy_name(self, strategy_class_name: str) -> str:
        """将策略类名转换为中文名称"""
        name_mapping = {
            'ImmortalGuidanceStrategy': '仙人指路策略',
            'LimitUpSidewaysStrategy': '涨停横盘策略',
            'LimitUpPullbackStrategy': '涨停回马枪策略',
            'MACDReversalStrategy': 'MACD反转策略',
            'BullishHaramiStrategy': '多方炮策略',
            'BottomTrendReversalStrategy': '底部趋势拐点策略',
            'ResistanceBreakoutStrategy': '阻力位突破策略',
            'turtle': '海龟策略',
            'bollinger': '布林带策略',
            'rsi': 'RSI策略',
            'support': '支撑位策略'
        }
        return name_mapping.get(strategy_class_name, strategy_class_name)
    
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
            
            # 计算需要获取的天数
            from datetime import datetime
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            days = (end_dt - start_dt).days + 1
            
            # 从数据源获取
            df = self.stock_data_fetcher.fetch_stock_update(stock_code, days=days)
            if df is not None and not df.empty:
                # 按日期范围过滤
                df = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
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
    
    def _execute_sell_operations(self, trade_date: str) -> List[Dict]:
        """执行卖出操作
        
        Args:
            trade_date: 交易日期
            
        Returns:
            卖出信号列表
        """
        sell_signals = []
        
        try:
            # 记录当前资金和持仓情况
            positions_count = len(self.portfolio)
            available_cash = getattr(self, 'current_total_capital', 0)
            total_assets = available_cash
            
            # 计算持仓市值
            for stock_code, position in self.portfolio.items():
                total_assets += position.get('market_value', 0)
            
            logger.info(f"【卖出准备】{trade_date} 当前资金: ¥{available_cash:.2f}, 持仓: {positions_count}只")
            
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
                # 注意：缓存数据是倒序排列的（最新日期在前面）
                current_price = df.iloc[0]['close']
                open_price = df.iloc[0]['open']
                buy_price = position['buy_price']
                buy_date = position.get('buy_date', '')
                profit_rate = (current_price - buy_price) / buy_price
                
                # ========== 移动止损逻辑（与回测引擎保持一致） ==========
                # 获取移动止损配置
                enable_trailing_stop = self.config.get('enable_trailing_stop', True)
                trailing_base_stop = self.config.get('trailing_base_stop', -6)
                trailing_profit_levels = self.config.get('trailing_profit_levels', [
                    {'profit_threshold': 5, 'stop_level': 0},
                    {'profit_threshold': 10, 'stop_level': 3},
                    {'profit_threshold': 15, 'stop_level': 5},
                    {'profit_threshold': 20, 'stop_level': 8}
                ])
                
                # 计算当前止损线
                current_stop = self.stop_loss_threshold
                if enable_trailing_stop:
                    # 计算从买入日期到前一交易日的最高价
                    # 移动止损的最高价应该是买入日期至前一日的最高价，不包括当日最高价
                    current_highest_price = buy_price
                    
                    if buy_date and not df.empty:
                        buy_date_str = buy_date if isinstance(buy_date, str) else buy_date.strftime('%Y-%m-%d')
                        # 获取前一交易日（返回字符串类型）
                        prev_day_str = get_previous_trading_day(trade_date)
                        if prev_day_str:
                            # 筛选买入日期到前一交易日的数据
                            mask = (df['date'] >= buy_date_str) & (df['date'] <= prev_day_str)
                            filtered_df = df[mask]
                            if not filtered_df.empty:
                                current_highest_price = filtered_df['high'].max()
                    
                    # 计算最高价收益率
                    highest_price_return = (current_highest_price - buy_price) / buy_price * 100
                    
                    # 根据最高收益率计算移动止损线
                    current_stop_level = self.stop_loss_threshold * 100
                    for level in trailing_profit_levels:
                        if highest_price_return >= level['profit_threshold']:
                            current_stop_level = level['stop_level']
                    
                    current_stop = current_stop_level / 100
                    
                    logger.debug(f"  移动止损: 买入价={buy_price:.2f}, 最高价={current_highest_price:.2f}, 最高价收益率={highest_price_return:.2f}%, 止损线={current_stop*100:.2f}%")
                # ========== 移动止损逻辑结束 ==========
                
                # 记录择时信号详情
                stock_name = position['stock_name']
                
                # 生成卖出信号
                if timing_result.is_sell or profit_rate >= self.take_profit_threshold or profit_rate <= current_stop:
                    # 确定卖出原因
                    if timing_result.is_sell:
                        reason = timing_result.message
                        signal_type = 'strategy_sell'
                    elif profit_rate >= self.take_profit_threshold:
                        reason = f'止盈 (收益率: {profit_rate*100:.2f}%)'
                        signal_type = 'take_profit'
                    elif enable_trailing_stop and current_stop > self.stop_loss_threshold:
                        reason = f'移动止损 (收益率: {profit_rate*100:.2f}%, 止损线: {current_stop*100:.2f}%)'
                        signal_type = 'trailing_stop'
                    else:
                        reason = f'止损 (收益率: {profit_rate*100:.2f}%)'
                        signal_type = 'stop_loss'
                    
                    # 记录卖出决策
                    logger.info(f"【卖出信号】{trade_date} {stock_code} {stock_name} | "
                               f"持仓: {position['quantity']}股 | "
                               f"成本: ¥{buy_price:.2f} | 现价: ¥{current_price:.2f} | "
                               f"收益率: {profit_rate*100:.2f}% | "
                               f"信号类型: {signal_type} | 原因: {reason}")
                    
                    signal = {
                        'id': f"sell_{stock_code}_{trade_date}",
                        'date': trade_date,
                        'stock_code': stock_code,
                        'stock_name': stock_name,
                        'signal_type': signal_type,
                        'quantity': position['quantity'],
                        'price': current_price,
                        'amount': current_price * position['quantity'],
                        'profit_rate': profit_rate,
                        'reason': reason,
                        'strategy_name': position.get('strategy_name', 'N/A'),
                        'timing_strategy': self.timing_strategy_name,
                        'executed': False,
                        'executed_date': None
                    }
                    sell_signals.append(signal)
                    stocks_to_remove.append(stock_code)
                else:
                    # 没有卖出信号，记录日志
                    # 计算止损价格（参考回测引擎逻辑）
                    stop_price = buy_price * (1 + current_stop)
                    logger.info(f"【无卖出信号】{trade_date} {stock_code} {stock_name} | "
                               f"持仓: {position['quantity']}股 | "
                               f"成本: ¥{buy_price:.2f} | 现价: ¥{current_price:.2f} | "
                               f"收益率: {profit_rate*100:.2f}% | "
                               f"止损价: ¥{stop_price:.2f} | "
                               f"择时卖出: {timing_result.is_sell} | 止盈未触发 | 止损未触发")
            
            # 执行卖出操作
            for stock_code in stocks_to_remove:
                del self.portfolio[stock_code]
            
            # ========== 新增：卖出后更新冷却池和连续亏损计数 ==========
            for sell_signal in sell_signals:
                stock_code = sell_signal['stock_code']
                profit_rate = sell_signal['profit_rate'] if 'profit_rate' in sell_signal else 0
                
                # 获取配置
                enable_loss_cool_down = self.config.get('enable_loss_cool_down', True)
                enable_consecutive_loss_limit = self.config.get('enable_consecutive_loss_limit', True)
                cool_down_threshold = self.config.get('cool_down_threshold', -8)
                cool_down_days = self.config.get('cool_down_days', 20)
                max_consecutive_losses = self.config.get('max_consecutive_losses', 2)
                consecutive_loss_cool_down = self.config.get('consecutive_loss_cool_down', 30)
                
                # 更新连续亏损计数
                if enable_consecutive_loss_limit:
                    if profit_rate > 0:
                        # 盈利，重置计数
                        self.consecutive_loss_count[stock_code] = 0
                    else:
                        # 亏损，增加计数
                        current_count = self.consecutive_loss_count.get(stock_code, 0) + 1
                        self.consecutive_loss_count[stock_code] = current_count
                        
                        # 检查是否达到连续亏损限制
                        if current_count >= max_consecutive_losses:
                            cool_down_end = self._get_future_trading_day(trade_date, consecutive_loss_cool_down)
                            # 更新股票池中的冷却状态
                            self._update_stock_cool_down_status(stock_code, True, cool_down_end)
                            logger.warning(f"  股票 {stock_code} 连续亏损 {current_count} 次，加入冷却池至 {cool_down_end}")
                
                # 检查是否触发亏损冷却期（单笔亏损超阈值）
                # 两个条件独立判断：单笔亏损超8% 或 连续两次亏损
                if enable_loss_cool_down and profit_rate * 100 <= cool_down_threshold:
                    # 检查是否已经在冷却期，避免重复记录
                    if not self._is_stock_cooling(stock_code):
                        cool_down_end = self._get_future_trading_day(trade_date, cool_down_days)
                        # 更新股票池中的冷却状态
                        self._update_stock_cool_down_status(stock_code, True, cool_down_end)
                        logger.warning(f"  股票 {stock_code} 单笔亏损 {profit_rate*100:.2f}% 超过阈值 {cool_down_threshold}%，加入冷却池至 {cool_down_end}")
            # ========== 冷却池和连续亏损计数更新结束 ==========
            
            logger.info(f"【卖出汇总】{trade_date} 执行卖出操作，生成 {len(sell_signals)} 个卖出信号，共检查 {len(self.portfolio) + len(stocks_to_remove)} 只持仓")
        except Exception as e:
            logger.error(f"执行卖出操作失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
        
        return sell_signals
    
    def _execute_buy_operations(self, trade_date: str, initial_cash: float, check_capital: bool = False) -> List[Dict]:
        """执行买入操作
        
        Args:
            trade_date: 交易日期
            initial_cash: 初始资金
            check_capital: 是否检查资金限制，默认False（T日生成信号时不限制）
            
        Returns:
            买入信号列表
        """
        buy_signals = []
        
        try:
            # 获取当前可用资金（从配置文件加载的初始资金，或从持仓文件恢复的资金）
            if not hasattr(self, 'current_total_capital'):
                self.current_total_capital = initial_cash
            
            # current_total_capital 直接表示可用资金，不需要重置
            current_cash = self.current_total_capital
            
            # T日生成信号模式：不限制资金
            if not check_capital:
                logger.info(f"【买入检查】{trade_date} T日信号生成模式 | 可用资金: ¥{current_cash:.2f} | "
                           f"持仓: {len(self.portfolio)} | 候选股票: {len(self.buy_candidate_pool)}")
            else:
                # T+1日执行模式：检查资金
                if current_cash < 2000:
                    logger.info(f"【买入检查】{trade_date} 可用资金不足2000元 (¥{current_cash:.2f})，跳过买入操作")
                    return buy_signals
                if current_cash <= 0:
                    logger.info(f"【买入检查】{trade_date} 可用资金为负 (¥{current_cash:.2f})，跳过买入操作")
                    return buy_signals
                logger.info(f"【买入检查】{trade_date} T+1日执行模式 | 可用资金: ¥{current_cash:.2f} | "
                           f"持仓: {len(self.portfolio)} | 候选股票: {len(self.buy_candidate_pool)}")
            
            # 遍历可买股票池
            for candidate in self.buy_candidate_pool:
                
                # 适配新的股票池结构
                stock_info = candidate.get('stock', candidate)
                stock_code = stock_info['stock_code']
                stock_name = stock_info['stock_name']
                score = stock_info.get('score', 0)
                
                # ========== 检查冷却期 ==========
                # 加入冷却池的条件（卖出执行时处理）：
                # 1. 单次亏损超过8%
                # 2. 连续两次亏损
                # 连续亏损达到限制后会自动加入冷却池，因此只需要检查冷却池即可
                enable_loss_cool_down = self.config.get('enable_loss_cool_down', True)
                enable_consecutive_loss_limit = self.config.get('enable_consecutive_loss_limit', True)
                
                if enable_loss_cool_down or enable_consecutive_loss_limit:
                    if self._check_cool_down(stock_code, trade_date):
                        cool_down_end = self.loss_cool_down_pool.get(stock_code, 'N/A')
                        logger.info(f"【买入检查】{trade_date} {stock_code} 在冷却期内（至 {cool_down_end}），跳过")
                        continue
                # ========== 冷却期检查结束 ==========
                
                # 获取股票数据（优先从缓存获取）
                df = self.stock_filtered_cache.get(stock_code)
                if df is None:
                    logger.debug(f"【买入检查】{trade_date} {stock_code} {stock_name} 获取股票数据失败，跳过")
                    continue
                
                # 日期切片
                df_to_date = df[df['date'] <= trade_date].copy()
                if df_to_date.empty:
                    logger.debug(f"【买入检查】{trade_date} {stock_code} {stock_name} 无有效数据，跳过")
                    continue
                
                # 反转为倒序
                if len(df_to_date) > 1 and df_to_date['date'].iloc[0] < df_to_date['date'].iloc[-1]:
                    df_to_date = df_to_date.iloc[::-1].reset_index(drop=True)
                
                # ========== 新增：检查20日涨幅是否超过50% ==========
                if len(df_to_date) >= 20:
                    # 获取20日前的收盘价（倒序，iloc[19]是20日前的数据）
                    price_20d_ago = df_to_date.iloc[19]['close']
                    current_price = df_to_date.iloc[0]['close']
                    if price_20d_ago > 0:
                        gain_20d = (current_price - price_20d_ago) / price_20d_ago
                        if gain_20d > 0.5:  # 涨幅超过50%
                            logger.info(f"【买入检查】{trade_date} {stock_code} {stock_name} 20日涨幅 {gain_20d*100:.1f}% > 50%，跳过")
                            continue
                # ========== 20日涨幅检查结束 ==========
                
                # 检查是否已在持仓中
                existing_pos = self.portfolio.get(stock_code)
                
                # 调用择时策略判断
                timing_result = self.timing_strategy.get_timing_result(df_to_date, existing_pos, current_cash, use_prev_day_signal=False)
                
                # 记录择时信号详情
                current_price = df_to_date.iloc[0]['close']  # 倒序数据，iloc[0]是最新数据
                logger.info(f"【择时信号】{trade_date} {stock_code} {stock_name} | "
                           f"评分: {score:.1f} | 现价: ¥{current_price:.2f} | "
                           f"支撑位: ¥{candidate.get('support_level', 0):.2f} | "
                           f"买入信号: {timing_result.is_buy} | "
                           f"加仓信号: {timing_result.trade_type == 'add'} | "
                           f"信号强度: {timing_result.signal_strength:.2f} | "
                           f"信息: {timing_result.message}")
                
                # 生成买入信号
                if timing_result.is_buy:
                    # 根据交易类型决定买入数量计算方式
                    if timing_result.trade_type == 'add':
                        # 加仓：使用策略返回的买入数量
                        buy_quantity = timing_result.buy_quantity
                        quantity_source = '择时策略（加仓）'
                    else:
                        # 首次建仓：使用凯莉公式计算
                        strategy_name = candidate.get('strategy_name', 'N/A')
                        
                        # 使用初始化时计算的总资产
                        total_assets = getattr(self, 'current_total_assets', current_cash)
                        
                        position_amount = KellyCalculator.calculate_position_amount(
                            total_capital=total_assets,
                            available_cash=current_cash,
                            strategy_name=strategy_name
                        )
                        buy_quantity = KellyCalculator.calculate_buy_quantity(
                            position_amount=position_amount,
                            price=current_price
                        )
                        quantity_source = '凯莉公式'
                    
                    if buy_quantity <= 0:
                        logger.debug(f"【买入检查】{trade_date} {stock_code} {stock_name} 买入数量不足100股，跳过")
                        continue
                    
                    # 记录买入决策
                    logger.info(f"【买入决策】{trade_date} {stock_code} {stock_name} | "
                               f"数量: {buy_quantity}股 ({quantity_source}) | 价格: ¥{current_price:.2f} | "
                               f"金额: ¥{current_price * buy_quantity:.2f} | "
                               f"信号: {timing_result.message}")
                    
                    signal = {
                        'id': f"buy_{stock_code}_{trade_date}",
                        'date': trade_date,
                        'stock_code': stock_code,
                        'stock_name': stock_name,
                        'signal_type': 'buy',
                        'quantity': buy_quantity,
                        'price': current_price,
                        'amount': current_price * buy_quantity,
                        'reason': timing_result.message,
                        'strategy_name': candidate.get('strategy_name', 'N/A'),
                        'timing_strategy': self.timing_strategy_name,
                        'support_level': candidate.get('support_level', 0),
                        'executed': False,
                        'executed_date': None
                    }
                    buy_signals.append(signal)
                    
                    # T+1日执行模式：更新可用资金
                    if check_capital:
                        current_cash -= current_price * buy_quantity
                
                # 记录加仓信号
                elif timing_result.trade_type == 'add' and existing_pos:
                    add_quantity = timing_result.buy_quantity if timing_result.buy_quantity > 0 else 100
                    logger.info(f"【加仓信号】{trade_date} {stock_code} {stock_name} | "
                               f"加仓数量: {add_quantity}股 | 价格: ¥{current_price:.2f} | "
                               f"金额: ¥{current_price * add_quantity:.2f} | "
                               f"加仓次数: {timing_result.add_count} | "
                               f"信号: {timing_result.message}")
            
            logger.info(f"【买入汇总】{trade_date} 执行买入操作，生成 {len(buy_signals)} 个买入信号")
        except Exception as e:
            logger.error(f"执行买入操作失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
        
        return buy_signals
    
    def run_strategy(self, strategy_names: List[str], timing_strategy_name: str, config: Dict) -> Dict:
        """运行策略
        
        与回测引擎保持一致：
        1. 首次运行：从数据库预加载全市场股票 → 执行选股 → 评分 → 初始化股票池
        2. 后续运行：加载持久化股票池 → 检查移除条件 → 继续选股加入新股票
        
        Args:
            strategy_names: 选股策略列表
            timing_strategy_name: 择时策略名称
            config: 策略运行配置参数（可选，如果不提供则从回测配置获取）
            
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

            # 记录传入的config
            logger.info(f"run_strategy 传入的config: {config}")
            logger.info(f"run_strategy 传入的selection_date: {config.get('selection_date')}")

            # 如果 config 中没有提供参数，则从回测配置获取
            if not config.get('initial_capital') or not config.get('max_daily_buys') or not config.get('score_threshold'):
                backtest_config = self._get_backtest_config()
                if backtest_config:
                    logger.info(f"从回测配置获取参数: {backtest_config}")
                    config = {**backtest_config, **config}  # config 中的值优先
                    logger.info(f"合并后的config: {config}")
            
            # 获取配置参数（与回测引擎保持一致）
            initial_capital = config.get('initial_capital', 300000)
            max_daily_buys = config.get('max_daily_buys', 8)  # 使用 max_daily_buys，与回测一致
            score_threshold = config.get('score_threshold', 60)
            
            # 将参数存入 config，确保后续代码可以使用
            config['initial_capital'] = initial_capital
            config['max_daily_buys'] = max_daily_buys
            config['score_threshold'] = score_threshold

            # 确定工作日期（优先使用config中的selection_date）
            if config.get('selection_date'):
                working_date = config['selection_date']
                logger.info(f"使用配置的选股日期: {working_date}")
            else:
                # 根据当前时间确定处理日期
                now = datetime.datetime.now()
                today = now.strftime('%Y-%m-%d')
                
                # 判断是否是交易日（使用全局函数）
                if is_trading_day(today):
                    # 交易日：15:30之前处理T-1日（前一交易日）数据，15:30之后处理当日数据
                    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
                        # 15:30之前，处理前一交易日数据
                        working_date = get_previous_trading_day(today)
                        logger.info(f"【信号生成】当前时间 {now.strftime('%H:%M')} < 15:30，处理前一交易日数据: {working_date}")
                    else:
                        # 15:30之后，处理当日数据
                        working_date = today
                        logger.info(f"【信号生成】当前时间 {now.strftime('%H:%M')} >= 15:30，处理当日数据: {working_date}")
                else:
                    # 非交易日，使用最近的交易日
                    working_date = self.get_working_date()
                    logger.info(f"【信号生成】今日({today})非交易日，处理最近交易日数据: {working_date}")
            
            # ========== 自动初始化当日数据 ==========
            # 在执行策略前，先确保当日数据已初始化（从前一交易日继承持仓）
            self.initialize_daily_data(working_date)
            
            # 检查是否已处理（日期层面的检查，避免重复执行）
            if self.check_if_processed(working_date):
                logger.info(f"日期 {working_date} 已处理，直接返回结果")
                _strategy_run_lock.release()
                return {"status": "success", "message": "日期已处理", "data": {"date": working_date}}
            
            # 检查是否已处理（策略层面的检查）
            # 注意：不同策略组合需要分别执行，不应根据 daily 文件判断
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            signals_file = self.running_dir / f"signals_{working_date}.json"
            if portfolio_file.exists() and signals_file.exists():
                # 检查信号文件是否包含当前策略的结果
                try:
                    with open(signals_file, 'r', encoding='utf-8') as f:
                        signals_data = json.load(f)
                    # 检查信号列表中是否有当前策略的信号
                    current_strategies = set(strategy_names)
                    existing_strategies = set()
                    signals_list = signals_data if isinstance(signals_data, list) else signals_data.get('signals', [])
                    for sig in signals_list:
                        if isinstance(sig, dict) and sig.get('strategy_name'):
                            existing_strategies.add(sig['strategy_name'])
                    if current_strategies.issubset(existing_strategies):
                        logger.info(f"策略 {strategy_names} 在 {working_date} 已执行，跳过")
                        return {"status": "success", "message": "策略已执行", "data": {"date": working_date}}
                except Exception:
                    pass  # 文件可能为空或不完整，继续执行
            
            # 加载持仓信息
            portfolio_data = self._load_portfolio(str(portfolio_file))
            self.portfolio = portfolio_data.get('positions', {})
            
            # 加载信号历史
            signals_file = self.running_dir / f"signals_{working_date}.json"
            self.signals = self._load_signals(str(signals_file))
            
            # 初始化择时策略
            timing_params = config.get('timing_params', {})
            strategy_params = timing_params.get(timing_strategy_name, {})
            
            # 特殊处理：如果是海龟策略
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
            
            self.timing_strategy = TimingStrategyFactory.create_strategy(
                timing_strategy_name, strategy_params
            )
            self.timing_strategy_name = timing_strategy_name
            self.timing_strategy_params = strategy_params
            
            logger.info(f"初始化择时策略: {timing_strategy_name}")
            
            # ========== 股票池初始化逻辑 ==========
            
            # 1. 尝试从持久化文件加载股票池
            loaded_pool, is_first_run = self._load_pool_from_file()
            
            # 预加载股票数据（无论是否首次运行，缓存为空时都需要预加载）
            if not self.stock_filtered_cache:
                strategy_name = strategy_names[0] if strategy_names else 'default'
                logger.info(f"股票数据缓存为空，开始预加载...")
                self._preload_stock_data(working_date, strategy_name)
            
            if is_first_run:
                # 首次运行：使用预加载机制初始化股票池（与回测引擎一致）
                logger.info("首次运行，使用预加载机制初始化股票池...")
                strategy_name = strategy_names[0] if strategy_names else 'ImmortalGuidanceStrategy'
                self._execute_stock_pool_preload(strategy_name, config)
                logger.info(f"首次运行初始化股票池: {len(self.buy_candidate_pool)} 只股票")
            else:
                # 后续运行：使用已加载的股票池
                self.buy_candidate_pool = loaded_pool
                logger.info(f"从持久化文件加载股票池: {len(self.buy_candidate_pool)} 只股票")
            
            # 2. 检查股票池移除条件（破支撑位、趋势验证）
            logger.info(f"【股票池移除检查】开始检查股票池移除条件，当前股票池数量: {len(self.buy_candidate_pool)}")
            removed = self._check_pool_removal(working_date)
            if removed:
                logger.info(f"【股票池移除完成】移除 {len(removed)} 只股票，剩余: {len(self.buy_candidate_pool)} 只")
            else:
                logger.info(f"【股票池移除完成】未移除任何股票，股票池数量保持: {len(self.buy_candidate_pool)} 只")
            
            # 3. 继续选股，加入新股票（仅非首次运行时执行，首次运行已在初始化阶段完成）
            if not is_first_run:
                selection_date_str = working_date
                
                logger.info(f"执行选股日期: {selection_date_str}")
                
                for strategy_name in strategy_names:
                    # 执行选股和评分
                    new_candidates = self._select_and_score_stocks(strategy_name, selection_date_str, score_threshold)
                    
                    # 将新选出的股票加入股票池
                    for stock in new_candidates:
                        # 检查是否已在池中
                        if not any(item['stock']['stock_code'] == stock['stock_code'] for item in self.buy_candidate_pool):
                            # 计算支撑位
                            support_level = self._calculate_support_level(stock, strategy_name, selection_date_str)
                            support_method = self._get_support_method_for_strategy(strategy_name)
                            
                            # 提取关键日（从策略信号中获取，默认为选入日期）
                            key_date = stock.get('signal', {}).get('key_date')
                            if key_date:
                                if hasattr(key_date, 'strftime'):
                                    key_date = key_date.strftime('%Y-%m-%d')
                                key_date = str(key_date)
                            else:
                                key_date = selection_date_str

                            self.buy_candidate_pool.append({
                                'stock': stock,
                                'added_date': selection_date_str,
                                'key_date': key_date,                      # 关键日（形态实际形成日期）
                                'strategy_name': strategy_name,
                                'support_level': support_level,
                                'support_method': support_method
                            })
            
            logger.info(f"选股后股票池数量: {len(self.buy_candidate_pool)}")
            
            # 4. 保存股票池到持久化文件
            self._save_pool_to_file(self.buy_candidate_pool, working_date)
            
            # ========== T日盘后：生成交易信号 ==========
            
            # 5. 卖出操作（生成卖出信号）
            sell_signals = self._execute_sell_operations(working_date)
            
            # 6. 买入操作（生成买入信号，不限制资金和次数）
            buy_signals = self._execute_buy_operations(working_date, initial_capital, check_capital=False)
            
            # 7. 保存信号文件
            daily_record = {
                "date": working_date,
                "trading_date": working_date,
                "status": "completed",
                "is_first_run": is_first_run,
                "pool_summary": {
                    "stock_count": len(self.buy_candidate_pool),  # 只统计候选股票数量
                    "removed_count": removed_count if 'removed_count' in locals() else 0,
                    "added_count": added_count if 'added_count' in locals() else 0
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
                        "status": "candidate"
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
            
            # 8. 保存当日记录
            records_file = self.running_dir / f"daily_{working_date}.json"
            self._save_daily_record(working_date, daily_record, str(records_file))
            
            # 9. 保存信号文件
            signals = sell_signals + buy_signals
            self.signals.extend(signals)
            self._save_signals(self.signals, str(signals_file))
            
            logger.info(f"T日信号生成完成: {working_date}")
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
    
        except Exception as e:
            logger.error(f"策略运行失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "failed", "message": str(e)}
        finally:
            _strategy_run_lock.release()
            logger.info("释放策略运行锁")
    
    def run_strategies_batch(self, tasks: List[Dict], config: Dict) -> Dict:
        """批量运行策略（所有策略执行完成后统一保存文件）
        
        Args:
            tasks: 任务列表，每个任务包含：
                - selection_strategy: 选股策略名称
                - timing_strategy: 择时策略名称
            config: 运行配置参数
            
        Returns:
            批量执行结果
        """
        # 检查是否正在执行（防止并发）
        if StrategyRunner._is_running:
            logger.warning("策略正在执行中，拒绝重复请求")
            return {
                "status": "failed",
                "message": "策略正在执行中，请等待当前任务完成"
            }
        
        StrategyRunner._is_running = True
        
        # 获取策略运行锁
        if not _strategy_run_lock.acquire(blocking=False):
            logger.warning("策略运行任务正在执行中，等待...")
            _strategy_run_lock.acquire(blocking=True)
            logger.info("获取策略运行锁，开始执行批量任务")
        
        try:
            logger.info(f"开始批量执行 {len(tasks)} 个策略任务")
            
            # 清空上次的缓存数据（只清空一次）
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            self.stock_filtered_cache.clear()
            self.buy_candidate_pool = []
            
            # 获取配置参数
            initial_capital = config.get('initial_capital', 300000)
            max_daily_buys = config.get('max_daily_buys', 8)
            score_threshold = config.get('score_threshold', 60)
            config['initial_capital'] = initial_capital
            config['max_daily_buys'] = max_daily_buys
            config['score_threshold'] = score_threshold
            
            # 确定工作日期
            working_date = self.get_working_date()
            logger.info(f"工作日期: {working_date}")
            
            # 检查是否已处理
            if self.check_if_processed(working_date):
                logger.info(f"日期 {working_date} 已处理，直接返回结果")
                StrategyRunner._is_running = False
                _strategy_run_lock.release()
                return {"status": "success", "message": "日期已处理", "data": {"date": working_date}}
            
            # 加载持仓信息
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            portfolio_data = self._load_portfolio(str(portfolio_file))
            self.portfolio = portfolio_data.get('positions', {})
            
            # 加载信号历史
            signals_file = self.running_dir / f"signals_{working_date}.json"
            self.signals = self._load_signals(str(signals_file))
            
            # 记录每个任务的执行结果
            task_results = []
            
            # 首次运行标记
            is_first_run = False
            
            # 预加载数据（首次执行）
            first_task = tasks[0]
            first_strategy = first_task.get('selection_strategy', first_task.get('strategy_names', ['ImmortalGuidanceStrategy']))
            if isinstance(first_strategy, list):
                first_strategy = first_strategy[0] if first_strategy else 'ImmortalGuidanceStrategy'
            
            # 加载股票池（首次运行时初始化）
            loaded_pool, need_reexecute = self._load_pool_from_file(working_date)
            if need_reexecute:
                logger.info("需要重新执行，初始化股票池...")
                is_first_run = True
            
            # 预加载股票数据
            if not self.stock_filtered_cache:
                logger.info(f"股票数据缓存为空，开始预加载...")
                self._preload_stock_data(working_date, first_strategy)
            
            if need_reexecute:
                logger.info("需要重新执行，使用预加载机制初始化股票池...")
                self._execute_stock_pool_preload(first_strategy, config)
                logger.info(f"初始化股票池: {len(self.buy_candidate_pool)} 只股票")
            else:
                self.buy_candidate_pool = loaded_pool
                logger.info(f"从持久化文件加载股票池: {len(self.buy_candidate_pool)} 只股票")
            
            # 检查股票池移除条件
            logger.info(f"开始检查股票池移除条件，当前股票池数量: {len(self.buy_candidate_pool)}")
            removed = self._check_pool_removal(working_date)
            if removed:
                logger.info(f"股票池移除 {len(removed)} 只股票，剩余: {len(self.buy_candidate_pool)} 只")
            
            # 顺序执行每个策略任务
            for idx, task in enumerate(tasks, 1):
                selection_strategy = task.get('selection_strategy', task.get('strategy_names', ['ImmortalGuidanceStrategy']))
                if isinstance(selection_strategy, list):
                    selection_strategy = selection_strategy[0] if selection_strategy else 'ImmortalGuidanceStrategy'
                timing_strategy = task.get('timing_strategy', 'support')
                
                logger.info(f"执行任务 {idx}/{len(tasks)}: 选股={selection_strategy}, 择时={timing_strategy}")
                
                try:
                    # 初始化择时策略
                    timing_params = config.get('timing_params', {})
                    strategy_params = timing_params.get(timing_strategy, {})
                    
                    # 特殊处理：如果是海龟策略
                    if timing_strategy == 'turtle':
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
                    
                    self.timing_strategy = TimingStrategyFactory.create_strategy(
                        timing_strategy, strategy_params
                    )
                    self.timing_strategy_name = timing_strategy
                    self.timing_strategy_params = strategy_params
                    
                    # 执行选股
                    candidate_stocks = self._select_and_score_stocks(selection_strategy, working_date, score_threshold)
                    
                    # 将选出的股票加入股票池
                    new_added = 0
                    for stock in candidate_stocks:
                        exists = any(item['stock']['stock_code'] == stock['stock_code'] for item in self.buy_candidate_pool)
                        if not exists:
                            support_level = self._calculate_support_level(stock, selection_strategy, working_date)
                            support_method = self._get_support_method_for_strategy(selection_strategy)
                            
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
                                'strategy_name': selection_strategy,
                                'support_level': support_level,
                                'support_method': support_method
                            })
                            new_added += 1
                    
                    task_results.append({
                        'selection_strategy': selection_strategy,
                        'timing_strategy': timing_strategy,
                        'selected_count': len(candidate_stocks),
                        'new_added': new_added,
                        'pool_count': len(self.buy_candidate_pool),
                        'status': 'success'
                    })
                    logger.info(f"{selection_strategy} 选出 {len(candidate_stocks)} 只股票，新增 {new_added} 只")
                    
                except Exception as e:
                    logger.error(f"任务执行失败: {selection_strategy} - {str(e)}")
                    task_results.append({
                        'selection_strategy': selection_strategy,
                        'timing_strategy': timing_strategy,
                        'status': 'failed',
                        'error': str(e)
                    })
            
            logger.info(f"选股完成，股票池数量: {len(self.buy_candidate_pool)}")
            
            # ========== 所有策略执行完成后，统一保存文件 ==========
            
            # 保存股票池
            self._save_pool_to_file(self.buy_candidate_pool, working_date)
            
            # 执行卖出操作
            sell_signals = self._execute_sell_operations(working_date)
            
            # 执行买入操作
            buy_signals = self._execute_buy_operations(working_date, initial_capital)
            
            # 构建当日记录
            signals = sell_signals + buy_signals
            daily_record = {
                "date": working_date,
                "trading_date": working_date,
                "status": "completed",
                "is_first_run": is_first_run,
                "pool_summary": {
                    "stock_count": len(self.buy_candidate_pool),  # 只统计候选股票数量
                    "removed_count": removed_count if 'removed_count' in locals() else 0,
                    "added_count": added_count if 'added_count' in locals() else 0
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
                        "status": "candidate"
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
                "portfolio": self.portfolio,
                "task_results": task_results
            }
            
            # 保存所有文件
            records_file = self.running_dir / f"daily_{working_date}.json"
            self._save_daily_record(working_date, daily_record, str(records_file))
            logger.info(f"每日记录已保存到: {records_file}")
            
            self.signals.extend(signals)
            self._save_signals(self.signals, str(signals_file))
            logger.info(f"信号已保存到: {signals_file}")
            
            self._save_portfolio(self.portfolio, str(portfolio_file))
            logger.info(f"持仓信息已保存到: {portfolio_file}")
            
            logger.info(f"批量策略执行完成")
            return {
                "status": "success",
                "message": "批量策略运行完成",
                "data": {
                    "run_date": working_date,
                    "is_first_run": is_first_run,
                    "pool_count": len(self.buy_candidate_pool),
                    "total_signals": len(signals),
                    "buy_signals": len(buy_signals),
                    "sell_signals": len(sell_signals),
                    "position_count": len(self.portfolio),
                    "task_results": task_results
                }
            }
            
        except Exception as e:
            logger.error(f"批量运行策略失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "failed", "message": str(e)}
        finally:
            _strategy_run_lock.release()
            StrategyRunner._is_running = False
            logger.info("释放策略运行锁")
    
    def run_plan(self, plan: ExecutionPlan, config: Dict) -> Dict:
        """运行执行方案（支持多组合策略顺序执行）
        
        根据执行方案中定义的策略组合顺序，依次执行每个组合
        每个组合包含一个选股策略和一个择时策略
        
        Args:
            plan: 执行方案对象
            config: 运行配置参数
            
        Returns:
            策略运行结果字典，包含每个组合的执行结果
        """
        # 获取策略运行锁
        if not _strategy_run_lock.acquire(blocking=False):
            logger.warning("策略运行任务正在执行中，等待...")
            _strategy_run_lock.acquire(blocking=True)
            logger.info("获取策略运行锁，开始执行方案")
        
        try:
            logger.info(f"开始执行方案: {plan.name} (ID: {plan.id})")
            
            # 验证方案
            if not plan.validate():
                return {"status": "failed", "message": "执行方案验证失败"}
            
            # 获取启用的组合列表
            enabled_combinations = plan.get_enabled_combinations()
            if not enabled_combinations:
                return {"status": "failed", "message": "方案中没有启用的策略组合"}
            
            logger.info(f"方案包含 {len(enabled_combinations)} 个启用的策略组合")
            
            # 清空上次的缓存数据
            self.stock_data_cache.clear()
            self.stock_name_cache.clear()
            self.stock_filtered_cache.clear()
            self.buy_candidate_pool = []
            
            # 获取配置参数
            initial_cash = config.get('initial_cash', 300000)
            score_threshold = config.get('score_threshold', 60)
            
            # 确定工作日期
            working_date = self.get_working_date()
            logger.info(f"工作日期: {working_date}")
            
            # 检查是否已处理
            if self.check_if_processed(working_date):
                logger.info(f"日期 {working_date} 已处理，直接返回结果")
                return {"status": "success", "message": "日期已处理", "data": {"date": working_date}}
            
            # 加载持仓信息
            portfolio_file = self.running_dir / f"portfolio_{working_date}.json"
            portfolio_data = self._load_portfolio(str(portfolio_file))
            self.portfolio = portfolio_data.get('positions', {})
            
            # 加载信号历史
            signals_file = self.running_dir / f"signals_{working_date}.json"
            self.signals = self._load_signals(str(signals_file))
            
            # 记录每个组合的执行结果
            combination_results = []
            
            # 顺序执行每个策略组合
            for idx, combination in enumerate(enabled_combinations, 1):
                selection_strategy = combination.selection_strategy
                timing_strategy_name = combination.timing_strategy
                
                logger.info(f"执行组合 {idx}/{len(enabled_combinations)}: 选股={selection_strategy}, 择时={timing_strategy_name}")
                
                # 预加载股票数据（首次组合时执行）
                if idx == 1 and not self.stock_filtered_cache:
                    logger.info(f"股票数据缓存为空，开始预加载...")
                    self._preload_stock_data(working_date, selection_strategy)
                
                # 尝试从持久化文件加载股票池（仅首次组合）
                if idx == 1:
                    loaded_pool, is_first_run = self._load_pool_from_file()
                    if is_first_run:
                        logger.info("首次运行，初始化股票池...")
                    else:
                        self.buy_candidate_pool = loaded_pool
                        logger.info(f"从持久化文件加载股票池: {len(self.buy_candidate_pool)} 只股票")
                
                # 初始化择时策略
                timing_params = config.get('timing_params', {})
                strategy_params = timing_params.get(timing_strategy_name, {})
                
                # 特殊处理：如果是海龟策略
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
                
                self.timing_strategy = TimingStrategyFactory.create_strategy(
                    timing_strategy_name, strategy_params
                )
                self.timing_strategy_name = timing_strategy_name
                self.timing_strategy_params = strategy_params
                
                # 执行选股和评分
                candidate_stocks = self._select_and_score_stocks(selection_strategy, working_date, score_threshold)
                logger.info(f"{selection_strategy} 选出 {len(candidate_stocks)} 只股票")
                
                # 将选出的股票加入股票池
                for stock in candidate_stocks:
                    # 检查是否已在池中
                    exists = any(item['stock']['stock_code'] == stock['stock_code'] for item in self.buy_candidate_pool)
                    if not exists:
                        # 计算支撑位
                        support_level = self._calculate_support_level(stock, selection_strategy, working_date)
                        support_method = self._get_support_method_for_strategy(selection_strategy)
                        
                        # 提取关键日（从策略信号中获取，默认为选入日期）
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
                            'key_date': key_date,                      # 关键日（形态实际形成日期）
                            'strategy_name': selection_strategy,
                            'support_level': support_level,
                            'support_method': support_method
                        })
                
                # 检查股票池移除条件
                removed = self._check_pool_removal(working_date)
                if removed:
                    logger.info(f"股票池移除 {len(removed)} 只股票，剩余: {len(self.buy_candidate_pool)} 只")
                
                # 记录组合执行结果
                combination_results.append({
                    'combination_id': combination.id,
                    'selection_strategy': selection_strategy,
                    'timing_strategy': timing_strategy_name,
                    'selected_count': len(candidate_stocks),
                    'pool_count_after': len(self.buy_candidate_pool)
                })
            
            # 保存股票池到持久化文件
            self._save_pool_to_file(self.buy_candidate_pool, working_date)
            logger.info(f"股票池保存完成: {len(self.buy_candidate_pool)} 只股票")
            
            # ========== 执行交易操作 ==========
            
            # 卖出操作
            sell_signals = self._execute_sell_operations(working_date)
            
            # 买入操作
            buy_signals = self._execute_buy_operations(working_date, initial_cash)
            
            # 构建当日记录
            daily_record = {
                "date": working_date,
                "trading_date": working_date,
                "status": "completed",
                "plan_id": plan.id,
                "plan_name": plan.name,
                "is_first_run": is_first_run,
                "pool_summary": {
                    "stock_count": len(self.buy_candidate_pool),  # 只统计候选股票数量
                    "removed_count": removed_count if 'removed_count' in locals() else 0,
                    "added_count": added_count if 'added_count' in locals() else 0
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
                        "status": "candidate"
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
                "portfolio": self.portfolio,
                "combination_results": combination_results
            }
            
            # 保存当日记录
            records_file = self.running_dir / f"daily_{working_date}.json"
            self._save_daily_record(working_date, daily_record, str(records_file))
            
            # 保存信号
            signals = sell_signals + buy_signals
            self.signals.extend(signals)
            self._save_signals(self.signals, str(signals_file))
            
            # 保存持仓信息
            self._save_portfolio(self.portfolio, str(portfolio_file))
            
            logger.info(f"方案执行完成: {plan.name}")
            return {
                "status": "success",
                "message": "方案执行完成",
                "data": {
                    "run_date": working_date,
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "is_first_run": is_first_run,
                    "pool_count": len(self.buy_candidate_pool),
                    "total_signals": len(signals),
                    "buy_signals": len(buy_signals),
                    "sell_signals": len(sell_signals),
                    "final_portfolio": {
                        "position_count": len(self.portfolio)
                    },
                    "combination_results": combination_results
                }
            }
        
        except Exception as e:
            logger.error(f"方案执行失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "failed", "message": str(e)}
        finally:
            _strategy_run_lock.release()
            logger.info("释放策略运行锁")
