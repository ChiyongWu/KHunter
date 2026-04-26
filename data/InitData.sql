-- ============================================
-- KHunter 系统 - 数据初始化脚本
-- 说明: 包含各表的初始化数据
-- 数据库类型: SQLite
-- ============================================

-- ============================================
-- 股票池移除策略配置表初始化数据
-- ============================================
-- 择时策略（TurtleStrategy、SupportStrategy）不用于选股，不参与股票池移除
INSERT OR REPLACE INTO pool_removal_config (strategy_name, min_hold_days, is_enabled, remarks) VALUES
    ('ImmortalGuidanceStrategy', 0, 1, '仙人指路策略：买入后立即验证趋势'),
    ('ContinuousRisingWithVolumeStrategyV2', 3, 1, '连阳回调策略：持有3天后验证趋势'),
    ('ResistBreakoutStrategy', 2, 1, '阻力突破策略：持有2天后验证趋势'),
    ('BottomTrendInflectionStrategy', 5, 1, '底部反转策略：持有5天后验证趋势');
