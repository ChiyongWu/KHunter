# 热门板块改版 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把仪表盘【最热板块】改版为同花顺 APP 样式的"热门板块"榜单：最近交易日概念/行业板块按主力净流入额排名，列为排名/板块名称(含代码)/涨幅/热度。

**架构：** Tushare 同花顺接口（ths_index/ths_daily/moneyflow_cnt_ths/moneyflow_ind_ths）每日随 daily_update 落库到新表 `sector_hot_rank`；新接口 `/api/dashboard/hot-sectors` 只查库返回前 5；前端重写 `loadHotSectors()` 带 tab 切换；删除旧 hot-industries/hot-areas/industry-stocks/area-stocks 四个接口及对应前端代码。设计文档：`docs/superpowers/specs/2026-09-20-hot-sectors-design.md`。

**技术栈：** Flask + SQLite（DBManager）、Tushare pro API、原生 ES Module 前端（无框架，自定义 CSS，可用类：`.btn/.btn-secondary/.text-muted/.text-danger/.text-success/.table/.table-striped/.table-responsive`，**无 Bootstrap，无 .btn-group/.btn-danger**）。

**测试说明：** 本项目无任何测试基础设施（无 tests/ 目录、无 pytest 配置），遵循项目既有惯例：每个任务用可运行的验证命令（py_compile、临时脚本查库、curl、浏览器）代替单元测试。

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `data/DataSql.sql` | 修改 | 末尾追加 `sector_hot_rank` 建表语句（启动时 `init_databases_if_needed` 自动执行 CREATE TABLE IF NOT EXISTS） |
| `utils/sector_hot_rank_fetcher.py` | 创建 | 热门板块采集器：3 个 Tushare 接口抓取 + 拼装 + 事务落库，唯一入口 `fetch_and_store(trade_date)` |
| `utils/akshare_fetcher.py` | 修改 | `daily_update()` 第5步后挂钩热门板块更新（失败不阻塞主流程） |
| `web_server.py` | 修改 | 删除 4 个旧接口（L393-658），新增 `GET /api/dashboard/hot-sectors` |
| `web/static/js/modules/stocks.js` | 修改 | 删除 `loadHotIndustries`/`showIndustryStocks`/`showAreaStocks`，重写 `loadHotAreas` → `loadHotSectors` |
| `web/static/js/app.js` | 修改 | 清理旧调用与 window 挂载，挂载 `loadHotSectors` |
| `web/templates/index.html` | 修改 | 删除【最热行业】卡片，重写【热门板块】卡片，app.js 版本号 `?v=15`→`?v=16` |
| `tmp_verify_hot_sectors.py` | 创建后删除 | 临时验证脚本：手动触发落库 + 查库校验，任务 7 结束时删除 |

---

### 任务 1：新增 sector_hot_rank 表

**文件：**
- 修改：`data/DataSql.sql`（文件末尾，L972 之后）

- [ ] **步骤 1：在 DataSql.sql 末尾追加建表语句**

在文件末尾（`-- idx_stock_favorite_code: 股票代码索引，用于快速查询收藏状态` 之后）追加：

```sql

-- ============================================
-- 28. 热门板块排名表（KHunter）
-- ============================================
CREATE TABLE IF NOT EXISTS sector_hot_rank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- id: 自增主键
    trade_date DATE NOT NULL,
    -- trade_date: 交易日，格式YYYY-MM-DD
    sector_code VARCHAR(20) NOT NULL,
    -- sector_code: 同花顺板块代码，例如885823.TI
    sector_name VARCHAR(50) NOT NULL,
    -- sector_name: 板块名称，例如创新药
    sector_type VARCHAR(10) NOT NULL,
    -- sector_type: 板块类型，concept概念/industry行业
    pct_chg REAL,
    -- pct_chg: 板块涨跌幅，百分比，例如1.27
    main_net_flow REAL,
    -- main_net_flow: 主力净流入额，单位元，例如208000000
    created_date DATETIME,
    -- created_date: 创建时间，格式YYYY-MM-DD HH:MM:SS
    UNIQUE(trade_date, sector_code)
    -- 交易日、板块代码组合唯一，同一交易日重跑覆盖更新
);
```

- [ ] **步骤 2：运行临时验证脚本确认建表成功**

