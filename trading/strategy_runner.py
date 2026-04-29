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
from scipy import stats

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
        
        # 加载回测评分器
        from trading.backtest_scorer import BacktestScoreCalculator
        self.score_calculator = BacktestScoreCalculator(db_manager=self.db_manager)
        
        # 加载股票池移除配置
        self._pool_removal_config = self._load_pool_removal_config()
        
        # 加载支撑位方法配置
        self._support_methods_config = self._load_support_methods_config()
    
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
    
    def _load_pool_from_file(self) -> Tuple[List[Dict], bool]:
        """从文件加载股票池
        
        Returns:
            (股票池列表, 是否首次运行)
        """
        pool_file = Path(POOL_PERSIST_FILE)
        if not pool_file.exists():
            logger.info("股票池文件不存在，首次运行将初始化")
            return [], True
        
        try:
            with open(pool_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            pool = data.get('pool', [])
            last_date = data.get('last_date', '')
            
            logger.info(f"从文件加载股票池: {len(pool)} 只股票，上次运行日期: {last_date}")
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
            data = {
                'last_date': date,
                'pool': pool,
                'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(POOL_PERSIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
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
                    
                    # 执行选股
                    signal_list = strategy.execute_selection(df_to_date, code, name)
                    
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
                df_to_date = df[df['date'] <= current_date].copy()
                price_for_check = df_to_date.iloc[0]['close'] if len(df_to_date) > 0 else 0
            else:
                # 非当日加入的股票：使用前一日收盘价
                df_to_date = df[df['date'] <= prev_date_str].copy()
                if len(df_to_date) < 20:
                    remaining.append(candidate)
                    continue
                price_for_check = df_to_date.iloc[-1]['close']
            
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
            
            # 查询是否有当日的股票数据
            sql = f"""
                SELECT COUNT(*) FROM stock_kline 
                WHERE date = '{date}' 
                LIMIT 1
            """
            result = db_manager.query(sql)
            
            return result[0]['COUNT(*)'] > 0 if result else False
        except Exception as e:
            logger.warning(f"检查K线数据失败: {str(e)}")
            return False
    
    def get_working_date(self) -> str:
        """获取当前工作日期
        
        判断逻辑：
        - 当日有K线数据，工作日即为当日
        - 当日没有K线数据（比如周末、盘中交易时间），则为前一个交易日
        
        Returns:
            工作日期字符串 (YYYY-MM-DD)
        """
        today = datetime.datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        
        # 检查当日是否有K线数据
        if self._has_kline_data(today_str):
            # 有K线数据，使用今日
            logger.info(f"当日有K线数据，使用今日作为工作日期: {today_str}")
            return today_str
        
        # 没有K线数据，返回前一交易日
        working_date = get_previous_trading_day(today_str)
        logger.info(f"当日没有K线数据，使用前一交易日: {working_date}")
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
                
                # 记录择时信号详情
                stock_name = position['stock_name']
                logger.info(f"【择时信号】{trade_date} {stock_code} {stock_name} | "
                           f"持仓: {position['quantity']}股 | "
                           f"成本: ¥{buy_price:.2f} | 现价: ¥{current_price:.2f} | "
                           f"收益率: {profit_rate*100:.2f}% | "
                           f"择时卖出: {timing_result.is_sell} | "
                           f"信号: {timing_result.message}")
                
                # 生成卖出信号
                if timing_result.is_sell or profit_rate >= self.take_profit_threshold or profit_rate <= self.stop_loss_threshold:
                    # 确定卖出原因
                    if timing_result.is_sell:
                        reason = timing_result.message
                        signal_type = 'strategy_sell'
                    elif profit_rate >= self.take_profit_threshold:
                        reason = f'止盈 (收益率: {profit_rate*100:.2f}%)'
                        signal_type = 'take_profit'
                    else:
                        reason = f'止损 (收益率: {profit_rate*100:.2f}%)'
                        signal_type = 'stop_loss'
                    
                    # 记录卖出决策
                    logger.info(f"【卖出决策】{trade_date} {stock_code} {stock_name} | "
                               f"数量: {position['quantity']}股 | 价格: ¥{current_price:.2f} | "
                               f"金额: ¥{current_price * position['quantity']:.2f} | "
                               f"原因: {reason}")
                    
                    signal = {
                        'id': f"sell_{stock_code}_{trade_date}",
                        'date': trade_date,
                        'stock_code': stock_code,
                        'stock_name': stock_name,
                        'signal_type': signal_type,
                        'quantity': position['quantity'],
                        'price': current_price,
                        'amount': current_price * position['quantity'],
                        'reason': reason,
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
            
            logger.info(f"【卖出汇总】{trade_date} 执行卖出操作，生成 {len(sell_signals)} 个卖出信号")
        except Exception as e:
            logger.error(f"执行卖出操作失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
        
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
                logger.info(f"【买入检查】{trade_date} 可用资金不足 (¥{current_cash:.2f})，跳过买入操作")
                return buy_signals
            
            if len(self.portfolio) >= max_stocks:
                logger.info(f"【买入检查】{trade_date} 持仓数量已达上限 ({len(self.portfolio)}/{max_stocks})，跳过买入操作")
                return buy_signals
            
            logger.info(f"【买入检查】{trade_date} 开始检查买入机会 | 可用资金: ¥{current_cash:.2f} | "
                       f"持仓: {len(self.portfolio)}/{max_stocks} | 候选股票: {len(self.buy_candidate_pool)}")
            
            # 遍历可买股票池
            for candidate in self.buy_candidate_pool:
                # 检查是否已达到最大持仓数
                if len(self.portfolio) >= max_stocks:
                    logger.info(f"【买入检查】{trade_date} 已达最大持仓数，停止检查")
                    break
                
                # 适配新的股票池结构
                stock_info = candidate.get('stock', candidate)
                stock_code = stock_info['stock_code']
                stock_name = stock_info['stock_name']
                score = stock_info.get('score', 0)
                
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
                
                # 检查是否已在持仓中
                existing_pos = self.portfolio.get(stock_code)
                
                # 调用择时策略判断
                timing_result = self.timing_strategy.get_timing_result(df_to_date, existing_pos, current_cash, use_prev_day_signal=False)
                
                # 记录择时信号详情
                current_price = df_to_date.iloc[-1]['close']
                logger.info(f"【择时信号】{trade_date} {stock_code} {stock_name} | "
                           f"评分: {score:.1f} | 现价: ¥{current_price:.2f} | "
                           f"支撑位: ¥{candidate.get('support_level', 0):.2f} | "
                           f"买入信号: {timing_result.is_buy} | "
                           f"加仓信号: {timing_result.trade_type == 'add'} | "
                           f"信号强度: {timing_result.signal_strength:.2f} | "
                           f"信息: {timing_result.message}")
                
                # 生成买入信号
                if timing_result.is_buy:
                    # 使用择时策略返回的买入数量，如果为0则使用默认计算
                    if timing_result.buy_quantity > 0:
                        buy_quantity = timing_result.buy_quantity
                        quantity_source = '择时策略'
                    else:
                        # 默认计算买入数量
                        buy_amount = min(current_cash * 0.2, 100000)
                        buy_quantity = int(buy_amount / current_price / 100) * 100
                        quantity_source = '默认公式'
                    
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
                    
                    # 更新持仓
                    self.portfolio[stock_code] = {
                        'stock_name': stock_name,
                        'quantity': buy_quantity,
                        'buy_price': current_price,
                        'buy_date': trade_date,
                        'current_price': current_price,
                        'profit_loss': 0.0,
                        'profit_rate': 0.0,
                        'holding_days': 0,
                        'industry': stock_info.get('industry', ''),
                        'sector': stock_info.get('sector', ''),
                        'selection_score': stock_info.get('score', 0),
                        'support_level': candidate.get('support_level', 0)
                    }
                    
                    # 更新可用资金
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
            self.portfolio = self._load_portfolio(str(portfolio_file))
            
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
                # 首次运行：执行选股初始化股票池
                logger.info("首次运行，初始化股票池...")
                
                strategy_name = strategy_names[0] if strategy_names else 'default'
                
                # 执行选股和评分
                candidate_stocks = self._select_and_score_stocks(strategy_name, working_date, score_threshold)
                
                # 初始化股票池
                for stock in candidate_stocks:
                    # 计算支撑位
                    support_level = self._calculate_support_level(stock, strategy_name, working_date)
                    support_method = self._get_support_method_for_strategy(strategy_name)
                    
                    self.buy_candidate_pool.append({
                        'stock': stock,
                        'added_date': working_date,
                        'strategy_name': strategy_name,
                        'support_level': support_level,
                        'support_method': support_method
                    })
                
                logger.info(f"首次运行初始化股票池: {len(self.buy_candidate_pool)} 只股票")
            else:
                # 后续运行：使用已加载的股票池
                self.buy_candidate_pool = loaded_pool
                logger.info(f"从持久化文件加载股票池: {len(self.buy_candidate_pool)} 只股票")
            
            # 2. 检查股票池移除条件（破支撑位、趋势验证）
            logger.info(f"开始检查股票池移除条件，当前股票池数量: {len(self.buy_candidate_pool)}")
            removed = self._check_pool_removal(working_date)
            if removed:
                logger.info(f"股票池移除 {len(removed)} 只股票，剩余: {len(self.buy_candidate_pool)} 只")
            
            # 3. 继续选股，加入新股票（直接使用工作日期进行选股）
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
                        
                        self.buy_candidate_pool.append({
                            'stock': stock,
                            'added_date': selection_date_str,
                            'strategy_name': strategy_name,
                            'support_level': support_level,
                            'support_method': support_method
                        })
                        
                        logger.info(f"股票 {stock['stock_code']} {stock['stock_name']} 加入股票池, "
                                   f"支撑位={support_level:.2f}, 方法={support_method}")
            
            logger.info(f"选股后股票池数量: {len(self.buy_candidate_pool)}")
            
            # 4. 保存股票池到持久化文件
            self._save_pool_to_file(self.buy_candidate_pool, working_date)
            
            # ========== 执行交易操作 ==========
            
            # 5. 卖出操作
            sell_signals = self._execute_sell_operations(working_date)
            
            # 6. 买入操作
            buy_signals = self._execute_buy_operations(working_date, initial_cash, max_stocks)
            
            # 7. 构建当日记录
            daily_record = {
                "date": working_date,
                "trading_date": working_date,
                "status": "completed",
                "is_first_run": is_first_run,
                "pool_summary": {
                    "stock_count": len(self.buy_candidate_pool) + len(self.portfolio)
                },
                "pool_stocks": [
                    {
                        "code": candidate['stock']['stock_code'],
                        "name": candidate['stock']['stock_name'],
                        "score": candidate['stock'].get('score', 0),
                        "days": (datetime.datetime.strptime(working_date, '%Y-%m-%d') - 
                                datetime.datetime.strptime(candidate.get('added_date', working_date), '%Y-%m-%d')).days + 1,
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
            
            # 9. 保存信号
            signals = sell_signals + buy_signals
            self.signals.extend(signals)
            self._save_signals(self.signals, str(signals_file))
            
            # 10. 保存持仓信息
            self._save_portfolio(self.portfolio, str(portfolio_file))
            
            logger.info(f"策略运行完成: {working_date}")
            return {
                "status": "success", 
                "message": "策略运行完成",
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
