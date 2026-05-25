"""
股票池管理器模块
负责股票池的加载、保存和管理
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


class PoolManager:
    """股票池管理器"""
    
    def _load_pool_from_file(self, working_date: str = None) -> Tuple[List[Dict], bool]:
        """从文件加载股票池
        
        Args:
            working_date: 工作日期（可选）
            
        Returns:
            (股票池列表, 是否为首次运行)
        """
        pool_file = self.running_dir / "buy_candidate_pool.json"
        
        if pool_file.exists():
            try:
                with open(pool_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if isinstance(data, dict) and 'pool' in data:
                    pool = data['pool']
                    is_first_run = False
                elif isinstance(data, list):
                    pool = data
                    is_first_run = False
                else:
                    pool = []
                    is_first_run = True
                    
                logger.info(f"从文件加载股票池: {len(pool)} 只股票")
                return pool, is_first_run
                
            except Exception as e:
                logger.error(f"加载股票池文件失败: {str(e)}")
                return [], True
        else:
            logger.info("股票池文件不存在，首次运行")
            return [], True
    
    def _save_pool_to_file(self, pool: List[Dict], date: str):
        """保存股票池到文件
        
        Args:
            pool: 股票池列表
            date: 日期
        """
        pool_file = self.running_dir / "buy_candidate_pool.json"
        
        try:
            data = {
                "date": date,
                "update_time": self._get_current_time_str(),
                "pool": pool
            }
            
            with open(pool_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"股票池已保存到: {pool_file}")
            
        except Exception as e:
            logger.error(f"保存股票池文件失败: {str(e)}")
    
    def _update_stock_cool_down_status(self, stock_code: str, is_cooling: bool, cool_down_end: str = None):
        """更新股票冷却状态
        
        Args:
            stock_code: 股票代码
            is_cooling: 是否冷却中
            cool_down_end: 冷却结束日期（可选）
        """
        for candidate in self.buy_candidate_pool:
            if candidate['stock'].get('stock_code') == stock_code:
                candidate['is_cooling'] = is_cooling
                candidate['cool_down_end'] = cool_down_end
                break
    
    def _check_cool_down(self, stock_code: str, current_date: str) -> bool:
        """检查股票是否在冷却期
        
        Args:
            stock_code: 股票代码
            current_date: 当前日期
            
        Returns:
            True表示在冷却期，False表示不在冷却期
            
        Note:
            如果冷却期已过期，会自动更新股票的冷却状态为False
        """
        for candidate in self.buy_candidate_pool:
            if candidate['stock'].get('stock_code') == stock_code:
                if candidate.get('is_cooling', False):
                    cool_down_end = candidate.get('cool_down_end')
                    if cool_down_end and current_date <= cool_down_end:
                        return True
                    elif cool_down_end and current_date > cool_down_end:
                        # 冷却期已过期，更新状态
                        candidate['is_cooling'] = False
                        candidate['cool_down_end'] = None
                        logger.info(f"股票 {stock_code} 冷却期已过期，状态已更新")
        return False
    
    def _get_current_time_str(self) -> str:
        """获取当前时间字符串
        
        Returns:
            当前时间字符串，格式为 YYYY-MM-DD HH:MM:SS
        """
        import datetime
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')