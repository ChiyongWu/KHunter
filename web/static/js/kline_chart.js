/**
 * K线图表模块 - 使用Canvas绘制
 * 功能：全量K线数据展示、时间范围切换（近1月/近3月/近6月/近1年/全部）、
 *       拖拽框选放大、滚轮以光标为锚点缩放、双击/按钮还原、
 *       十字光标悬停提示、区间涨跌统计、MA均线、成交量
 *
 * 数据契约：后端 /api/stock/<code> 返回全量K线（升序），
 *           展示范围由前端切换控件控制，MA/KDJ 基于全量计算保证连续性
 */

// 全局变量存储图表实例
let klineChartInstance = null;

// 时间范围预设（交易日近似数）
const KLINE_RANGES = [
    { key: '1m',  label: '近1月', bars: 22 },
    { key: '3m',  label: '近3月', bars: 66 },
    { key: '6m',  label: '近6月', bars: 132 },
    { key: '1y',  label: '近1年', bars: 250 },
    { key: 'all', label: '全部',  bars: Infinity },
];

const UP_COLOR = '#ef4444';
const DOWN_COLOR = '#10b981';

/**
 * 初始化K线图表（入口函数，签名保持兼容）
 * @param {string} containerId - 容器ID
 * @param {Array} rawData - 原始数据数组（升序）
 */
