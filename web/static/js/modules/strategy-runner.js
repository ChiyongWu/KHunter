/**
 * 策略运行模块
 * 实现策略运行的前端界面和交互（简化版 - 无方案管理）
 */

// 策略运行模块
const StrategyRunnerModule = {
    // 执行任务列表
    tasks: [],
    
    // 初始化策略运行模块
    initStrategyRunnerModule: function() {
        this.setupEventListeners();
        this.loadStrategyRunnerPage();
    },
    
    // 设置事件监听器
    setupEventListeners: function() {
        // 加入任务按钮（先解绑防止重复绑定）
        const addTaskBtn = document.getElementById('add-runner-task-btn');
        if (addTaskBtn) {
            addTaskBtn.removeEventListener('click', this._addTaskHandler);
            this._addTaskHandler = () => this.addTask();
            addTaskBtn.addEventListener('click', this._addTaskHandler);
        }
        
        // 开始执行按钮
        const startBtn = document.getElementById('start-runner-btn');
        if (startBtn) {
            startBtn.addEventListener('click', () => this.startExecution());
        }
        
        // 取消按钮
        const cancelBtn = document.getElementById('cancel-runner-btn');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => this.cancelExecution());
        }
        
        // 信号执行按钮（动态代理）
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
        
        // 任务删除按钮（动态代理）
        document.addEventListener('click', (e) => {
            if (e.target.classList.contains('remove-task-btn')) {
                const index = parseInt(e.target.dataset.index);
                this.removeTask(index);
            }
        });
    },
    
    // 加载策略运行页面
    loadStrategyRunnerPage: async function() {
        // 加载选股策略列表
        try {
            await this.loadSelectionStrategies();
        } catch (error) {
            console.error('加载选股策略失败:', error);
        }
        
        // 加载运行状态
        try {
            await this.loadStrategyStatus();
        } catch (error) {
            console.error('加载运行状态失败:', error);
        }
        
        // 加载持仓信息
        try {
            await this.loadPortfolio();
        } catch (error) {
            console.error('加载持仓信息失败:', error);
        }
        
        // 加载信号列表
        try {
            await this.loadSignals();
        } catch (error) {
            console.error('加载信号列表失败:', error);
        }
    },
    
    // 加载选股策略列表
    loadSelectionStrategies: async function() {
        try {
            const response = await fetch('/api/strategies');
            const result = await response.json();
            
            if (result.success && result.strategies) {
                const select = document.getElementById('selection-strategy');
                if (select) {
                    select.innerHTML = result.strategies.map(strategy => 
                        `<option value="${strategy.name}">${strategy.display_name || strategy.name}</option>`
                    ).join('');
                }
            }
        } catch (error) {
            console.error('加载选股策略失败:', error);
        }
    },
    
    // 添加任务
    addTask: function() {
        const selectionSelect = document.getElementById('selection-strategy');
        const selectionStrategy = selectionSelect.value;
        const selectionStrategyDisplayName = selectionSelect.options[selectionSelect.selectedIndex].text;
        
        const timingSelect = document.getElementById('timing-strategy');
        const timingStrategy = timingSelect.value;
        const timingStrategyDisplayName = timingSelect.options[timingSelect.selectedIndex].text;
        
        if (!selectionStrategy) {
            alert('请选择选股策略');
            return;
        }
        
        const task = {
            selection_strategy: selectionStrategy,
            selection_strategy_display_name: selectionStrategyDisplayName,
            timing_strategy: timingStrategy,
            timing_strategy_display_name: timingStrategyDisplayName
        };
        
        this.tasks.push(task);
        this.renderTaskList();
    },
    
    // 移除任务
    removeTask: function(index) {
        if (index >= 0 && index < this.tasks.length) {
            this.tasks.splice(index, 1);
            this.renderTaskList();
        }
    },
    
    // 渲染任务列表
    renderTaskList: function() {
        const taskListEl = document.getElementById('runner-task-list');
        const taskBody = document.getElementById('runner-task-body');
        const taskCount = document.getElementById('runner-task-count');
        const startBtn = document.getElementById('start-runner-btn');
        
        if (this.tasks.length === 0) {
            taskListEl.style.display = 'none';
            return;
        }
        
        taskListEl.style.display = 'block';
        taskCount.textContent = this.tasks.length;
        startBtn.disabled = false;
        
        taskBody.innerHTML = this.tasks.map((task, index) => `
            <tr>
                <td>${index + 1}</td>
                <td style="word-break:break-all;">${task.selection_strategy_display_name || task.selection_strategy}</td>
                <td>${task.timing_strategy_display_name || task.timing_strategy}</td>
                <td style="text-align:center;">
                    <button class="btn btn-sm btn-outline-danger remove-task-btn" data-index="${index}">删除</button>
                </td>
            </tr>
        `).join('');
    },
    
    // 开始执行
    startExecution: async function() {
        if (this.tasks.length === 0) {
            alert('请先添加执行任务');
            return;
        }
        
        // 更新状态
        document.getElementById('execution-status').className = 'execution-status status-running';
        document.getElementById('execution-status').textContent = '执行中';
        
        // 显示进度
        document.getElementById('runner-progress-container').style.display = 'block';
        document.getElementById('runner-current-task').textContent = '正在执行...';
        document.getElementById('runner-progress-fill').style.width = '0%';
        document.getElementById('runner-progress-percent').textContent = '0%';
        
        // 清空日志
        document.getElementById('execution-log').innerHTML = '<p style="color:#22c55e; margin:0;">开始执行...</p>';
        
        // 逐个执行任务
        for (let i = 0; i < this.tasks.length; i++) {
            const task = this.tasks[i];
            const progress = ((i + 1) / this.tasks.length * 100).toFixed(0);
            
            document.getElementById('runner-current-task').textContent = 
                `正在执行 ${i + 1}/${this.tasks.length}: ${task.selection_strategy_display_name || task.selection_strategy}`;
            document.getElementById('runner-progress-fill').style.width = progress + '%';
            document.getElementById('runner-progress-percent').textContent = progress + '%';
            
            this.appendLog(`执行任务 ${i + 1}: ${task.selection_strategy_display_name || task.selection_strategy} + ${task.timing_strategy_display_name || task.timing_strategy}`);
            
            try {
                const response = await fetch('/api/strategy/run', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        strategies: [task.selection_strategy],
                        timing_strategy: task.timing_strategy
                    })
                });
                
                const result = await response.json();
                
                if (result.success) {
                    this.appendLog(`  ✓ 成功`);
                } else {
                    this.appendLog(`  ✗ 失败: ${result.message || '未知错误'}`);
                }
            } catch (error) {
                console.error('执行任务失败:', error);
                this.appendLog(`  ✗ 失败: ${error.message}`);
            }
        }
        
        // 执行完成
        this.appendLog('--- 执行完成 ---');
        document.getElementById('execution-status').className = 'execution-status status-idle';
        document.getElementById('execution-status').textContent = '就绪';
        
        // 刷新页面数据
        this.loadStrategyStatus();
        this.loadPortfolio();
        this.loadSignals();
        
        // 清空任务列表
        this.tasks = [];
        this.renderTaskList();
    },
    
    // 取消执行
    cancelExecution: function() {
        document.getElementById('runner-progress-container').style.display = 'none';
        document.getElementById('execution-status').className = 'execution-status status-idle';
        document.getElementById('execution-status').textContent = '已取消';
        this.appendLog('执行已取消');
    },
    
    // 加载运行状态
    loadStrategyStatus: async function() {
        try {
            const response = await fetch('/api/strategy/status');
            const result = await response.json();
            
            if (result.success) {
                const statusContainer = document.getElementById('strategy-status');
                if (statusContainer) {
                    const status = result.data;
                    statusContainer.innerHTML = `
                        <div class="card-body" style="padding:10px 16px;">
                            <div style="display:flex; gap:24px; flex-wrap:wrap; font-size:13px;">
                                <div><strong>当前状态:</strong> ${status.running ? '运行中' : '停止'}</div>
                                <div><strong>今日已选股:</strong> ${status.selected_stocks || 0} 只</div>
                                <div><strong>今日已交易:</strong> ${status.today_trades || 0} 笔</div>
                                <div><strong>最后运行:</strong> ${status.last_run || '从未'}</div>
                            </div>
                        </div>
                    `;
                }
            }
        } catch (error) {
            console.error('加载策略状态失败:', error);
        }
    },
    
    // 加载持仓信息
    loadPortfolio: async function() {
        try {
            const response = await fetch('/api/portfolio');
            const result = await response.json();
            
            if (result.success) {
                const portfolio = result.data;
                
                // 更新统计卡片
                document.getElementById('position-count').textContent = portfolio.positions_count || 0;
                document.getElementById('available-cash').textContent = '¥' + (portfolio.available_cash || 0).toLocaleString();
                document.getElementById('total-assets').textContent = '¥' + (portfolio.total_assets || 0).toLocaleString();
                document.getElementById('portfolio-profit').textContent = (portfolio.total_profit_percent || 0).toFixed(2) + '%';
                
                // 更新持仓列表
                const portfolioList = document.getElementById('portfolio-list');
                if (portfolioList) {
                    if (portfolio.positions && portfolio.positions.length > 0) {
                        portfolioList.innerHTML = portfolio.positions.map(pos => `
                            <tr>
                                <td><a href="#" onclick="viewStockDetail('${pos.stock_code}')">${pos.stock_code}</a></td>
                                <td>${pos.stock_name}</td>
                                <td>${pos.quantity}</td>
                                <td>¥${pos.cost_price.toFixed(2)}</td>
                                <td>¥${pos.current_price.toFixed(2)}</td>
                                <td>${pos.profit_loss >= 0 ? '+' : ''}¥${pos.profit_loss.toFixed(2)}</td>
                                <td style="color: ${pos.profit_loss_percent >= 0 ? '#22c55e' : '#ef4444'}">
                                    ${pos.profit_loss_percent >= 0 ? '+' : ''}${pos.profit_loss_percent.toFixed(2)}%
                                </td>
                                <td>${pos.hold_days}</td>
                                <td>
                                    <button class="btn btn-sm btn-danger sell-position-btn" data-position-id="${pos.id}">卖出</button>
                                </td>
                            </tr>
                        `).join('');
                    } else {
                        portfolioList.innerHTML = '<tr><td colspan="9" style="text-align:center; color:#9ca3af;">暂无持仓</td></tr>';
                    }
                }
            }
        } catch (error) {
            console.error('加载持仓信息失败:', error);
        }
    },
    
    // 加载信号列表
    loadSignals: async function() {
        try {
            const response = await fetch('/api/signals/today');
            const result = await response.json();
            
            if (result.success) {
                const signals = result.data;
                const signalsList = document.getElementById('signals-list');
                
                if (signalsList) {
                    if (signals && signals.length > 0) {
                        signalsList.innerHTML = signals.map(signal => `
                            <tr>
                                <td>${signal.type === 'buy' ? '<span style="color:#22c55e;">买入</span>' : '<span style="color:#ef4444;">卖出</span>'}</td>
                                <td>${signal.stock_code}</td>
                                <td>${signal.stock_name}</td>
                                <td>¥${signal.price.toFixed(2)}</td>
                                <td>${signal.quantity}</td>
                                <td>${signal.reason}</td>
                                <td>
                                    <button class="btn btn-sm btn-success execute-signal-btn" data-signal-id="${signal.id}">执行</button>
                                    <button class="btn btn-sm btn-secondary ignore-signal-btn" data-signal-id="${signal.id}">忽略</button>
                                </td>
                            </tr>
                        `).join('');
                    } else {
                        signalsList.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#9ca3af;">今日暂无信号</td></tr>';
                    }
                }
            }
        } catch (error) {
            console.error('加载信号列表失败:', error);
        }
    },
    
    // 执行信号
    executeSignal: async function(signalId) {
        try {
            const response = await fetch(`/api/signals/${signalId}/execute`, {
                method: 'POST'
            });
            
            const result = await response.json();
            
            if (result.success) {
                this.appendLog('信号执行成功');
                this.loadSignals();
                this.loadPortfolio();
            } else {
                alert('信号执行失败: ' + (result.message || '未知错误'));
            }
        } catch (error) {
            console.error('执行信号失败:', error);
            alert('执行信号失败');
        }
    },
    
    // 忽略信号
    ignoreSignal: async function(signalId) {
        try {
            const response = await fetch(`/api/signals/${signalId}/ignore`, {
                method: 'POST'
            });
            
            const result = await response.json();
            
            if (result.success) {
                this.appendLog('信号已忽略');
                this.loadSignals();
            } else {
                alert('忽略信号失败: ' + (result.message || '未知错误'));
            }
        } catch (error) {
            console.error('忽略信号失败:', error);
            alert('忽略信号失败');
        }
    },
    
    // 追加日志
    appendLog: function(message) {
        const logContainer = document.getElementById('execution-log');
        const timestamp = new Date().toLocaleTimeString();
        const logItem = document.createElement('div');
        logItem.innerHTML = `<span style="color:#9ca3af;">[${timestamp}]</span> ${message}`;
        logContainer.appendChild(logItem);
        logContainer.scrollTop = logContainer.scrollHeight;
    }
};

// 导出模块
export default StrategyRunnerModule;
