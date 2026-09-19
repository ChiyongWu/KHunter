# 热门板块功能改版设计

日期：2026-09-20
状态：已确认

## 背景与目标

仪表盘现有【最热板块】卡片显示的是"当日选股记录 top50 的板块计数分布"，与真实板块行情无关，板块缺失时显示"未知"。参照同花顺 APP 的"热门板块"样式改版为：显示最近一个交易日全市场热门概念/行业板块榜单，列为排名、板块名称（含代码）、涨幅、热度。

已确认的决策：
- **热度指标**：主力净流入额（同花顺专有人气数据无法获取，用资金关注度替代）
- **数据时机**：每日落库，接口只查库不做实时请求
- **板块类型**：概念板块 / 行业板块 tab 切换（默认概念，同花顺一致）
- **数据源**：Tushare 同花顺接口（ths_index / ths_daily / moneyflow_cnt_ths / moneyflow_ind_ths）
- **旧【最热行业】卡片**：删除

## 数据链路与落库

### 新表 `sector_hot_rank`

```sql
CREATE TABLE IF NOT EXISTS sector_hot_rank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date DATE NOT NULL,              -- 交易日
    sector_code VARCHAR(20) NOT NULL,      -- 同花顺板块代码，如 885823.TI
    sector_name VARCHAR(50) NOT NULL,      -- 板块名称，如 创新药
    sector_type VARCHAR(10) NOT NULL,      -- concept 概念 / industry 行业
    pct_chg REAL,                          -- 板块涨跌幅 %
    main_net_flow REAL,                    -- 主力净流入额（元）
    created_date DATETIME,
    UNIQUE(trade_date, sector_code)
);
```

同一交易日重跑按 `UNIQUE(trade_date, sector_code)` 覆盖更新。

### 每日更新流程（挂在 `AKShareFetcher.daily_update` 链路，utils/akshare_fetcher.py:307）

1. `pro.ths_index` 取全量板块清单及类型（type N=概念 / I=行业），参考 sector_scorer.py `_get_sector_name_map()`（L372-417）
2. `pro.ths_daily(trade_date=...)` 取当日全部板块涨跌幅，参考 sector_scorer.py `_fetch_sector_daily()`（L419-477）
3. 概念板块资金流 `pro.moneyflow_cnt_ths(trade_date=...)`、行业板块资金流 `pro.moneyflow_ind_ths(trade_date=...)` 取主力净流入，参考 fund_flow_fetcher.py 现有调用（L237、L165）
4. 三者按板块代码拼装写入 `sector_hot_rank`（独立连接 + 显式 commit 模式，与 fund_flow_fetcher 落库写法一致）

约 3-4 次 Tushare 请求/日。**不依赖** `stock_sector` / `sector_fund_flow` / `industry_fund_flow` 等既有表，自包含。

### 排序与格式化

- "热门"排序：按 `main_net_flow` 降序（同花顺按热度排序，此处热度即主力净流入）
- 热度列格式化：`≥1亿` 显示 `X.XX亿`，否则 `XXXX万`；负值同样格式化
- 涨幅保留 2 位小数，红涨绿跌

## 后端接口

### 新增 `GET /api/dashboard/hot-sectors?type=concept|industry`

```json
{
  "success": true,
  "date": "2026-09-18",
  "type": "concept",
  "sectors": [
    {"rank": 1, "sector_code": "886015.TI", "sector_name": "创新药",
     "pct_chg": 1.27, "main_net_flow": 208000000}
  ]
}
```

- 查询 `sector_hot_rank` 中 `MAX(trade_date)`（自动回退最近交易日）→ 该日按 `main_net_flow` 降序取前 5
- `type` 参数缺省为 `concept`，非法值返回 400

### 删除旧接口

- `GET /api/dashboard/hot-industries`（web_server.py:393-446）
- `GET /api/dashboard/hot-areas`（web_server.py:449-502）
- `GET /api/dashboard/industry-stocks`、`GET /api/dashboard/area-stocks`（弹窗接口，前端无入口后一并删除）

已核实：`showIndustryStocks` / `showAreaStocks` 仅被两张旧卡片调用，无其他引用。

## 前端

### 删除

- 【最热行业】卡片（index.html L442-448）
- `loadHotIndustries()`（stocks.js:102-171）及 app.js:111 调用
- `showIndustryStocks()` / `showAreaStocks()`（stocks.js:572-604）及 app.js:150-151 window 挂载
- `loadHotAreas()` 旧实现（stocks.js:176-245）重写为热门板块加载逻辑，app.js:112 调用保留（改调新函数）

### 【热门板块】卡片

- 标题：`🔥 热门板块`（沿用卡片风格）
- 顶部 tab：`概念板块` | `行业板块`，默认概念板块；切换重新请求
- 表格四列：
  - **排名**：数字角标，前三名红/橙/黄高亮（同花顺样式）
  - **板块名称**：名称 + 下方灰色小字代码（`886015.TI` 显示为 `886015`）
  - **涨幅**：红涨绿跌（A 股配色），保留 2 位小数
  - **热度**：主力净流入格式化（`2.08亿` / `3560万`）
- 每类显示前 5 条，底部显示数据日期
- 保留现有 AbortController 5 秒超时 + 失败占位文案模式
- 静态资源版本号 bump（index.html 的 app.js `?v=` 递增、app.js 内模块 import 版本参数递增）防缓存

## 错误处理

- 表无数据（daily_update 从未跑过）：接口返回 `success: true, sectors: []`，前端显示"暂无板块数据"
- Tushare 单接口失败：整体跳过本次落库并记日志（与现有 daily_update 各子项失败不互相阻塞的策略一致），接口侧表现为数据停留在上一交易日
- 前端请求失败/超时：显示"暂无板块数据"，不自动重试（与现状一致）

## 不做的事（YAGNI）

- 不做"更多"跳转页（同花顺有，仪表盘卡片不需要）
- 不做历史榜单回溯查询接口
- 不做实时刷新，数据随 daily_update 更新
- 不动 `stock_sector` / `sector_fund_flow` / `industry_fund_flow` 既有表和评分链路

## 验证方式

1. 手动触发一次板块数据落库，查库确认 `sector_hot_rank` 概念/行业记录数与主键唯一性
2. curl 请求 `/api/dashboard/hot-sectors?type=concept` 与 `type=industry`，确认返回结构与降序正确
3. 浏览器打开仪表盘：确认旧【最热行业】卡片消失、新卡片 tab 切换正常、涨幅红色/绿色正确、数据日期正确
4. 控制台无 404（旧接口删除后无残留调用）
