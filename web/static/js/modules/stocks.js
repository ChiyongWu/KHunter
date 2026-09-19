/**
 * 股票相关功能模块
 */

/**
 * 加载统计信息
 */
export async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const result = await response.json();
        
        if (result.success) {
            document.getElementById('stat-stocks').textContent = result.data.total_stocks;
            document.getElementById('stat-date').textContent = result.data.latest_date;
            document.getElementById('stat-strategies').textContent = result.data.strategies;
        }
    } catch (error) {
        console.error('加载统计信息失败:', error);
    }
}

/**
 * 加载我的金股数据
 */
export async function loadMyGoldenStocks() {
    const container = document.getElementById('my-golden-stocks-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/my-golden-stocks', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
            return;
        }
        
        // 如果success为true或result中有stocks数据
        if (result.stocks && result.stocks.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>股票代码</th>
                                <th>股票名称</th>
                                <th>评分</th>
                                <th>行业</th>
                                <th>板块</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            result.stocks.forEach((stock, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td><a href="javascript:void(0)" onclick="viewStockDetail('${stock.stock_code}')" class="stock-link">${stock.stock_code}</a></td>
                        <td>${stock.stock_name}</td>
                        <td><a href="javascript:void(0)" onclick="showScoreDetail('${stock.stock_code}', '${result.date}')" class="score-link">${(stock.total_score || 0).toFixed(2)}</a></td>
                        <td>${stock.industry || '-'}</td>
                        <td>${stock.area || '-'}</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        }
    } catch (error) {
        console.error('加载我的金股失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无金股数据</p>';
        }
    }
}

/**
 * 加载最热行业数据
 */
export async function loadHotIndustries() {
    const container = document.getElementById('hot-industries-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/hot-industries', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
            return;
        }
        
        // 如果success为true或result中有industries数据
        if (result.industries && result.industries.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>行业</th>
                                <th>股票数量</th>
                                <th>占比</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            // 只显示前5个行业
            const top5Industries = result.industries.slice(0, 5);
            top5Industries.forEach((industry, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td>${industry.industry}</td>
                        <td><a href="javascript:void(0)" onclick="showIndustryStocks('${industry.industry}', ${industry.count})" class="stock-link">${industry.count}</a></td>
                        <td>${industry.percentage}%</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        }
    } catch (error) {
        console.error('加载最热行业失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无行业数据</p>';
        }
    }
}

/**
 * 加载最热板块数据
 */
export async function loadHotAreas() {
    const container = document.getElementById('hot-areas-content');
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000); // 5秒超时
        
        const response = await fetch('/api/dashboard/hot-areas', { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const result = await response.json();
        
        // 检查result是否为空或没有success字段
        if (!result || (result.success === false)) {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
            return;
        }
        
        // 如果success为true或result中有areas数据
        if (result.areas && result.areas.length > 0) {
            let html = `
                <div class="table-responsive">
                    <table class="table table-striped">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>板块</th>
                                <th>股票数量</th>
                                <th>占比</th>
                            </tr>
                        </thead>
                        <tbody>
            `;
            
            // 只显示前5个板块
            const top5Areas = result.areas.slice(0, 5);
            top5Areas.forEach((area, index) => {
                html += `
                    <tr>
                        <td>${index + 1}</td>
                        <td>${area.area}</td>
                        <td><a href="javascript:void(0)" onclick="showAreaStocks('${area.area}', ${area.count})" class="stock-link">${area.count}</a></td>
                        <td>${area.percentage}%</td>
                    </tr>
                `;
            });
            
            html += `
                        </tbody>
                    </table>
                </div>
                <p class="text-muted" style="margin-top: 10px; font-size: 12px;">数据日期: ${result.date}</p>
            `;
            
            container.innerHTML = html;
        } else {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        }
    } catch (error) {
        console.error('加载最热板块失败:', error);
        if (error.name === 'AbortError') {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        } else {
            container.innerHTML = '<p class="text-muted">暂无板块数据</p>';
        }
    }
}

/**
 * 股票列表分页状态
 */
let stocksPageState = {
    page: 1,
    perPage: 20,
    totalPages: 1,
    total: 0,
    keyword: '',
    searchTimer: null,
};

/**
 * 股票页事件绑定守卫（避免每次渲染重复绑定监听器）
 */
let stocksEventsBound = false;

/**
 * 绑定搜索与每页条数事件（仅绑定一次）
 */
function bindStocksEvents() {
    if (stocksEventsBound) return;
    stocksEventsBound = true;

    // 搜索：防抖后重置到第 1 页，服务端过滤（股票代码/名称）
    const searchInput = document.getElementById('stock-search');
    searchInput.addEventListener('input', (e) => {
        clearTimeout(stocksPageState.searchTimer);
        stocksPageState.searchTimer = setTimeout(() => {
            stocksPageState.keyword = e.target.value.trim();
            loadStocks(1);
        }, 300);
    });

    // 每页条数切换
    const perPageSelect = document.getElementById('stocks-per-page');
    perPageSelect.addEventListener('change', (e) => {
        stocksPageState.perPage = parseInt(e.target.value, 10) || 20;
        loadStocks(1);
    });
}

/**
 * 加载股票列表（服务端分页，每次仅请求当前页）
 */
export async function loadStocks(page = 1) {
    bindStocksEvents();

    const tbody = document.getElementById('stocks-tbody');
    stocksPageState.page = page;
    tbody.innerHTML = '<tr><td colspan="7" class="loading">正在加载股票列表...</td></tr>';

    try {
        const keyword = encodeURIComponent(stocksPageState.keyword || '');
        const response = await fetch(`/api/stocks?page=${page}&per_page=${stocksPageState.perPage}&keyword=${keyword}`);
        const result = await response.json();

        if (result.success) {
            stocksPageState.total = result.total;
            stocksPageState.totalPages = result.total_pages;
            renderStocks(result.data);
            renderStocksPagination();
        } else {
            tbody.innerHTML = `<tr><td colspan="7" class="loading">加载失败: ${result.error || '未知错误'}</td></tr>`;
        }
    } catch (error) {
        tbody.innerHTML = `<tr><td colspan="7" class="loading">加载失败: ${error.message}</td></tr>`;
    }
}

/**
 * 渲染股票列表（当前页数据）
 * @param {Array} stocks - 股票列表数据
 */
export function renderStocks(stocks) {
    const tbody = document.getElementById('stocks-tbody');

    if (stocks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="loading">暂无数据</td></tr>';
        return;
    }

    tbody.innerHTML = stocks.map(stock => `
        <tr>
            <td><strong>${stock.code}</strong></td>
            <td>${stock.name}</td>
            <td>¥${stock.latest_price}</td>
            <td>${stock.latest_date}</td>
            <td>${stock.market_cap}</td>
            <td>${stock.data_count}</td>
            <td>
                <button class="btn btn-secondary" onclick="viewStockDetail('${stock.code}')">
                    查看
                </button>
            </td>
        </tr>
    `).join('');
}

/**
 * 渲染股票列表分页控件（页码窗口 ±2 + 首末页/省略号 + 页信息）
 */
function renderStocksPagination() {
    const container = document.getElementById('stocks-pagination');
    if (!container) return;

    const { page, perPage, totalPages, total } = stocksPageState;

    if (totalPages <= 1) {
        container.style.display = 'none';
        return;
    }
    container.style.display = 'flex';

    const start = (page - 1) * perPage + 1;
    const end = Math.min(page * perPage, total);

    const btn = (label, target, opts = {}) => {
        const disabled = opts.disabled ? 'disabled' : '';
        const active = opts.active ? ' class="active"' : '';
        return `<button${active}${disabled} data-page="${target}">${label}</button>`;
    };

    let html = `<span id="stocks-page-info">第 ${start}-${end} 条，共 ${total} 只</span>`;

    // 上一页
    html += btn('← 上一页', page - 1, { disabled: page <= 1 });

    // 页码窗口（当前页 ±2，含首页/末页与省略号）
    const startPage = Math.max(1, page - 2);
    const endPage = Math.min(totalPages, page + 2);
    if (startPage > 1) {
        html += btn('1', 1);
        if (startPage > 2) html += btn('...', 0, { disabled: true });
    }
    for (let i = startPage; i <= endPage; i++) {
        html += btn(String(i), i, { active: i === page });
    }
    if (endPage < totalPages) {
        if (endPage < totalPages - 1) html += btn('...', 0, { disabled: true });
        html += btn(String(totalPages), totalPages);
    }

    // 下一页
    html += btn('下一页 →', page + 1, { disabled: page >= totalPages });

    container.innerHTML = html;

    // 绑定页码点击
    container.querySelectorAll('button[data-page]').forEach(b => {
        b.addEventListener('click', () => {
            const target = parseInt(b.dataset.page, 10);
            if (target >= 1 && target <= stocksPageState.totalPages) {
                loadStocks(target);
            }
        });
    });
}

/**
 * 当前查看的股票代码（用于收藏功能）
 */
let currentStockCode = '';

/**
 * 查看股票详情
 * @param {string} code - 股票代码
 */
export async function viewStockDetail(code) {
    try {
        const response = await fetch(`/api/stock/${code}`);
        const result = await response.json();
        
        if (result.success) {
            showStockModal(code, result.data);
        } else {
            alert('加载股票详情失败: ' + result.error);
        }
    } catch (error) {
        alert('加载股票详情失败: ' + error.message);
    }
}

/**
 * 显示股票详情弹窗
 * @param {string} code - 股票代码
 * @param {Object} data - 股票数据
 */
export function showStockModal(code, data) {
    currentStockCode = code;
    const modal = document.getElementById('stock-modal');
    document.getElementById('stock-detail-modal-title').textContent = `股票详情: ${code}`;
    
    // 检查收藏状态并更新按钮
    updateFavoriteButton(code);
    
    // 显示K线图表容器
    const chartContainer = document.getElementById('stock-chart-container');
    chartContainer.style.display = 'block';
    
    // 清空股票信息区域，只显示K线图表
    document.getElementById('stock-info').innerHTML = '';
    
    // 先显示模态框，让容器获得正确的尺寸
    modal.classList.add('active');
    
    // 使用requestAnimationFrame确保DOM已更新，容器有正确的宽度
    requestAnimationFrame(() => {
        // 初始化K线图表
        // 注意：使用stock-chart-container而不是stock-chart（canvas元素）
        initKlineChart('stock-chart-container', data);
    });
}

/**
 * 更新收藏按钮状态
 * @param {string} code - 股票代码
 */
export async function updateFavoriteButton(code) {
    const btn = document.getElementById('favorite-btn');
    if (!btn) return;
    try {
        const response = await fetch(`/api/stock/favorite/${code}`);
        const result = await response.json();
        if (result.success && result.favorited) {
            btn.textContent = '⭐';
            btn.classList.add('active');
            btn.title = '取消收藏';
        } else {
            btn.textContent = '☆';
            btn.classList.remove('active');
            btn.title = '收藏';
        }
    } catch (error) {
        console.error('检查收藏状态失败:', error);
        btn.textContent = '☆';
        btn.classList.remove('active');
    }
}

/**
 * 切换收藏状态（供 onclick 调用的全局函数）
 */
window.toggleFavorite = async function() {
    const code = currentStockCode;
    if (!code) return;
    const btn = document.getElementById('favorite-btn');
    const isActive = btn.classList.contains('active');
    
    try {
        if (isActive) {
            // 取消收藏
            const response = await fetch(`/api/stock/favorite/${code}`, { method: 'DELETE' });
            const result = await response.json();
            if (result.success) {
                btn.textContent = '☆';
                btn.classList.remove('active');
                btn.title = '收藏';
            } else {
                alert('取消收藏失败: ' + (result.error || ''));
            }
        } else {
            // 添加收藏
            const response = await fetch('/api/stock/favorite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stock_code: code }),
            });
            const result = await response.json();
            if (result.success) {
                btn.textContent = '⭐';
                btn.classList.add('active');
                btn.title = '取消收藏';
            } else {
                alert('收藏失败: ' + (result.error || ''));
            }
        }
    } catch (error) {
        alert('操作失败: ' + error.message);
    }
};

/**
 * 关闭弹窗
 */
export function closeModal() {
    document.getElementById('stock-modal').classList.remove('active');
}

/**
 * 加载策略列表到历史记录下拉框
 * 与策略回测页面使用统一的数据源 /api/trading/backtest/strategies
 */
export async function loadHistoryStrategyOptions() {
    const strategySelect = document.getElementById('history-strategy-filter');
    if (!strategySelect) return;
    
    try {
        // 使用与策略回测一致的API端点
        const response = await fetch('/api/trading/backtest/strategies');
        const data = await response.json();
        
        if (data.success && data.data && data.data.strategies) {
            // 保留第一个选项（全部策略）
            strategySelect.innerHTML = '<option value="">全部策略</option>';
            
            data.data.strategies.forEach(strategy => {
                const option = document.createElement('option');
                // 使用中文名称作为value和显示文本，与策略回测页面保持一致
                const chineseName = strategy.display_name || strategy.name;
                option.value = chineseName;
                option.textContent = chineseName;
                strategySelect.appendChild(option);
            });
        }
    } catch (error) {
        console.error('加载策略列表失败:', error);
    }
}

/**
 * 显示行业股票列表
 * @param {string} industry - 行业名称
 * @param {number} limit - 显示数量
 */
export async function showIndustryStocks(industry, limit = 50) {
    try {
        const response = await fetch(`/api/dashboard/industry-stocks?industry=${encodeURIComponent(industry)}&limit=${limit}`);
        const result = await response.json();
        
        if (result.success) {
            showStocksModal(`${industry}行业股票列表`, result.stocks, result.date || '');
        } else {
            alert('加载行业股票失败: ' + result.error);
        }
    } catch (error) {
        alert('加载行业股票失败: ' + error.message);
    }
}

/**
 * 显示板块股票列表
 * @param {string} area - 板块名称
 * @param {number} limit - 显示数量
 */
export async function showAreaStocks(area, limit = 50) {
    try {
        const response = await fetch(`/api/dashboard/area-stocks?area=${encodeURIComponent(area)}&limit=${limit}`);
        const result = await response.json();
        
        if (result.success) {
            showStocksModal(`${area}板块股票列表`, result.stocks, result.date || '');
        } else {
            alert('加载板块股票失败: ' + result.error);
        }
    } catch (error) {
        alert('加载板块股票失败: ' + error.message);
    }
}

/**
 * 显示股票列表模态框
 * @param {string} title - 模态框标题
 * @param {Array} stocks - 股票列表数据
 * @param {string} date - 评分日期
 */
export function showStocksModal(title, stocks, date) {
    const modal = document.getElementById('stock-modal');
    document.getElementById('stock-detail-modal-title').textContent = title;
    
    // 隐藏K线图表容器，只显示股票列表
    const chartContainer = document.getElementById('stock-chart-container');
    chartContainer.style.display = 'none';
    
    // 清空股票信息区域
    const stockInfo = document.getElementById('stock-info');
    stockInfo.innerHTML = '';
    
    if (stocks.length === 0) {
        stockInfo.innerHTML = '<p class="text-muted">暂无股票数据</p>';
        modal.classList.add('active');
        return;
    }
    
    // 构建表格
    let html = `
        <div class="table-responsive">
            <table class="table table-striped">
                <thead>
                    <tr>
                        <th>排名</th>
                        <th>股票代码</th>
                        <th>股票名称</th>
                        <th>评分</th>
                        <th>行业</th>
                        <th>板块</th>
                        <th>选入价</th>
                        <th>当前价</th>
                        <th>收益率</th>
                        <th>最高价格</th>
                        <th>最高收益</th>
                    </tr>
                </thead>
                <tbody>
    `;
    
    stocks.forEach((item, index) => {
        // 防御性代码，处理可能的undefined值
        const score = item.score || 0;
        const selectionPrice = item.selection_price || 0;
        const currentPrice = item.current_price || 0;
        const currentReturn = item.current_yield || 0;
        const highestPrice = item.highest_price || 0;
        const highestReturn = item.highest_yield || 0;
        
        html += `
            <tr>
                <td>${index + 1}</td>
                <td><a href="javascript:void(0)" onclick="viewStockDetail('${item.stock_code}')" class="stock-link">${item.stock_code}</a></td>
                <td>${item.stock_name}</td>
                <td><a href="javascript:void(0)" onclick="showScoreDetail('${item.stock_code}', '${date}')" class="score-link">${score.toFixed(2)}</a></td>
                <td>${item.industry || '-'}</td>
                <td>${item.sector || '-'}</td>
                <td>¥${selectionPrice.toFixed(2)}</td>
                <td>¥${currentPrice.toFixed(2)}</td>
                <td class="${currentReturn >= 0 ? 'text-success' : 'text-danger'}">${currentReturn.toFixed(2)}%</td>
                <td>¥${highestPrice.toFixed(2)}</td>
                <td class="${highestReturn >= 0 ? 'text-success' : 'text-danger'}">${highestReturn.toFixed(2)}%</td>
            </tr>
        `;
    });
    
    html += `
                </tbody>
            </table>
        </div>
    `;
    
    stockInfo.innerHTML = html;
    modal.classList.add('active');
}

/**
 * 初始化K线图表
 * @param {string} containerId - 容器ID
 * @param {Object} data - K线数据
 */
function initKlineChart(containerId, data) {
    // 调用全局的initKlineChart函数
    if (window.initKlineChart) {
        window.initKlineChart(containerId, data);
    } else {
        console.error('全局initKlineChart函数不存在');
    }
}