创建临时脚本 `tmp_verify_hot_sectors.py`（项目根目录，任务 7 会删除）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时验证脚本：确认建表/手动触发热门板块落库并检查结果（验证后删除）"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

from utils.db_manager import DBManager


def check_table():
    db = DBManager()
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name='sector_hot_rank'")
    print(f"建表检查: {rows}")
    assert rows, "sector_hot_rank 表不存在！"


def fetch_and_check(trade_date):
    from utils.sector_hot_rank_fetcher import SectorHotRankFetcher
    db = DBManager()
    saved = SectorHotRankFetcher(db).fetch_and_store(trade_date)
    print(f"\n写入条数: {saved}")

    rows = db.query(
        "SELECT sector_type, COUNT(*) AS cnt FROM sector_hot_rank WHERE trade_date = ? GROUP BY sector_type",
        (trade_date,)
    )
    for r in rows:
        print(f"{r['sector_type']}: {r['cnt']} 条")

    dup = db.query(
        "SELECT sector_code, COUNT(*) AS cnt FROM sector_hot_rank GROUP BY sector_code, trade_date HAVING cnt > 1"
    )
    print(f"唯一性检查（应无输出）: {dup}")

    top = db.query(
        "SELECT sector_name, sector_type, pct_chg, main_net_flow FROM sector_hot_rank "
        "WHERE trade_date = ? AND sector_type = 'concept' ORDER BY main_net_flow DESC LIMIT 5",
        (trade_date,)
    )
    print("\n概念板块 Top5（按主力净流入降序）:")
    for r in top:
        print(f"  {r['sector_name']}  {r['pct_chg']}%  {r['main_net_flow'] / 1e8:.2f}亿")


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'fetch':
        fetch_and_check(sys.argv[2])
    else:
        check_table()