function initKlineChart(containerId, rawData) {
    // 销毁旧的图表实例
    if (klineChartInstance) {
        klineChartInstance.destroy();
        klineChartInstance = null;
    }

    // 获取容器
    const container = document.getElementById(containerId);
    if (!container) {
        console.error(`容器 ${containerId} 不存在`);
        return;
    }

    // 清空容器
    container.innerHTML = '';

    // 检查容器尺寸
    const containerWidth = container.clientWidth;
    const containerHeight = container.clientHeight;

    if (containerWidth === 0 || containerHeight === 0) {
        console.error(`容器尺寸无效: ${containerWidth}x${containerHeight}，容器可能未显示`);
        container.innerHTML = '<div style="padding: 20px; color: #ef4444;">容器尺寸无效，请稍后重试</div>';
        return;
    }

    try {
        // 转换数据格式（基于全量计算均线，保证切换范围时均线连续）
        const formatted = formatKlineData(rawData);

        // 检查是否有足够的数据
        if (formatted.candleData.length === 0) {
            console.error('没有有效的K线数据');
            container.innerHTML = '<div style="padding: 20px; color: #ef4444;">没有有效的K线数据</div>';
            return;
        }

        // 图表状态
        const state = {
            rawData,
            formatted,
            range: 'all',                       // 默认展示全部数据
            viewStart: 0,
            viewEnd: formatted.candleData.length,
            layout: null,                       // 绘制布局参数（十字光标用）
        };

        // 构建DOM结构：工具条 + 图表区（主画布/悬浮层/提示框）
        container.innerHTML = `
            <div class="kline-toolbar">
                <div class="kline-range-group">
                    ${KLINE_RANGES.map(r =>
                        `<button type="button" data-range="${r.key}"${r.key === 'all' ? ' class="active"' : ''}>${r.label}</button>`
                    ).join('')}
                </div>
                <button type="button" class="kline-reset-btn" disabled
                        title="还原到当前选定的标准范围（也可双击图表）">⤾ 还原</button>
                <div class="kline-stats" id="kline-range-stats"></div>
            </div>
            <div class="kline-chart-area">
                <canvas class="kline-main-canvas"></canvas>
                <canvas class="kline-overlay-canvas"></canvas>
                <div class="kline-tooltip" style="display: none;"></div>
            </div>
        `;

        const mainCanvas = container.querySelector('.kline-main-canvas');
        const overlayCanvas = container.querySelector('.kline-overlay-canvas');
        const tooltipEl = container.querySelector('.kline-tooltip');
        const chartArea = container.querySelector('.kline-chart-area');
        const statsEl = document.getElementById('kline-range-stats');
        const resetBtn = container.querySelector('.kline-reset-btn');

        const ctx = mainCanvas.getContext('2d', { alpha: false });
        const overlayCtx = overlayCanvas.getContext('2d');
        if (!ctx) {
            throw new Error('无法获取Canvas上下文');
        }

        // 同步主/悬浮画布尺寸
        const syncCanvasSize = () => {
            const w = chartArea.clientWidth;
            const h = chartArea.clientHeight;
            [mainCanvas, overlayCanvas].forEach(c => {
                c.style.width = w + 'px';
                c.style.height = h + 'px';
                c.width = w;
                c.height = h;
            });
            return { w, h };
        };

        // 计算当前范围的视图窗口
        const applyRange = (rangeKey) => {
            const preset = KLINE_RANGES.find(r => r.key === rangeKey) || KLINE_RANGES[KLINE_RANGES.length - 1];
            const total = state.formatted.candleData.length;
            const bars = Math.min(preset.bars, total);
            state.range = rangeKey;
            state.viewStart = Math.max(0, total - bars);
            state.viewEnd = total;
        };

        // 更新区间统计信息（起止日期 + 区间涨跌幅）
        const updateStats = () => {
            if (!statsEl) return;
            const cd = state.formatted.candleData;
            const s = state.viewStart;
            const e = state.viewEnd;
            if (e <= s) { statsEl.innerHTML = ''; return; }
            const first = cd[s];
            const last = cd[e - 1];
            const change = ((last.close - first.open) / first.open) * 100;
            const cls = change >= 0 ? 'up' : 'down';
            const sign = change >= 0 ? '+' : '';
            statsEl.innerHTML =
                `<span>${fmtDate(first.time)} ~ ${fmtDate(last.time)}（${e - s}个交易日）</span>` +
                `<span class="${cls}">区间${change >= 0 ? '上涨' : '下跌'} ${sign}${change.toFixed(2)}%</span>`;
        };

        // 绘制当前视图
        const drawView = () => {
            const { w, h } = syncCanvasSize();
            const f = state.formatted;
            const s = state.viewStart;
            const e = state.viewEnd;
            const viewData = {
                candleData: f.candleData.slice(s, e),
                volumeData: f.volumeData.slice(s, e),
                ma5Arr: f.ma5Arr.slice(s, e),
                ma10Arr: f.ma10Arr.slice(s, e),
                ma20Arr: f.ma20Arr.slice(s, e),
                kArr: f.kArr.slice(s, e),
                dArr: f.dArr.slice(s, e),
                jArr: f.jArr.slice(s, e),
            };
            state.layout = drawKlineChart(ctx, mainCanvas, viewData, w, h);
        };

        // 范围切换按钮事件
        const rangeGroup = container.querySelector('.kline-range-group');
        rangeGroup.addEventListener('click', (ev) => {
            const btn = ev.target.closest('button[data-range]');
            if (!btn) return;
            rangeGroup.querySelectorAll('button').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            applyRange(btn.dataset.range);
            resetBtn.disabled = true;   // 回到标准范围后还原按钮失效
            updateStats();
            drawView();
            clearCrosshair();
        });

        // ---- 十字光标与悬停提示（仅重绘悬浮层，不重绘主图）----
        const clearCrosshair = () => {
            overlayCtx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
            tooltipEl.style.display = 'none';
        };

        // ---- 拖拽框选放大 / 滚轮缩放 / 还原 ----
        const MIN_BARS = 10;    // 视图最少保留的K线根数
        const clampNum = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

        // 框选拖拽状态（像素坐标，绘图区内）
        const dragState = { active: false, x0: 0, x1: 0 };

        // 应用自定义视图窗口并刷新（缩放/框选统一入口）
        const setView = (s, e) => {
            const total = state.formatted.candleData.length;
            s = clampNum(Math.round(s), 0, total - 1);
            e = clampNum(Math.round(e), s + 1, total);
            if (e - s < MIN_BARS) {         // 最小根数保护：以中心扩展
                const mid = Math.floor((s + e) / 2);
                s = clampNum(mid - Math.floor(MIN_BARS / 2), 0, total - MIN_BARS);
                e = s + MIN_BARS;
            }
            state.viewStart = s;
            state.viewEnd = e;
            updateStats();
            drawView();
            clearCrosshair();
        };

        // 进入自定义视图：取消预设按钮高亮，启用还原按钮
        const markCustom = () => {
            if (state.range !== 'custom') {
                state.range = 'custom';
                rangeGroup.querySelectorAll('button').forEach(b => b.classList.remove('active'));
            }
            resetBtn.disabled = false;
        };

        // 滚轮缩放：以光标所在的K线为锚点，放大/缩小视图窗口
        const zoomAt = (anchorViewIdx, factor) => {
            const total = state.formatted.candleData.length;
            const n = state.viewEnd - state.viewStart;
            const n2 = clampNum(Math.round(n * factor), MIN_BARS, total);
            const anchor = state.viewStart + anchorViewIdx + 0.5;   // 锚点全局位置（K线中心）
            const t = (anchorViewIdx + 0.5) / n;                    // 锚点在视图中的相对位置
            let s = anchor - t * n2;
            let e = s + n2;
            if (s < 0) { s = 0; e = n2; }
            if (e > total) { e = total; s = total - n2; }
            markCustom();
            setView(s, e);
        };

        // 还原到当前激活的标准范围（自定义状态下还原到"全部"）
        const resetToPreset = () => {
            const rangeKey = KLINE_RANGES.some(r => r.key === state.range) ? state.range : 'all';
            const btn = rangeGroup.querySelector(`button[data-range="${rangeKey}"]`);
            if (btn) btn.click();
        };

        // 像素 x -> 视图内K线索引（clamp 到有效范围）
        const xToIdx = (x) => {
            const layout = state.layout;
            if (!layout) return 0;
            const raw = Math.floor((x - layout.padding) / layout.candleSpacing);
            return clampNum(raw, 0, layout.count - 1);
        };

        // 绘制框选矩形：半透明蓝色区域 + 虚线边框 + 选区信息提示
        const drawSelection = () => {
            const layout = state.layout;
            if (!layout) return;
            overlayCtx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);

            const left = Math.min(dragState.x0, dragState.x1);
            const right = Math.max(dragState.x0, dragState.x1);
            const i0 = xToIdx(left);
            const i1 = xToIdx(right);
            const px0 = layout.padding + i0 * layout.candleSpacing;
            const px1 = layout.padding + (i1 + 1) * layout.candleSpacing;
            const top = layout.padding;
            const bottom = layout.volumeStartY + layout.volumeHeight;

            overlayCtx.save();
            overlayCtx.fillStyle = 'rgba(59, 130, 246, 0.12)';
            overlayCtx.fillRect(px0, top, px1 - px0, bottom - top);
            overlayCtx.strokeStyle = 'rgba(37, 99, 235, 0.8)';
            overlayCtx.lineWidth = 1;
            overlayCtx.setLineDash([4, 3]);
            overlayCtx.strokeRect(px0, top, px1 - px0, bottom - top);
            overlayCtx.restore();

            // 选区信息提示：日期范围 + 交易日数
            const cd = state.formatted.candleData;
            const d0 = fmtDate(cd[state.viewStart + i0].time);
            const d1 = fmtDate(cd[state.viewStart + i1].time);
            const text = `${d0} ~ ${d1}（${i1 - i0 + 1}个交易日）松开放大`;
            overlayCtx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
            const tw = overlayCtx.measureText(text).width;
            const bx = clampNum((px0 + px1) / 2 - tw / 2 - 8, 4, overlayCanvas.clientWidth - tw - 20);
            const by = top + 8;

            overlayCtx.save();
            overlayCtx.fillStyle = 'rgba(255, 255, 255, 0.95)';
            overlayCtx.strokeStyle = 'rgba(37, 99, 235, 0.5)';
            overlayCtx.lineWidth = 1;
            const bw = tw + 16, bh = 24, r = 6;
            overlayCtx.beginPath();
            overlayCtx.moveTo(bx + r, by);
            overlayCtx.arcTo(bx + bw, by, bx + bw, by + bh, r);
            overlayCtx.arcTo(bx + bw, by + bh, bx, by + bh, r);
            overlayCtx.arcTo(bx, by + bh, bx, by, r);
            overlayCtx.arcTo(bx, by, bx + bw, by, r);
            overlayCtx.closePath();
            overlayCtx.fill();
            overlayCtx.stroke();
            overlayCtx.fillStyle = '#1d4ed8';
            overlayCtx.textAlign = 'left';
            overlayCtx.fillText(text, bx + 8, by + 16);
            overlayCtx.restore();
        };

        // 左键按下：开始框选
        overlayCanvas.addEventListener('mousedown', (ev) => {
            if (ev.button !== 0) return;
            const rect = overlayCanvas.getBoundingClientRect();
            const x = ev.clientX - rect.left;
            const layout = state.layout;
            if (!layout || x < layout.padding || x > rect.width - layout.padding) return;
            dragState.active = true;
            dragState.x0 = x;
            dragState.x1 = x;
            overlayCanvas.classList.add('kline-dragging');
            tooltipEl.style.display = 'none';
            ev.preventDefault();
        });

        // 左键松开：应用框选范围（位移过小视为误操作，忽略）
        overlayCanvas.addEventListener('mouseup', (ev) => {
            if (!dragState.active) return;
            dragState.active = false;
            overlayCanvas.classList.remove('kline-dragging');
            const moved = Math.abs(dragState.x1 - dragState.x0);
            if (moved < 5) { clearCrosshair(); return; }
            const i0 = xToIdx(Math.min(dragState.x0, dragState.x1));
            const i1 = xToIdx(Math.max(dragState.x0, dragState.x1));
            // 全局窗口 + 左右各留2根边距，保证选中K线不贴边
            const total = state.formatted.candleData.length;
            let gs = clampNum(state.viewStart + i0 - 2, 0, total);
            let ge = clampNum(state.viewStart + i1 + 3, gs + 1, total);
            if (ge - gs < 5) {      // 选区过窄：以选区中心扩展到5根
                const mid = Math.floor((gs + ge) / 2);
                gs = clampNum(mid - 2, 0, Math.max(0, total - 5));
                ge = gs + Math.min(5, total);
            }
            markCustom();
            setView(gs, ge);
        });

        // 滚轮缩放：上滚放大（显示更少），下滚缩小（显示更多）
        overlayCanvas.addEventListener('wheel', (ev) => {
            ev.preventDefault();
            const layout = state.layout;
            if (!layout) return;
            const rect = overlayCanvas.getBoundingClientRect();
            const x = ev.clientX - rect.left;
            if (x < layout.padding || x > rect.width - layout.padding) return;
            const idx = xToIdx(x);
            zoomAt(idx, ev.deltaY > 0 ? 1.2 : 1 / 1.2);
        }, { passive: false });

        // 双击还原到标准范围
        overlayCanvas.addEventListener('dblclick', resetToPreset);

        // 还原按钮
        resetBtn.addEventListener('click', resetToPreset);

        overlayCanvas.addEventListener('mousemove', (ev) => {
            const layout = state.layout;
            if (!layout) return;
            const rect = overlayCanvas.getBoundingClientRect();
            const x = ev.clientX - rect.left;
            const y = ev.clientY - rect.top;

            // 拖拽框选中：更新选区并绘制，不处理十字光标
            if (dragState.active) {
                dragState.x1 = clampNum(x, layout.padding, rect.width - layout.padding);
                drawSelection();
                return;
            }

            const { padding, chartWidth, candleSpacing, count, klineHeight, volumeStartY, adjustedMin, adjustedRange, prices, closes } = layout;
            let idx = Math.floor((x - padding) / candleSpacing);
            if (idx < 0 || idx >= count) { clearCrosshair(); return; }
            const snapX = padding + idx * candleSpacing + candleSpacing / 2;

            // 绘制十字线：竖线吸附K线中心，横线跟随鼠标（限制在K线区域内）
            overlayCtx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
            overlayCtx.save();
            overlayCtx.strokeStyle = '#94a3b8';
            overlayCtx.lineWidth = 1;
            overlayCtx.setLineDash([4, 4]);
            overlayCtx.beginPath();
            overlayCtx.moveTo(snapX, padding);
            overlayCtx.lineTo(snapX, volumeStartY + layout.volumeHeight);
            overlayCtx.stroke();
            if (y >= padding && y <= volumeStartY + layout.volumeHeight) {
                overlayCtx.beginPath();
                overlayCtx.moveTo(padding, y);
                overlayCtx.lineTo(padding + chartWidth, y);
                overlayCtx.stroke();
            }
            overlayCtx.restore();

            // 构建提示内容
            const item = state.formatted.candleData[state.viewStart + idx];
            const prevClose = state.viewStart + idx > 0
                ? state.formatted.candleData[state.viewStart + idx - 1].close
                : item.open;
            const pct = ((item.close - prevClose) / prevClose) * 100;
            const pctCls = pct >= 0 ? 'up' : 'down';
            const chg = item.close - prevClose;

            // 鼠标位置对应价格（K线区域内的横线价位）
            const hoverPrice = y >= padding && y <= padding + klineHeight
                ? (adjustedMin + adjustedRange * (1 - (y - padding) / klineHeight)) : null;

            const k = state.formatted.kArr[state.viewStart + idx];
            const d = state.formatted.dArr[state.viewStart + idx];
            const j = state.formatted.jArr[state.viewStart + idx];
            const ma5 = state.formatted.ma5Arr[state.viewStart + idx];
            const ma10 = state.formatted.ma10Arr[state.viewStart + idx];
            const ma20 = state.formatted.ma20Arr[state.viewStart + idx];
            tooltipIndexCache = idx; // 同步成交量索引（findVolume 依赖）

            const row = (label, value, cls = '') =>
                `<div class="kline-tooltip-row"><span>${label}</span><span class="${cls}">${value}</span></div>`;

            tooltipEl.innerHTML =
                `<div class="kline-tooltip-row kline-tooltip-date">${fmtDate(item.time)}</div>` +
                row('开', item.open.toFixed(2)) +
                row('高', item.high.toFixed(2)) +
                row('低', item.low.toFixed(2)) +
                row('收', item.close.toFixed(2), pct >= 0 ? 'up' : 'down') +
                row('涨跌', `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}（${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%）`, pctCls) +
                row('成交量', formatVolume(findVolume(item.time))) +
                row('MA5', ma5 != null ? ma5.toFixed(2) : '-') +
                row('MA10', ma10 != null ? ma10.toFixed(2) : '-') +
                row('MA20', ma20 != null ? ma20.toFixed(2) : '-') +
                row('KDJ', `${k != null ? k.toFixed(1) : '-'} / ${d != null ? d.toFixed(1) : '-'} / ${j != null ? j.toFixed(1) : '-'}`) +
                (hoverPrice != null ? row('光标价', hoverPrice.toFixed(2)) : '');

            // 提示框定位：跟随鼠标，靠近右边界时翻转
            tooltipEl.style.display = 'block';
            const ttW = tooltipEl.offsetWidth;
            const ttH = tooltipEl.offsetHeight;
            let ttX = snapX + 14;
            if (ttX + ttW > rect.width - 8) ttX = snapX - ttW - 14;
            let ttY = Math.min(Math.max(8, y - ttH / 2), rect.height - ttH - 8);
            tooltipEl.style.left = ttX + 'px';
            tooltipEl.style.top = ttY + 'px';
        });

        // 成交量按时间查找（volumeData 与 candleData 索引一致）
        const findVolume = (time) => {
            const vol = state.formatted.volumeData[state.viewStart + tooltipIndexCache];
            return vol ? vol.value : 0;
        };
        let tooltipIndexCache = 0;

        // 鼠标移出：取消未完成的框选并清除光标
        overlayCanvas.addEventListener('mouseleave', () => {
            dragState.active = false;
            overlayCanvas.classList.remove('kline-dragging');
            clearCrosshair();
        });

        // 窗口尺寸变化时重绘（画布已移除DOM时自动解除监听）
        const resizeHandler = () => {
            if (!mainCanvas.isConnected) {
                window.removeEventListener('resize', resizeHandler);
                return;
            }
            drawView();
            clearCrosshair();
        };
        window.addEventListener('resize', resizeHandler);

        // 初始渲染：默认展示全部数据
        applyRange('all');
        updateStats();
        drawView();

        // 保存图表实例
        klineChartInstance = {
            canvas: mainCanvas,
            ctx,
            destroy() {
                window.removeEventListener('resize', resizeHandler);
            }
        };

        console.log(`K线图表初始化成功: ${state.formatted.candleData.length} 条全量数据`);
    } catch (error) {
        console.error('K线图表初始化失败:', error);
        container.innerHTML = `<div style="padding: 20px; color: #ef4444;">图表初始化失败: ${error.message}</div>`;
    }
}

