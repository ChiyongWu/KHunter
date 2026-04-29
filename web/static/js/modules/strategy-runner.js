/**
 * 策略运行模块
 * 实现策略运行的前端界面和交互
 */

// 策略运行模块
const StrategyRunnerModule = {
    // 初始化策略运行模块
    initStrategyRunnerModule: function() {
        this.setupEventListeners();
        this.loadStrategyRunnerPage();
    },
    
    // 设置事件监听器
    setupEventListeners: function() {
        // 运行策略按钮
        const runStrategyBtn = document.getElementById('run-strategy-btn');
        if (runStrategyBtn) {
            runStrategyBtn.addEventListener('click', () => this.runStrategy());
        }
        
        // 策略选择相关
        const selectAllStrategiesBtn = document.getElementById('select-all-strategies');
        if (selectAllStrategiesBtn) {
            selectAllStrategiesBtn.addEventListener('click', () => this.selectAllStrategies());
        }
        
        const deselectAllStrategiesBtn = document.getElementById('deselect-all-strategies');
        if (deselectAllStrategiesBtn) {
            deselectAllStrategiesBtn.addEventListener('click', () => this.deselectAllStrategies());
        }
        
        // 信号执行按钮
        document.addEventListener('click', (e) => {
            if (e.target.classList.contains('execute-signal-btn')) {
                const signalId = e.target.dataset.signalId;
                this.executeSignal(signalId);
            }
        });
        
        document.addEventListener('click', (e) => {
            if (e.target.classList.contains('ignore-signal-btn')) {
                const signalId = e.target.dataset.signalId;
                this.ignoreSignal(signalId);
            }
        });
    },
    
    // 加载策略运行页面
    loadStrategyRunnerPage: async function() {
        // 加载策略列表（独立错误隔离）
        try {
            await this.loadStrategies();
        } catch (error) {
            console.error('加载策略列表失败:', error);
        }
        
        // 加载择时策略列表（独立错误隔离）
        try {
            await this.loadTimingStrategies();
        } catch (error) {
            console.error('加载择时策略列表失败:', error);
        }
        
        // 加载运行状态（独立错误隔离）
        try {
            await this.loadStrategyStatus();
        } catch (error) {
            console.error('加载运行状态失败:', error);
        }
        
        // 加载持仓信息（独立错误隔离）
        try {
            await this.loadPortfolio();
        } catch (error) {
            console.error('加载持仓信息失败:', error);
        }
        
        // 加载信号列表（独立错误隔离）
        try {
            await this.loadSignals();
        } catch (error) {
            console.error('加载信号列表失败:', error);
        }
    },
    
    // 加载策略列表
    loadStrategies: async function() {
        try {
            console.log('开始加载策略列表...');
            const response = await fetch('/api/strategies');
            console.log('策略列表API响应:', response);
            const result = await response.json();
            console.log('策略列表数据:', result);
            
            if (result.success && result.strategies) {
                const strategyList = document.getElementById('strategy-list');
                console.log('找到策略列表容器:', strategyList);
                if (strategyList) {
                    strategyList.innerHTML = '';
                    // 策略列表为空时显示提示
                    if (result.strategies.length === 0) {
                        strategyList.innerHTML = '<div class="text-center text-muted p-3">暂无可用策略</div>';
                    } else {
                        result.strategies.forEach(strategy => {
                            const checkbox = document.createElement('div');
                            checkbox.className = 'form-check';
                            checkbox.innerHTML = `
                                <input class="form-check-input" type="checkbox" value="${strategy.name}" id="strategy-${strategy.name}">
                                <label class="form-check-label" for="strategy-${strategy.name}">
                                    ${strategy.display_name || strategy.name}
                                </label>
                            `;
                            strategyList.appendChild(checkbox);
                        });
                    }
                    console.log('策略列表加载完成，共', result.strategies.length, '个策略');
                } else {
                    console.error('未找到策略列表容器');
                }
            } else {
                // API返回错误时显示错误提示
                const strategyList = document.getElementById('strategy-list');
                if (strategyList) {
                    strategyList.innerHTML = '<div class="text-center text-danger p-3">策略加载失败，请刷新页面重试</div>';
                }
            }
        } catch (error) {
            console.error('加载策略列表失败:', error);
        }
    },
    
    // 加载择时策略列表
    loadTimingStrategies: async function() {
        try {
            console.log('开始加载择时策略列表...');
            const timingStrategies = [
                { value: 'turtle', label: '海龟策略' },
                { value: 'rsi', label: 'RSI策略' },
                { value: 'bollinger', label: '布林带策略' },
                { value: 'support', label: '支撑位策略' }
            ];
            
            const timingStrategySelect = document.getElementById('timing-strategy');
            console.log('找到择时策略选择框:', timingStrategySelect);
            if (timingStrategySelect) {
                timingStrategySelect.innerHTML = '';
                timingStrategies.forEach(strategy => {
                    const option = document.createElement('option');
                    option.value = strategy.value;
                    option.textContent = strategy.label;
                    timingStrategySelect.appendChild(option);
                });
                console.log('择时策略列表加载完成，共', timingStrategies.length, '个策略');
            } else {
                console.error('未找到择时策略选择框');
            }
        } catch (error) {
            console.error('加载择时策略列表失败:', error);
        }
    },
    
    // 加载策略运行状态
    loadStrategyStatus: async function() {
        try {
            const response = await fetch('/api/strategy/status');
            const result = await response.json();
            
            if (result.success) {
                const statusElement = document.getElementById('strategy-status');
                if (statusElement) {
                    statusElement.innerHTML = `
                        <div class="card-body">
                            <h6 class="card-title">运行状态</h6>
                            <p class="card-text">
                                <strong>运行日期:</strong> ${result.data.date || '未运行'}</p>
                            <p class="card-text">
                                <strong>状态:</strong> <span class="badge ${result.data.status === 'completed' ? 'bg-success' : 'bg-warning'}">
                                    ${result.data.status === 'completed' ? '已处理' : '未运行'}
                                </span>
                            </p>
                            <p class="card-text">
                                <strong>策略:</strong> ${result.data.strategy || '未设置'}
                            </p>
                        </div>
                    `;
                }
            } else {
                // 运行器未就绪时显示提示
                const statusElement = document.getElementById('strategy-status');
                if (statusElement) {
                    statusElement.innerHTML = `
                        <div class="card-body">
                            <h6 class="card-title">运行状态</h6>
                            <p class="card-text">
                                <span class="badge bg-secondary">运行器未就绪</span>
                            </p>
                        </div>
                    `;
                }
            }
        } catch (error) {
            console.error('加载策略运行状态失败:', error);
        }
    },
    
    // 加载持仓信息
    loadPortfolio: async function() {
        try {
            const response = await fetch('/api/portfolio');
            const result = await response.json();
            
            if (result.success && result.data) {
                const initialCash = result.data.initial_cash || 1000000;
                const positions = result.data.positions || {};
                
                // 计算持仓统计
                let positionCount = 0;
                let totalMarketValue = 0;
                let totalProfitLoss = 0;
                
                Object.entries(positions).forEach(([code, pos]) => {
                    positionCount++;
                    const marketValue = pos.quantity * pos.current_price;
                    totalMarketValue += marketValue;
                    totalProfitLoss += pos.profit_loss;
                });
                
                const availableCash = initialCash - totalMarketValue;
                const totalAssets = initialCash + totalProfitLoss;
                const profitRate = totalAssets / initialCash - 1;
                
                // 更新统计卡片
                const positionCountEl = document.getElementById('position-count');
                const availableCashEl = document.getElementById('available-cash');
                const totalAssetsEl = document.getElementById('total-assets');
                const portfolioProfitEl = document.getElementById('portfolio-profit');
                
                if (positionCountEl) positionCountEl.textContent = positionCount;
                if (availableCashEl) availableCashEl.textContent = `¥${availableCash.toFixed(0)}`;
                if (totalAssetsEl) totalAssetsEl.textContent = `¥${totalAssets.toFixed(0)}`;
                if (portfolioProfitEl) {
                    portfolioProfitEl.textContent = `${profitRate >= 0 ? '+' : ''}${(profitRate * 100).toFixed(2)}%`;
                    portfolioProfitEl.className = `stat-value ${profitRate >= 0 ? 'positive' : 'negative'}`;
                }
                
                // 更新持仓表格
                const portfolioElement = document.getElementById('portfolio-list');
                if (portfolioElement) {
                    if (Object.keys(positions).length === 0) {
                        portfolioElement.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无持仓</td></tr>';
                    } else {
                        portfolioElement.innerHTML = '';
                        Object.entries(positions).forEach(([code, pos]) => {
                            const row = document.createElement('tr');
                            row.innerHTML = `
                                <td>${code}</td>
                                <td>${pos.stock_name}</td>
                                <td>${pos.quantity}</td>
                                <td>¥${pos.buy_price.toFixed(2)}</td>
                                <td>¥${pos.current_price.toFixed(2)}</td>
                                <td class="${pos.profit_loss >= 0 ? 'text-success' : 'text-danger'}">
                                    ${pos.profit_loss >= 0 ? '+' : ''}¥${pos.profit_loss.toFixed(2)}
                                </td>
                                <td class="${pos.profit_rate >= 0 ? 'text-success' : 'text-danger'}">
                                    ${pos.profit_rate >= 0 ? '+' : ''}${(pos.profit_rate * 100).toFixed(2)}%
                                </td>
                                <td>${pos.holding_days}</td>
                                <td><button class="btn btn-sm btn-info" onclick="viewStockDetail('${code}')">详情</button></td>
                            `;
                            portfolioElement.appendChild(row);
                        });
                    }
                }
            } else {
                // 显示默认空持仓状态
                const portfolioElement = document.getElementById('portfolio-list');
                if (portfolioElement) {
                    portfolioElement.innerHTML = '<tr><td colspan="9" class="text-center text-muted">暂无持仓</td></tr>';
                }
            }
        } catch (error) {
            console.error('加载持仓信息失败:', error);
        }
    },
    
    // 加载信号列表
    loadSignals: async function() {
        try {
            const response = await fetch('/api/signals');
            const result = await response.json();
            
            if (result.success && result.data) {
                const signalsElement = document.getElementById('signals-list');
                if (signalsElement) {
                    // 使用防御性变量，避免直接访问可能不存在的属性
                    const signals = result.data.signals || [];
                    if (signals.length === 0) {
                        signalsElement.innerHTML = '<tr><td colspan="7" class="text-center text-muted">暂无信号</td></tr>';
                    } else {
                        signalsElement.innerHTML = '';
                        signals.forEach(signal => {
                            const row = document.createElement('tr');
                            row.innerHTML = `
                                <td class="${signal.signal_type === 'buy' ? 'text-success' : 'text-danger'}">
                                    ${signal.signal_type === 'buy' ? '买入' : '卖出'}
                                </td>
                                <td>${signal.stock_code}</td>
                                <td>${signal.stock_name}</td>
                                <td>¥${signal.price.toFixed(2)}</td>
                                <td>${signal.quantity}</td>
                                <td>${signal.reason}</td>
                                <td>
                                    <button class="btn btn-sm btn-success execute-signal-btn" data-signal-id="${signal.id}">执行</button>
                                    <button class="btn btn-sm btn-secondary ignore-signal-btn" data-signal-id="${signal.id}">忽略</button>
                                </td>
                            `;
                            signalsElement.appendChild(row);
                        });
                    }
                }
            } else {
                // API返回错误时显示默认状态
                const signalsElement = document.getElementById('signals-list');
                if (signalsElement) {
                    signalsElement.innerHTML = '<tr><td colspan="7" class="text-center text-muted">暂无信号</td></tr>';
                }
            }
        } catch (error) {
            console.error('加载信号列表失败:', error);
        }
    },
    
    // 全选策略
    selectAllStrategies: function() {
        const checkboxes = document.querySelectorAll('#strategy-list input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.checked = true;
        });
    },
    
    // 取消全选策略
    deselectAllStrategies: function() {
        const checkboxes = document.querySelectorAll('#strategy-list input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.checked = false;
        });
    },
    
    // 运行策略
    runStrategy: async function() {
        try {
            // 获取选中的策略
            const selectedStrategies = [];
            const checkboxes = document.querySelectorAll('#strategy-list input[type="checkbox"]:checked');
            checkboxes.forEach(checkbox => {
                selectedStrategies.push(checkbox.value);
            });
            
            // 获取择时策略
            const timingStrategy = document.getElementById('timing-strategy').value;
            
            // 显示加载中
            const runButton = document.getElementById('run-strategy-btn');
            const originalText = runButton.textContent;
            runButton.disabled = true;
            runButton.textContent = '运行中...';
            
            // 发送请求
            const response = await fetch('/api/strategy/run', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    strategy_names: selectedStrategies,
                    timing_strategy: timingStrategy
                })
            });
            
            const result = await response.json();
            
            if (result.status === 'success') {
                alert('策略运行成功！');
                // 刷新页面数据
                await this.loadStrategyStatus();
                await this.loadPortfolio();
                await this.loadSignals();
            } else {
                alert('策略运行失败: ' + result.message);
            }
        } catch (error) {
            console.error('运行策略失败:', error);
            alert('运行策略失败: ' + error.message);
        } finally {
            // 恢复按钮状态
            const runButton = document.getElementById('run-strategy-btn');
            if (runButton) {
                runButton.disabled = false;
                runButton.textContent = '开始运行';
            }
        }
    },
    
    // 执行信号
    executeSignal: async function(signalId) {
        try {
            const response = await fetch('/api/trades/execute', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ signal_id: signalId })
            });
            
            const result = await response.json();
            
            if (result.success) {
                alert('信号执行成功！');
                // 刷新页面数据
                await this.loadPortfolio();
                await this.loadSignals();
            } else {
                alert('信号执行失败: ' + result.error);
            }
        } catch (error) {
            console.error('执行信号失败:', error);
            alert('执行信号失败: ' + error.message);
        }
    },
    
    // 忽略信号
    ignoreSignal: async function(signalId) {
        try {
            const response = await fetch('/api/trades/ignore', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ signal_id: signalId })
            });
            
            const result = await response.json();
            
            if (result.success) {
                alert('信号已忽略！');
                // 刷新信号列表
                await this.loadSignals();
            } else {
                alert('忽略信号失败: ' + result.error);
            }
        } catch (error) {
            console.error('忽略信号失败:', error);
            alert('忽略信号失败: ' + error.message);
        }
    }
};

export default StrategyRunnerModule;