```

运行：`python tmp_verify_hot_sectors.py`
预期：输出 `建表检查: [('sector_hot_rank',)]`

- [ ] **步骤 3：Commit**

```bash
git add data/DataSql.sql tmp_verify_hot_sectors.py
git commit -m "feat: 新增热门板块排名表 sector_hot_rank"
```

---

### 任务 2：新建 SectorHotRankFetcher 落库模块

**文件：**
- 创建：`utils/sector_hot_rank_fetcher.py`

- [ ] **步骤 1：创建采集器模块**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热门板块数据采集器

通过 Tushare 同花顺接口抓取板块行情与主力资金流，拼装后写入 sector_hot_rank 表，
供仪表盘"热门板块"卡片展示最近交易日的热门概念/行业板块。

数据源（每个交易日约 3-4 次请求）：
- ths_index          板块清单（type N=概念 / I=行业）
- ths_daily          板块日行情（pct_chg）
- moneyflow_cnt_ths  概念板块资金流（net_amount，单位亿元）
- moneyflow_ind_ths  行业板块资金流（net_amount，单位亿元）
"""

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from utils.stock_data_fetcher import _tushare_limiter
from utils.tushare_client import get_tushare_pro

logger = logging.getLogger(__name__)


class SectorHotRankFetcher:
    """热门板块数据采集与落库"""

    def __init__(self, db_manager):
        """
        参数：
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager

    def fetch_and_store(self, trade_date: str) -> int:
        """
        抓取指定交易日热门板块数据并写入 sector_hot_rank

        参数：
            trade_date: 交易日期，格式 YYYY-MM-DD

        返回：
            写入条数；任一数据源失败返回 0（整体跳过本次落库，数据停留在上一交易日）
        """
        ts_date = trade_date.replace('-', '')
        try:
            df_index = self._fetch_sector_index()
            df_daily = self._fetch_sector_daily(ts_date)
            df_flow = self._fetch_sector_moneyflow(ts_date)

            if df_index is None or df_index.empty \
                    or df_daily is None or df_daily.empty \
                    or df_flow is None:
                logger.warning(f"热门板块数据不完整（trade_date={trade_date}），跳过本次落库")
                return 0

            return self._merge_and_store(trade_date, df_index, df_daily, df_flow)
        except Exception as e:
            logger.error(f"热门板块数据落库失败: {trade_date}, {e}")
            return 0

    def _fetch_sector_index(self) -> Optional[pd.DataFrame]:
        """获取同花顺板块清单（全量，仅保留概念N与行业I）"""
        try:
            _tushare_limiter.wait_if_needed()
            pro = get_tushare_pro()
            if pro is None:
                logger.error("未配置Tushare api_key")
                return None
            df = pro.ths_index()
            if df is not None and not df.empty and 'type' in df.columns:
                df = df[df['type'].isin(['N', 'I'])]
            return df
        except Exception as e:
            logger.error(f"获取板块清单失败: {e}")
            return None

    def _fetch_sector_daily(self, ts_date: str) -> Optional[pd.DataFrame]:
        """获取指定交易日全部板块日行情"""
        try:
            _tushare_limiter.wait_if_needed()
            pro = get_tushare_pro()
            if pro is None:
                logger.error("未配置Tushare api_key")
                return None
            return pro.ths_daily(trade_date=ts_date)
        except Exception as e:
            logger.error(f"获取板块行情失败: {ts_date}, {e}")
            return None

    def _fetch_sector_moneyflow(self, ts_date: str) -> Optional[pd.DataFrame]:
        """获取指定交易日概念+行业板块主力资金流"""
        try:
            pro = get_tushare_pro()
            if pro is None:
                logger.error("未配置Tushare api_key")
                return None

            frames = []
            for api in (pro.moneyflow_cnt_ths, pro.moneyflow_ind_ths):
                try:
                    _tushare_limiter.wait_if_needed()
                    df = api(trade_date=ts_date)
                    if df is not None and not df.empty:
                        frames.append(df)
                except Exception as e:
                    logger.warning(f"获取板块资金流失败: {ts_date}, {e}")

            if not frames:
                return None
            return pd.concat(frames, ignore_index=True)
        except Exception as e:
            logger.error(f"获取板块资金流失败: {ts_date}, {e}")
            return None

    def _merge_and_store(self, trade_date: str, df_index: pd.DataFrame,
                         df_daily: pd.DataFrame, df_flow: pd.DataFrame) -> int:
        """按板块代码拼装三份数据并写入数据库"""
        # 板块代码 → (名称, 类型)
        index_map = {}
        for _, row in df_index.iterrows():
            code = str(row.get('ts_code', '')).strip()
            name = str(row.get('name', '')).strip()
            sec_type = 'concept' if row.get('type') == 'N' else 'industry'
            if code and name:
                index_map[code] = (name, sec_type)

        # 板块代码 → 主力净流入（net_amount 单位亿元，换算为元）
        flow_map = {}
        for _, row in df_flow.iterrows():
            code = str(row.get('ts_code', '')).strip()
            if code and code not in flow_map:
                try:
                    flow_map[code] = float(row.get('net_amount', 0) or 0) * 1e8
                except (TypeError, ValueError):
                    flow_map[code] = 0.0

        saved_count = 0
        # 整批写入使用显式事务：execute_with_retry 不 commit，
        # 若不手动管理会导致悬挂写事务（与 fund_flow_fetcher 落库写法一致）
        self.db_manager.begin_transaction()
        try:
            pct_col = 'pct_chg' if 'pct_chg' in df_daily.columns else 'pct_change'
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for _, row in df_daily.iterrows():
                code = str(row.get('ts_code', '')).strip()
                if code not in index_map:
                    continue
                name, sec_type = index_map[code]
                pct_chg = row.get(pct_col)
                main_net_flow = flow_map.get(code, 0.0)

                # 同一交易日重跑按 UNIQUE(trade_date, sector_code) 覆盖更新
                check_sql = """
                    SELECT id FROM sector_hot_rank
                    WHERE trade_date = ? AND sector_code = ?
                """
                existing = self.db_manager.query_one(check_sql, (trade_date, code))
                if existing:
                    update_sql = """
                        UPDATE sector_hot_rank
                        SET sector_name = ?, sector_type = ?, pct_chg = ?,
                            main_net_flow = ?, created_date = ?
                        WHERE trade_date = ? AND sector_code = ?
                    """
                    self.db_manager.execute_with_retry(update_sql, (
                        name, sec_type, pct_chg, main_net_flow, now_str,
                        trade_date, code
                    ))
                else:
                    insert_sql = """
                        INSERT INTO sector_hot_rank
                        (trade_date, sector_code, sector_name, sector_type,
                         pct_chg, main_net_flow, created_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """
                    self.db_manager.execute_with_retry(insert_sql, (
                        trade_date, code, name, sec_type,
                        pct_chg, main_net_flow, now_str
                    ))
                saved_count += 1

            self.db_manager.commit()
            logger.info(f"热门板块数据保存完成: {trade_date}, {saved_count} 条")
        except Exception as e:
            try:
                self.db_manager.rollback()
            except Exception:
                pass
            logger.error(f"热门板块数据写入失败: {trade_date}, {e}")
            return 0

        return saved_count
```

