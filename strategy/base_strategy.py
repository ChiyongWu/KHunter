"""
策略基类定义
"""
from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """策略抽象基类"""
    
    def __init__(self, name, params=None):
        """
        初始化策略
        :param name: 策略名称
        :param params: 参数字典
        """
        self.name = name
        self.params = params or {}
    
    def quick_filter(self, df):
        """
        快速过滤 - 由子类实现
        
        目的：提前过滤不符合条件的股票，避免不必要的指标计算
        原则：只基于价格，不涉及复杂指标
        
        :param df: 股票数据DataFrame
        :return: True表示通过快速过滤，False表示未通过
        """
        # 默认实现：不进行快速过滤
        return True
    
    @abstractmethod
    def calculate_indicators(self, df) -> pd.DataFrame:
        """
        计算技术指标
        :param df: 股票数据DataFrame
        :return: 添加了指标列的DataFrame
        """
        pass
    
    @abstractmethod
    def select_stocks(self, df, stock_name='') -> list:
        """
        选股逻辑
        :param df: 包含指标的股票数据
        :param stock_name: 股票名称，用于过滤退市股票
        :return: 选股信号列表，每个元素为字典包含信号详情
        """
        pass
    
    def get_selection_criteria(self):
        """
        获取选股条件描述
        :return: 选股条件描述列表
        """
        return []
    
    def execute_selection(self, df, stock_code='', stock_name=''):
        """
        标准化的选股执行过程
        
        执行流程：
            1. 数据验证
            2. 快速过滤
            3. 计算指标
            4-N. 选股条件检查
        
        :param df: 股票数据DataFrame（正序，从旧到新）
        :param stock_code: 股票代码
        :param stock_name: 股票名称
        :return: 选股信号列表
        """
        # 第1步：数据验证
        if df is None or df.empty or len(df) < 20:
            return []
        
        # 第2步：快速过滤（由子类实现）
        if not self.quick_filter(df):
            return []
        
        # 第3步：计算指标
        try:
            df = self.calculate_indicators(df)
        except Exception:
            return []
        
        # 第4-N步：选股条件检查（由子类实现）
        return self.select_stocks(df, stock_name)
    
    def analyze_stock(self, stock_code, stock_name, df):
        """
        分析单只股票 - 专注于流程处理
        
        :param stock_code: 股票代码
        :param stock_name: 股票名称
        :param df: 股票数据DataFrame
        :return: 标准化的选股结果或None
        """
        try:
            # 使用标准化的选股执行过程
            signals = self.execute_selection(df, stock_code, stock_name)
            
            # 结果过滤和标准化
            if signals:
                return {
                    'code': stock_code,
                    'name': stock_name,
                    'signals': signals
                }
            return None
            
        except Exception as e:
            # 错误处理
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"分析股票 {stock_code} 失败: {str(e)}")
            return None
