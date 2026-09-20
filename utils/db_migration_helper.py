#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库迁移助手
用于检查和添加缺失的数据库列
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DatabaseMigrationHelper:
    """数据库迁移助手"""
    
    def __init__(self, db_path: str = 'data/stock_selection.db'):
        """
        初始化迁移助手
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
    
    def check_and_add_khunter_score_date_column(self) -> bool:
        """
        检查并添加 khunter 表的 score_date 列
        
        Returns:
            bool: 如果列已存在或成功添加则返回 True
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 检查 khunter 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='khunter'")
            if not cursor.fetchone():
                logger.warning("khunter 表不存在，跳过 score_date 列检查")
                conn.close()
                return False
            
            # 检查 score_date 列是否存在
            cursor.execute("PRAGMA table_info(khunter)")
            cols = cursor.fetchall()
            col_names = [col[1] for col in cols]
            
            if 'score_date' in col_names:
                logger.info("✓ khunter 表已有 score_date 列")
                conn.close()
                return True
            
            # 添加 score_date 列
            logger.info("正在为 khunter 表添加 score_date 列...")
            cursor.execute("ALTER TABLE khunter ADD COLUMN score_date DATE")
            conn.commit()
            logger.info("✓ score_date 列已成功添加到 khunter 表")
            
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"检查/添加 score_date 列失败: {str(e)}")
            return False
    
    def check_and_add_khunter_timing_columns(self) -> bool:
        """
        检查并添加 khunter 表的 timing_strategy 和 timing_signal 列
        
        Returns:
            bool: 如果列已存在或成功添加则返回 True
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 检查 khunter 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='khunter'")
            if not cursor.fetchone():
                logger.warning("khunter 表不存在，跳过 timing_strategy 列检查")
                conn.close()
                return False
            
            # 获取现有列
            cursor.execute("PRAGMA table_info(khunter)")
            cols = cursor.fetchall()
            col_names = [col[1] for col in cols]
            
            # 添加 timing_strategy 列
            if 'timing_strategy' not in col_names:
                logger.info("正在为 khunter 表添加 timing_strategy 列...")
                cursor.execute("ALTER TABLE khunter ADD COLUMN timing_strategy VARCHAR(20) NOT NULL DEFAULT 'support'")
                conn.commit()
                logger.info("✓ timing_strategy 列已成功添加到 khunter 表")
            else:
                logger.info("✓ khunter 表已有 timing_strategy 列")
            
            # 添加 timing_signal 列
            if 'timing_signal' not in col_names:
                logger.info("正在为 khunter 表添加 timing_signal 列...")
                cursor.execute("ALTER TABLE khunter ADD COLUMN timing_signal VARCHAR(200)")
                conn.commit()
                logger.info("✓ timing_signal 列已成功添加到 khunter 表")
            else:
                logger.info("✓ khunter 表已有 timing_signal 列")
            
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"检查/添加 timing_strategy/timing_signal 列失败: {str(e)}")
            return False
    
    def check_and_add_khunter_key_date_column(self) -> bool:
        """
        检查并添加 khunter 表的 key_date 列（关键日，形态实际形成日期）

        Returns:
            bool: 如果列已存在或成功添加则返回 True
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 检查 khunter 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='khunter'")
            if not cursor.fetchone():
                logger.warning("khunter 表不存在，跳过 key_date 列检查")
                conn.close()
                return False

            # 获取现有列
            cursor.execute("PRAGMA table_info(khunter)")
            cols = cursor.fetchall()
            col_names = [col[1] for col in cols]

            # 添加 key_date 列
            if 'key_date' not in col_names:
                logger.info("正在为 khunter 表添加 key_date 列...")
                cursor.execute("ALTER TABLE khunter ADD COLUMN key_date DATE")
                conn.commit()
                logger.info("✓ key_date 列已成功添加到 khunter 表")
            else:
                logger.info("✓ khunter 表已有 key_date 列")

            conn.close()
            return True

        except Exception as e:
            logger.error(f"检查/添加 key_date 列失败: {str(e)}")
            return False

    def check_and_add_khunter_buy_range_column(self) -> bool:
        """
        检查并添加 khunter 表的 buy_range 列
        
        Returns:
            bool: 如果列已存在或成功添加则返回 True
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 检查 khunter 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='khunter'")
            if not cursor.fetchone():
                logger.warning("khunter 表不存在，跳过 buy_range 列检查")
                conn.close()
                return False
            
            # 获取现有列
            cursor.execute("PRAGMA table_info(khunter)")
            cols = cursor.fetchall()
            col_names = [col[1] for col in cols]
            
            # 添加 buy_range 列
            if 'buy_range' not in col_names:
                logger.info("正在为 khunter 表添加 buy_range 列...")
                cursor.execute("ALTER TABLE khunter ADD COLUMN buy_range VARCHAR(50)")
                conn.commit()
                logger.info("✓ buy_range 列已成功添加到 khunter 表")
            else:
                logger.info("✓ khunter 表已有 buy_range 列")
            
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"检查/添加 buy_range 列失败: {str(e)}")
            return False
    
    def check_and_migrate_sector_hot_rank_ytd(self) -> bool:
        """
        迁移 sector_hot_rank 表：
        1. 添加 ytd_pct_chg 列（年初至今涨跌幅，通达信数据源）
        2. 添加 prev_year_pct_chg 列（上一年自然年涨跌幅）
        3. 清理旧的同花顺 .TI 板块数据（数据源已切换为通达信 .TDX）

        Returns:
            bool: 如果迁移成功或无需迁移则返回 True
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 检查 sector_hot_rank 表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sector_hot_rank'")
            if not cursor.fetchone():
                logger.warning("sector_hot_rank 表不存在，跳过 ytd_pct_chg 列检查")
                conn.close()
                return False

            # 获取现有列
            cursor.execute("PRAGMA table_info(sector_hot_rank)")
            cols = cursor.fetchall()
            col_names = [col[1] for col in cols]

            # 添加 ytd_pct_chg 列
            if 'ytd_pct_chg' not in col_names:
                logger.info("正在为 sector_hot_rank 表添加 ytd_pct_chg 列...")
                cursor.execute("ALTER TABLE sector_hot_rank ADD COLUMN ytd_pct_chg REAL")
                conn.commit()
                logger.info("✓ ytd_pct_chg 列已成功添加到 sector_hot_rank 表")
            else:
                logger.info("✓ sector_hot_rank 表已有 ytd_pct_chg 列")

            # 添加 prev_year_pct_chg 列
            if 'prev_year_pct_chg' not in col_names:
                logger.info("正在为 sector_hot_rank 表添加 prev_year_pct_chg 列...")
                cursor.execute("ALTER TABLE sector_hot_rank ADD COLUMN prev_year_pct_chg REAL")
                conn.commit()
                logger.info("✓ prev_year_pct_chg 列已成功添加到 sector_hot_rank 表")
            else:
                logger.info("✓ sector_hot_rank 表已有 prev_year_pct_chg 列")

            # 添加通达信行情扩展列（成交额 + 主力/主买资金四字段）
            for col in ('amount', 'bm_net', 'bm_ratio', 'bm_buy_net', 'bm_buy_ratio'):
                if col not in col_names:
                    logger.info(f"正在为 sector_hot_rank 表添加 {col} 列...")
                    cursor.execute(f"ALTER TABLE sector_hot_rank ADD COLUMN {col} REAL")
                    conn.commit()
                    logger.info(f"✓ {col} 列已成功添加到 sector_hot_rank 表")
                else:
                    logger.info(f"✓ sector_hot_rank 表已有 {col} 列")

            # 清理旧的同花顺 .TI 数据（数据源已切换为通达信 .TDX）
            cursor.execute("SELECT COUNT(*) FROM sector_hot_rank WHERE sector_code LIKE '%.TI'")
            old_count = cursor.fetchone()[0]
            if old_count > 0:
                logger.info(f"正在清理旧同花顺 .TI 板块数据（{old_count} 条）...")
                cursor.execute("DELETE FROM sector_hot_rank WHERE sector_code LIKE '%.TI'")
                conn.commit()
                logger.info(f"✓ 已清理旧同花顺 .TI 数据 {old_count} 条")

            conn.close()
            return True

        except Exception as e:
            logger.error(f"迁移 sector_hot_rank 表失败: {str(e)}")
            return False

    def check_all_required_columns(self) -> dict:
        """
        检查所有必需的列
        
        Returns:
            dict: 包含检查结果的字典
        """
        required_columns = {
            'khunter': [
                'id', 'stock_code', 'stock_name', 'industry', 'sector',
                'key_date', 'hunting_date', 'strategy_name', 'support_level', 'current_price',
                'price_diff', 'price_diff_percent', 'buy_range', 'score', 'score_date',
                'selection_record_id', 'created_at', 'updated_at',
                'timing_strategy', 'timing_signal'
            ]
        }
        
        results = {}
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            for table_name, required_cols in required_columns.items():
                # 检查表是否存在
                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                if not cursor.fetchone():
                    results[table_name] = {
                        'exists': False,
                        'missing_columns': required_cols
                    }
                    continue
                
                # 检查列
                cursor.execute(f"PRAGMA table_info({table_name})")
                cols = cursor.fetchall()
                existing_cols = {col[1] for col in cols}
                
                missing_cols = [col for col in required_cols if col not in existing_cols]
                
                results[table_name] = {
                    'exists': True,
                    'missing_columns': missing_cols,
                    'total_columns': len(existing_cols)
                }
            
            conn.close()
            
        except Exception as e:
            logger.error(f"检查列失败: {str(e)}")
        
        return results
    
    def print_migration_status(self):
        """打印迁移状态"""
        results = self.check_all_required_columns()
        
        logger.info("=" * 60)
        logger.info("数据库迁移状态检查")
        logger.info("=" * 60)
        
        for table_name, status in results.items():
            if status['exists']:
                if status['missing_columns']:
                    logger.warning(f"表 {table_name}: 缺少列 {status['missing_columns']}")
                else:
                    logger.info(f"✓ 表 {table_name}: 所有列都存在 ({status['total_columns']} 列)")
            else:
                logger.warning(f"表 {table_name}: 不存在")
        
        logger.info("=" * 60)


def ensure_database_schema(db_path: str = 'data/stock_selection.db') -> bool:
    """
    确保数据库模式正确
    
    Args:
        db_path: 数据库文件路径
    
    Returns:
        bool: 如果所有检查都通过则返回 True
    """
    helper = DatabaseMigrationHelper(db_path)
    
    # 检查并添加 score_date 列
    success = helper.check_and_add_khunter_score_date_column()
    
    # 检查并添加 timing_strategy 和 timing_signal 列
    success = helper.check_and_add_khunter_timing_columns() and success
    
    # 检查并添加 key_date 列
    success = helper.check_and_add_khunter_key_date_column() and success

    # 检查并添加 buy_range 列
    success = helper.check_and_add_khunter_buy_range_column() and success

    # 迁移 sector_hot_rank 表（ytd/上一年涨幅列 + 清理 .TI 旧数据）
    success = helper.check_and_migrate_sector_hot_rank_ytd() and success
    
    # 打印迁移状态
    helper.print_migration_status()
    
    return success