- [ ] **步骤 2：语法检查**

运行：`python -m py_compile utils/sector_hot_rank_fetcher.py`
预期：无输出，退出码 0

- [ ] **步骤 3：手动触发落库并查库验证**

运行：`python tmp_verify_hot_sectors.py fetch 2026-09-18`（2026-09-18 为最近一个交易日周五；如当天非交易日则改用上一个真实交易日）
预期输出示例：

```
写入条数: 400+
concept: 300+ 条
industry: 90+ 条
唯一性检查（应无输出）: []

概念板块 Top5（按主力净流入降序）:
  xxx  x.xx%  x.xx亿
```

再重跑一次同一命令，预期：`写入条数` 与首次相同（覆盖更新而非重复插入），唯一性检查仍无输出。

- [ ] **步骤 4：Commit**

```bash
git add utils/sector_hot_rank_fetcher.py
git commit -m "feat: 新增热门板块数据采集器（Tushare同花顺接口落库）"
```

---

### 任务 3：daily_update 挂钩热门板块更新

**文件：**
- 修改：`utils/akshare_fetcher.py:397-402`（第5步 record_update_complete 块之后、return 之前）

- [ ] **步骤 1：插入第 6 步**

将以下代码：

```python
        source_not_ready = '数据源尚未就绪' in kline_result.get('message', '')
        if kline_result.get('success') and not source_not_ready:
            validator.record_update_complete(target_date, {
                'kline_added': kline_result.get('added', 0),
                'kline_updated': kline_result.get('updated', 0),
            })

        return {
```

替换为：

```python
        source_not_ready = '数据源尚未就绪' in kline_result.get('message', '')
        if kline_result.get('success') and not source_not_ready:
            validator.record_update_complete(target_date, {
                'kline_added': kline_result.get('added', 0),
                'kline_updated': kline_result.get('updated', 0),
            })

        # 第6步：更新热门板块数据（独立落库，失败不阻塞K线更新结果）
        try:
            from utils.sector_hot_rank_fetcher import SectorHotRankFetcher
            hot_rank_saved = SectorHotRankFetcher(self.db_manager).fetch_and_store(target_date)
            logger.info(f"热门板块数据更新完成: {hot_rank_saved} 条")
        except Exception as e:
            logger.warning(f"热门板块数据更新失败（不影响主流程）: {e}")

        return {
```

- [ ] **步骤 2：语法检查**

运行：`python -m py_compile utils/akshare_fetcher.py`
预期：无输出，退出码 0

- [ ] **步骤 3：Commit**

```bash
git add utils/akshare_fetcher.py
git commit -m "feat: daily_update 挂钩热门板块数据每日落库"
```

---

### 任务 4：web_server 新增 hot-sectors 接口并删除 4 个旧接口

**文件：**
- 修改：`web_server.py:393-658`（hot-industries / hot-areas / industry-stocks / area-stocks 四个端点整体删除，原位置写入新端点）

- [ ] **步骤 1：删除四个旧接口**

删除以下完整代码块（从 `@app.route('/api/dashboard/hot-industries')` 起，到 `get_area_stocks` 的 `return jsonify({'success': False, 'error': str(e)})` 止，即 L393-658）：

- `get_hot_industries()`（`/api/dashboard/hot-industries`）
- `get_hot_areas()`（`/api/dashboard/hot-areas`）
- `get_industry_stocks()`（`/api/dashboard/industry-stocks`）
- `get_area_stocks()`（`/api/dashboard/area-stocks`）

保留其后的注释行 `# ==================== 股票收藏夹相关接口 ====================` 及以下内容。

- [ ] **步骤 2：在删除位置写入新接口**