/**
 * 格式化日期（时间戳秒 -> YYYY-MM-DD）
 */
function fmtDate(timeSec) {
    const d = new Date(timeSec * 1000);
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${m}-${day}`;
}

/**
 * 绘制移动平均线
 * @param {CanvasRenderingContext2D} ctx - Canvas上下文
 * @param {Object} viewData - 当前视图数据（均线为按索引对齐的数组）
 * @param {number} padding - 内边距
 * @param {number} chartHeight - 图表高度
 * @param {number} adjustedMin - 调整后的最小价格
 * @param {number} adjustedRange - 调整后的价格范围
 * @param {number} candleSpacing - K线间距
 */
function drawMovingAverages(ctx, viewData, padding, chartHeight, adjustedMin, adjustedRange, candleSpacing) {
    // 定义均线配置（MA5、MA10和MA20）
    const maConfigs = [
        { arr: viewData.ma5Arr, color: '#2962FF', label: 'MA5', lineWidth: 1.5 },
        { arr: viewData.ma10Arr, color: '#FF6D00', label: 'MA10', lineWidth: 1.5 },
        { arr: viewData.ma20Arr, color: '#FFD700', label: 'MA20', lineWidth: 1.5 }
    ];

    // 计算Y坐标的辅助函数
    const getY = (price) => {
        return padding + chartHeight - ((price - adjustedMin) / adjustedRange) * chartHeight;
    };

    // 绘制每条均线
    maConfigs.forEach(config => {
        if (!config.arr || config.arr.length === 0) return;

        ctx.strokeStyle = config.color;
        ctx.lineWidth = config.lineWidth;
        ctx.beginPath();

        let isFirstPoint = true;

        config.arr.forEach((value, candleIndex) => {
            if (value === null || value === undefined) return;
            const x = padding + candleIndex * candleSpacing + candleSpacing / 2;
            const y = getY(value);

            if (isFirstPoint) {
                ctx.moveTo(x, y);
                isFirstPoint = false;
            } else {
                ctx.lineTo(x, y);
            }
        });

        ctx.stroke();
    });

    // 绘制均线图例
    drawMALegend(ctx, maConfigs, padding);
}

/**
 * 绘制均线图例
 * @param {CanvasRenderingContext2D} ctx - Canvas上下文
 * @param {Array} maConfigs - 均线配置数组
 * @param {number} padding - 内边距
 */
function drawMALegend(ctx, maConfigs, padding) {
    const legendX = padding + 20;
    const legendY = padding + 20;
    const lineHeight = 18;

    ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
    ctx.textAlign = 'left';

    maConfigs.forEach((config, index) => {
        if (!config.arr || config.arr.length === 0) return;

        const y = legendY + index * lineHeight;

        // 绘制颜色块
        ctx.fillStyle = config.color;
        ctx.fillRect(legendX, y - 8, 12, 2);

        // 绘制标签
        ctx.fillStyle = config.color;
        ctx.fillText(config.label, legendX + 18, y);
    });
}

/**
 * 绘制K线图表
 * @param {CanvasRenderingContext2D} ctx - Canvas上下文
 * @param {HTMLCanvasElement} canvas - Canvas元素
 * @param {Object} viewData - 当前视图数据
 * @param {number} width - 绘制宽度
 * @param {number} height - 绘制高度
 * @returns {Object} 布局参数（十字光标定位用）
 */
function drawKlineChart(ctx, canvas, viewData, width, height) {
    // 获取设备像素比，用于高清显示
    const dpr = window.devicePixelRatio || 1;
    const displayWidth = width;
    const displayHeight = height;

    // 设置Canvas的实际绘制尺寸（高清）
    canvas.width = displayWidth * dpr;
    canvas.height = displayHeight * dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    const padding = 60;
    const chartWidth = width - padding * 2;

    // 为成交量图表留出空间，K线图占65%，成交量图占35%
    const klineHeight = (height - padding * 2) * 0.65;
    const volumeHeight = (height - padding * 2) * 0.35;
    const volumeStartY = padding + klineHeight;

    // 清空画布
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);

    // 启用文字抗锯齿
    ctx.textRendering = 'optimizeLegibility';
    ctx.imageSmoothingEnabled = true;

    // 获取价格范围
    const prices = viewData.candleData.map(d => [d.high, d.low]).flat();
    const minPrice = Math.min(...prices);
    const maxPrice = Math.max(...prices);
    const priceRange = maxPrice - minPrice || 1;

    // 添加价格范围的上下边距
    const paddingPercent = 0.1;
    const adjustedMin = minPrice - priceRange * paddingPercent;
    const adjustedMax = maxPrice + priceRange * paddingPercent;
    const adjustedRange = adjustedMax - adjustedMin;

    const count = viewData.candleData.length;

    // 计算K线宽度：随数据密度自适应，最小1px避免高密度下相互覆盖
    const candleSpacing = chartWidth / count;
    const candleWidth = Math.max(1, Math.min(15, Math.floor(candleSpacing * 0.6)));
    const denseMode = candleSpacing < 3;   // 高密度模式：影线并入实体，保持可读性

    // 绘制背景网格
    ctx.strokeStyle = '#e5e7eb';
    ctx.lineWidth = 0.5;

    // 水平网格线和价格标签
    for (let i = 0; i <= 5; i++) {
        const y = padding + (klineHeight / 5) * i;
        ctx.beginPath();
        ctx.moveTo(padding, y);
        ctx.lineTo(width - padding, y);
        ctx.stroke();

        // 绘制价格标签
        const price = adjustedMax - (adjustedRange / 5) * i;
        ctx.fillStyle = '#666';
        ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
        ctx.textAlign = 'right';
        ctx.fillText(price.toFixed(2), padding - 15, y + 4);
    }

    // 绘制竖直网格线和日期标签（固定约8条，密度不随数据量变化）
    const gridLines = 8;
    for (let i = 0; i <= gridLines; i++) {
        const x = padding + (chartWidth / gridLines) * i;
        ctx.beginPath();
        ctx.moveTo(x, padding);
        ctx.lineTo(x, height - padding);
        ctx.stroke();

        const dataIndex = Math.round((count - 1) * (i / gridLines));
        if (dataIndex >= 0 && dataIndex < count) {
            const candle = viewData.candleData[dataIndex];
            ctx.fillStyle = '#666';
            ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(fmtDate(candle.time), x, height - padding + 20);
        }
    }

    // 绘制K线
    viewData.candleData.forEach((item, index) => {
        const x = padding + index * candleSpacing + candleSpacing / 2;

        // 计算Y坐标
        const getY = (price) => {
            return padding + klineHeight - ((price - adjustedMin) / adjustedRange) * klineHeight;
        };

        const openY = getY(item.open);
        const closeY = getY(item.close);
        const highY = getY(item.high);
        const lowY = getY(item.low);

        // 判断涨跌
        const isUp = item.close >= item.open;
        const color = isUp ? UP_COLOR : DOWN_COLOR;

        // 绘制影线（高低价）；高密度模式下宽度趋近实体，影线不再单独描边
        ctx.strokeStyle = color;
        ctx.lineWidth = denseMode ? Math.min(1, candleWidth) : 1;
        if (!denseMode || candleWidth > 1) {
            ctx.beginPath();
            ctx.moveTo(x, highY);
            ctx.lineTo(x, lowY);
            ctx.stroke();
        }

        // 绘制K线实体
        ctx.fillStyle = color;
        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.abs(closeY - openY) || 1;
        ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    });

    // 绘制均线
    drawMovingAverages(ctx, viewData, padding, klineHeight, adjustedMin, adjustedRange, candleSpacing);

    // 绘制成交量图表
    drawVolumeChart(ctx, viewData, padding, volumeStartY, volumeHeight, candleWidth, candleSpacing);

    // 绘制坐标轴
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(padding, padding);
    ctx.lineTo(padding, height - padding);
    ctx.lineTo(width - padding, height - padding);
    ctx.stroke();

    // 绘制K线图和成交量图的分隔线
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding, volumeStartY);
    ctx.lineTo(width - padding, volumeStartY);
    ctx.stroke();

    // 绘制Y轴标签
    ctx.fillStyle = '#666';
    ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.save();
    ctx.translate(15, padding + klineHeight / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('价格 (¥)', 0, 0);
    ctx.restore();

    // 绘制成交量Y轴标签
    ctx.save();
    ctx.translate(15, volumeStartY + volumeHeight / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('成交量', 0, 0);
    ctx.restore();

    // 绘制X轴标签
    ctx.fillStyle = '#666';
    ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('交易日期', width / 2, height - 10);

    // 绘制标题
    ctx.fillStyle = '#1f2937';
    ctx.font = 'bold 16px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('K线图（日线）', padding + 10, padding - 20);

    // 返回布局参数（十字光标与提示框定位用）
    return {
        padding,
        chartWidth,
        candleSpacing,
        candleWidth,
        count,
        klineHeight,
        volumeHeight,
        volumeStartY,
        adjustedMin,
        adjustedRange,
        prices,
        closes: viewData.candleData.map(d => d.close),
    };
}

/**
 * 绘制成交量图表
 * @param {CanvasRenderingContext2D} ctx - Canvas上下文
 * @param {Object} viewData - 当前视图数据
 * @param {number} padding - 内边距
 * @param {number} volumeStartY - 成交量图表起始Y坐标
 * @param {number} volumeHeight - 成交量图表高度
 * @param {number} candleWidth - K线宽度
 * @param {number} candleSpacing - K线间距
 */
function drawVolumeChart(ctx, viewData, padding, volumeStartY, volumeHeight, candleWidth, candleSpacing) {
    // 获取成交量范围
    const volumes = viewData.volumeData.map(d => d.value);
    const maxVolume = volumes.length > 0 ? Math.max(...volumes) : 1;

    // 绘制成交量柱状图
    viewData.volumeData.forEach((item, index) => {
        const x = padding + index * candleSpacing + candleSpacing / 2;

        // 计算Y坐标
        const volumeY = volumeStartY + volumeHeight - (item.value / maxVolume) * volumeHeight;
        const volumeBarHeight = volumeHeight - (volumeY - volumeStartY);

        // 绘制成交量柱状图
        ctx.fillStyle = item.color;
        ctx.fillRect(x - candleWidth / 2, volumeY, candleWidth, volumeBarHeight);
    });

    // 绘制成交量网格线
    ctx.strokeStyle = '#e5e7eb';
    ctx.lineWidth = 0.5;

    for (let i = 1; i <= 3; i++) {
        const y = volumeStartY + (volumeHeight / 3) * i;
        ctx.beginPath();
        ctx.moveTo(padding, y);
        // 使用与K线相同的长度来计算网格线宽度，确保对齐
        ctx.lineTo(padding + (candleSpacing * viewData.candleData.length), y);
        ctx.stroke();
    }

    // 绘制成交量最大值标签
    ctx.fillStyle = '#999';
    ctx.font = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(formatVolume(maxVolume), padding + 4, volumeStartY + 12);
}

/**
 * 计算简单移动平均线（SMA）
 * @param {Array} prices - 价格数组
 * @param {number} period - 周期（如5、10、20）
 * @returns {Array} 均线数据（与输入索引对齐，不足周期处为null）
 */
function calculateSMA(prices, period) {
    const sma = [];
    for (let i = 0; i < prices.length; i++) {
        if (i < period - 1) {
            sma.push(null);
        } else {
            let sum = 0;
            for (let j = i - period + 1; j <= i; j++) {
                sum += prices[j];
            }
            sma.push(sum / period);
        }
    }
    return sma;
}

/**
 * 转换数据格式（基于全量数据计算均线，保证切换范围时均线连续）
 * @param {Array} rawData - 原始数据数组（升序）
 * @returns {Object} 转换后的数据对象
 */
function formatKlineData(rawData) {
    // API返回升序数据（最早的在前，最新的在后）
    const candleData = [];
    const volumeData = [];
    const closePrices = [];

    // 遍历数据并转换格式
    rawData.forEach((item) => {
        // 转换日期为时间戳（秒）
        const date = new Date(item.date);
        const time = Math.floor(date.getTime() / 1000);

        // 只有当K线数据完整时，才添加所有数据
        if (item.open && item.high && item.low && item.close) {
            // K线数据
            candleData.push({
                time: time,
                open: item.open,
                high: item.high,
                low: item.low,
                close: item.close
            });
            closePrices.push(item.close);

            // 成交量数据
            const volumeValue = item.volume || 0;
            // 根据收盘价与开盘价判断颜色
            const color = item.close >= item.open ? UP_COLOR : DOWN_COLOR;
            volumeData.push({
                time: time,
                value: volumeValue,
                color: color
            });
        }
    });

    // 均线/KDJ均基于全量计算，返回与candleData索引对齐的数组（不足处为null）
    const ma5Arr = calculateSMA(closePrices, 5);
    const ma10Arr = calculateSMA(closePrices, 10);
    const ma20Arr = calculateSMA(closePrices, 20);
    const kArr = rawData.length === candleData.length
        ? rawData.map(d => (d.K !== null && d.K !== undefined) ? d.K : null)
        : candleData.map(() => null);
    const dArr = rawData.length === candleData.length
        ? rawData.map(d => (d.D !== null && d.D !== undefined) ? d.D : null)
        : candleData.map(() => null);
    const jArr = rawData.length === candleData.length
        ? rawData.map(d => (d.J !== null && d.J !== undefined) ? d.J : null)
        : candleData.map(() => null);

    return {
        candleData,
        volumeData,
        ma5Arr,
        ma10Arr,
        ma20Arr,
        kArr,
        dArr,
        jArr
    };
}

/**
 * 格式化成交量显示
 * @param {number} volume - 成交量
 * @returns {string} 格式化后的成交量
 */
function formatVolume(volume) {
    if (volume >= 1e8) {
        return (volume / 1e8).toFixed(2) + '亿';
    } else if (volume >= 1e4) {
        return (volume / 1e4).toFixed(2) + '万';
    } else {
        return volume.toString();
    }
}

/**
 * 销毁K线图表
 */
function destroyKlineChart() {
    if (klineChartInstance) {
        klineChartInstance.destroy();
        klineChartInstance = null;
    }
}