```python
@app.route('/api/dashboard/hot-sectors')
def get_hot_sectors():
    """获取热门板块 - 最近有数据交易日（自动回退）的概念/行业板块按主力净流入额排名"""
    try:
        sector_type = request.args.get('type', 'concept')
        if sector_type not in ('concept', 'industry'):
            return jsonify({'success': False, 'error': 'type 参数必须是 concept 或 industry'}), 400

        # 最近有数据的交易日（表无数据时返回空列表）
        row = db_manager.query_one("SELECT MAX(trade_date) AS max_date FROM sector_hot_rank")
        if not row or not row['max_date']:
            return jsonify({'success': True, 'date': '', 'type': sector_type, 'sectors': []})
        trade_date = row['max_date']

        rows = db_manager.query("""
            SELECT sector_code, sector_name, pct_chg, main_net_flow
            FROM sector_hot_rank
            WHERE trade_date = ? AND sector_type = ?
            ORDER BY main_net_flow DESC
            LIMIT 5
        """, (trade_date, sector_type))

        sectors = []
        for idx, r in enumerate(rows, start=1):
            sectors.append({
                'rank': idx,
                'sector_code': r['sector_code'],
                'sector_name': r['sector_name'],
                'pct_chg': r['pct_chg'],
                'main_net_flow': r['main_net_flow']
            })

        return jsonify({
            'success': True,
            'date': trade_date,
            'type': sector_type,
            'sectors': sectors
        })
    except Exception as e:
        logger.error(f"获取热门板块失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
```

- [ ] **步骤 3：语法检查**

运行：`python -m py_compile web_server.py`
预期：无输出，退出码 0

- [ ] **步骤 4：启动服务并 curl 验证**

启动：`python web_server.py`（非阻塞）
确认旧接口已删、新接口正常：

```bash
curl.exe "http://localhost:5001/api/dashboard/hot-sectors?type=concept"
```
预期：`{"success": true, "date": "2026-09-18", "type": "concept", "sectors": [{"rank": 1, ...5条，main_net_flow 降序...}]}`

```bash
curl.exe "http://localhost:5001/api/dashboard/hot-sectors?type=industry"
```
预期：结构同上，type 为 industry

```bash
curl.exe -s -o NUL -w "%{http_code}" "http://localhost:5001/api/dashboard/hot-sectors?type=bad"
```
预期：`400`

```bash
curl.exe -s -o NUL -w "%{http_code}" "http://localhost:5001/api/dashboard/hot-industries"
```
预期：`404`

验证后停止服务。

- [ ] **步骤 5：Commit**

```bash
git add web_server.py
git commit -m "feat: 新增热门板块接口 hot-sectors，删除旧最热行业/板块及其弹窗接口"
```

---

### 任务 5：stocks.js 删除旧函数并实现 loadHotSectors

**文件：**
- 修改：`web/static/js/modules/stocks.js`

- [ ] **步骤 1：删除 showIndustryStocks / showAreaStocks（先删文件后部的，避免行号偏移）**

删除 L567-605 两个完整函数及其注释块：

- `/**\n * 显示行业股票列表...` 到 `showAreaStocks` 函数结束（`alert('加载板块股票失败: ' + error.message);\n    }\n}`）
- 保留其后的 `showStocksModal` 函数

- [ ] **步骤 2：删除 loadHotIndustries 与 loadHotAreas，原位置写入新实现**

删除 L99-245 两个完整函数及其注释块（从 `/**\n * 加载最热行业数据\n */` 到 `loadHotAreas` 函数结束）。

在该位置写入：

```javascript
/**
 * 热门板块当前选中类型：concept 概念 / industry 行业
 */
let hotSectorType = 'concept';

/**
 * 格式化主力净流入热度：≥1亿 显示 X.XX亿，否则 XXXX万（负值同样格式化）
 * @param {number} value - 主力净流入额（元）
 */
function formatHotValue(value) {
    if (value === null || value === undefined || isNaN(value)) {
        return '-';
    }
    const abs = Math.abs(value);
    if (abs >= 1e8) {
        return (value / 1e8).toFixed(2) + '亿';
    }
    return (value / 1e4).toFixed(0) + '万';
}

/**
 * 格式化涨跌幅：保留2位小数，红涨绿跌（A股配色）
 * @param {number} pct - 涨跌幅 %
 */
function formatPctChg(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) {
        return '<span class="text-muted">-</span>';
    }
    const colorClass = pct > 0 ? 'text-danger' : (pct < 0 ? 'text-success' : 'text-muted');
    return `<span class="${colorClass}">${pct.toFixed(2)}%</span>`;
}

/**
 * 渲染热门板块卡片内容（tab + 榜单表格 + 数据日期）
 * @param {Object|null} result - 接口返回数据，null 表示加载失败
 */
function renderHotSectors(result) {
    const activeStyle = 'padding: 4px 12px; margin-right: 8px; font-size: 13px; border-radius: 4px; border: 1px solid #e74c3c; background: #fdecec; color: #e74c3c; font-weight: bold; cursor: pointer;';
    const normalStyle = 'padding: 4px 12px; margin-right: 8px; font-size: 13px; border-radius: 4px; border: 1px solid #ddd; background: #f5f5f5; color: #666; cursor: pointer;';
    const tabs = `
        <div style="margin-bottom: 8px;">
            <button type="button" style="${hotSectorType === 'concept' ? activeStyle : normalStyle}" onclick="loadHotSectors('concept')">概念板块</button>
            <button type="button" style="${hotSectorType === 'industry' ? activeStyle : normalStyle}" onclick="loadHotSectors('industry')">行业板块</button>
        </div>
    `;

    if (!result || result.success === false) {
        return tabs + '<p class="text-muted">暂无板块数据</p>';
    }

    const sectors = result.sectors || [];
    if (sectors.length === 0) {
        return tabs + '<p class="text-muted">暂无板块数据</p>';
    }

    // 前三名排名角标颜色（同花顺样式：红/橙/黄）
    const rankColors = { 1: '#e74c3c', 2: '#e67e22', 3: '#f1c40f' };

    let html = tabs + `
        <div class="table-responsive">
            <table class="table" style="margin-bottom: 5px;">
                <thead>
                    <tr>
                        <th style="width: 50px;">排名</th>
                        <th>板块名称</th>
                        <th style="text-align: right;">涨幅</th>
                        <th style="text-align: right;">热度</th>
                    </tr>
                </thead>
                <tbody>
    `;

    sectors.forEach(sector => {
        const rankBadge = rankColors[sector.rank]
            ? `<span style="display:inline-block;min-width:20px;text-align:center;border-radius:4px;color:#fff;font-weight:bold;padding:1px 4px;background:${rankColors[sector.rank]};">${sector.rank}</span>`
            : `<span class="text-muted" style="display:inline-block;min-width:20px;text-align:center;">${sector.rank}</span>`;
        const code = (sector.sector_code || '').replace('.TI', '');
        html += `
            <tr>
                <td>${rankBadge}</td>
                <td>
                    <div>${sector.sector_name}</div>
                    <div class="text-muted" style="font-size: 12px;">${code}</div>
                </td>
                <td style="text-align: right;">${formatPctChg(sector.pct_chg)}</td>
                <td style="text-align: right;">${formatHotValue(sector.main_net_flow)}</td>
            </tr>
        `;
    });

    html += `
                </tbody>
            </table>
        </div>
        <p class="text-muted" style="font-size: 12px;">数据日期: ${result.date || '-'}</p>
    `;

    return html;
}

/**
 * 加载热门板块数据（最近交易日，按主力净流入排名，前5条）
 * @param {string} type - 板块类型：concept 概念 / industry 行业
 */
export async function loadHotSectors(type = hotSectorType) {
    hotSectorType = type;
    const container = document.getElementById('hot-sectors-content');
    if (!container) {
        return;
    }
    container.innerHTML = '<p class="text-muted">加载中...</p>';
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时

        const response = await fetch(`/api/dashboard/hot-sectors?type=${type}`, { signal: controller.signal });
        clearTimeout(timeoutId);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const result = await response.json();

        container.innerHTML = renderHotSectors(result);
    } catch (error) {
        console.error('加载热门板块失败:', error);
        container.innerHTML = renderHotSectors(null);
    }
}
```

- [ ] **步骤 3：Commit**

```bash
git add web/static/js/modules/stocks.js
git commit -m "feat: 前端实现热门板块榜单加载与渲染（概念/行业tab切换）"
```

---

### 任务 6：index.html 卡片改造 + app.js 清理 + 版本号 bump

**文件：**
- 修改：`web/templates/index.html:442-456`（两张旧卡片）、`index.html:2179`（版本号）
- 修改：`web/static/js/app.js:111-112`（调用）、`app.js:150-151`（window 挂载）

- [ ] **步骤 1：index.html 删除【最热行业】卡片并重写【最热板块】卡片**

将以下代码（L442-456）：

```html
                    <!-- 最热行业 -->
                    <div id="hot-industries-card" class="card">
                        <h3>📊 最热行业</h3>
                        <div id="hot-industries-content" class="card-body">
                            <p class="text-muted">暂无行业数据</p>
                        </div>
                    </div>

                    <!-- 最热板块 -->
                    <div id="hot-areas-card" class="card">
                        <h3>📍 最热板块</h3>
                        <div id="hot-areas-content" class="card-body">
                            <p class="text-muted">暂无板块数据</p>
                        </div>
                    </div>
```

替换为：

```html
                    <!-- 热门板块 -->
                    <div id="hot-sectors-card" class="card">
                        <h3>🔥 热门板块</h3>
                        <div id="hot-sectors-content" class="card-body">
                            <p class="text-muted">暂无板块数据</p>
                        </div>
                    </div>
```

- [ ] **步骤 2：index.html bump app.js 版本号**

将 L2179：

```html
    <script type="module" src="/static/js/app.js?v=15"></script>
```

替换为：

```html
    <script type="module" src="/static/js/app.js?v=16"></script>
```

- [ ] **步骤 3：app.js 更新调用与 window 挂载**

将 L111-112：

```javascript
    modules.stocks.loadHotIndustries();
    modules.stocks.loadHotAreas();
```

替换为：

```javascript
    modules.stocks.loadHotSectors();
```

将 L52 的模块加载增加版本参数（防缓存）：

```javascript
        const stocksModule = await import('./modules/stocks.js');
```

替换为：

```javascript
        const stocksModule = await import('./modules/stocks.js?v=2');
```

将 L150-151：

```javascript
    window.showIndustryStocks = modules.stocks.showIndustryStocks;
    window.showAreaStocks = modules.stocks.showAreaStocks;
```

替换为：

```javascript
    window.loadHotSectors = modules.stocks.loadHotSectors;
```

（`window.loadHotSectors` 是 tab 按钮 onclick 调用的全局入口，必须挂载。）

- [ ] **步骤 4：Commit**

```bash
git add web/templates/index.html web/static/js/app.js
git commit -m "feat: 仪表盘热门板块卡片改版（同花顺样式），移除旧最热行业/板块卡片"
```

---

### 任务 7：端到端验证与清理

**文件：**
- 删除：`tmp_verify_hot_sectors.py`

- [ ] **步骤 1：启动服务**

启动：`python web_server.py`（非阻塞，端口 5001）

- [ ] **步骤 2：浏览器验证**

打开 `http://localhost:5001`，逐项确认：

1. 旧【最热行业】卡片已消失，【我的金股】下方直接是【🔥 热门板块】卡片
2. 卡片默认显示概念板块 tab 高亮，5 条记录，前三名角标红/橙/黄
3. 板块名称下方灰色小字代码（如 886015），涨幅红涨绿跌保留 2 位小数，热度列 `X.XX亿` / `XXXX万`
4. 点击【行业板块】tab 重新请求并渲染行业数据，tab 高亮切换
5. 底部数据日期与接口返回 date 一致
6. 浏览器控制台无 404、无报错（重点确认旧接口 hot-industries/hot-areas 无残留调用）

- [ ] **步骤 3：curl 复核两个 tab 的接口数据与页面展示一致**

```bash
curl.exe "http://localhost:5001/api/dashboard/hot-sectors?type=industry"
```

- [ ] **步骤 4：删除临时验证脚本**

删除 `tmp_verify_hot_sectors.py`（任务 1 创建）。

- [ ] **步骤 5：Commit**

```bash
git add -u
git commit -m "chore: 删除热门板块临时验证脚本"
```

（若 `git add -u` 无变更可跳过 commit。）

- [ ] **步骤 6：停止服务**

验证完成后停止 web_server。
