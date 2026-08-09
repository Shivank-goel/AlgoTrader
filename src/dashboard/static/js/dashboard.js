const HEARTBEAT_INTERVAL = 2000;
const DATA_INTERVAL = 5000;
const UNIVERSE_INTERVAL = 30000;

let engineRunning = false;
let currentMode = 'demo';
let selectedSymbol = null;
let heartbeatTimer = null;
let dataTimer = null;
let universeTimer = null;

async function fetchJSON(url, options = {}) {
    let resp;
    try {
        resp = await fetch(url, options);
    } catch (networkErr) {
        throw new Error('Network error: ' + networkErr.message);
    }
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        let detail = resp.statusText;
        try { detail = JSON.parse(text).detail || detail; } catch (_) {}
        throw new Error(`${resp.status}: ${detail}`);
    }
    return resp.json();
}

function formatPrice(val) {
    if (val == null || isNaN(val)) return '—';
    return '$' + Number(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPct(val) {
    if (val == null || isNaN(val)) return '—';
    const sign = val >= 0 ? '+' : '';
    return sign + Number(val).toFixed(2) + '%';
}

function sideClass(side) {
    return side === 'long' ? 'side-long' : 'side-short';
}

function renderPositionStatusPanel(positions) {
    const container = document.getElementById('position-status-panel');
    const metaEl = document.getElementById('position-status-meta');
    if (!container) return;

    const count = positions.length;
    if (metaEl) {
        metaEl.textContent = count === 0
            ? 'No open positions'
            : `${count} open position${count === 1 ? '' : 's'}`;
    }

    if (!count) {
        container.innerHTML = '<p class="muted">No open positions — start the engine to trade</p>';
        return;
    }

    container.innerHTML = positions.map(p => {
        const progress = p.progress_pct != null ? Math.max(0, Math.min(100, p.progress_pct)) : null;
        const hasLevels = p.stop_loss != null && p.take_profit != null && progress != null;
        const pnlClass = (p.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative';
        const strategy = p.strategy_name
            ? `<span class="strategy-tag">${p.strategy_name}</span>`
            : '<span class="muted">—</span>';

        const progressBar = hasLevels ? `
            <div class="sl-tp-track">
                <span class="sl-tp-label sl">SL ${formatPrice(p.stop_loss)}</span>
                <div class="sl-tp-bar">
                    <div class="sl-tp-fill" style="width: ${progress}%"></div>
                    <div class="sl-tp-marker" style="left: ${progress}%"></div>
                </div>
                <span class="sl-tp-label tp">TP ${formatPrice(p.take_profit)}</span>
            </div>
        ` : `
            <div class="sl-tp-track sl-tp-missing">
                <span class="muted">Target / SL not set for this position</span>
            </div>
        `;

        return `
            <div class="position-card ${sideClass(p.side)}">
                <div class="position-card-header">
                    <div>
                        <span class="position-symbol">${p.symbol}</span>
                        <span class="position-side ${sideClass(p.side)}">${(p.side || '').toUpperCase()}</span>
                        ${strategy}
                    </div>
                    <div class="position-pnl ${pnlClass}">${formatCurrency(p.unrealized_pnl)}</div>
                </div>
                <div class="position-grid">
                    <div class="position-stat">
                        <span class="stat-label">Entry</span>
                        <span class="stat-value">${formatPrice(p.entry_price)}</span>
                    </div>
                    <div class="position-stat">
                        <span class="stat-label">Current</span>
                        <span class="stat-value">${formatPrice(p.current_price)}</span>
                    </div>
                    <div class="position-stat">
                        <span class="stat-label">Size</span>
                        <span class="stat-value">${Number(p.size).toFixed(4)}</span>
                    </div>
                    <div class="position-stat">
                        <span class="stat-label">Stop Loss</span>
                        <span class="stat-value sl-value">${formatPrice(p.stop_loss)}</span>
                        <span class="stat-sub">${formatPct(p.sl_distance_pct)} to SL</span>
                    </div>
                    <div class="position-stat">
                        <span class="stat-label">Target</span>
                        <span class="stat-value tp-value">${formatPrice(p.take_profit)}</span>
                        <span class="stat-sub">${formatPct(p.tp_distance_pct)} to TP</span>
                    </div>
                </div>
                ${progressBar}
            </div>
        `;
    }).join('');
}

function renderPositionsTable(positions) {
    renderTable('positions',
        ['Symbol', 'Side', 'Entry', 'Current', 'SL', 'TP', 'PnL'],
        positions.map(p => [
            p.symbol,
            `<span class="${sideClass(p.side)}">${p.side}</span>`,
            formatPrice(p.entry_price),
            formatPrice(p.current_price),
            formatPrice(p.stop_loss),
            formatPrice(p.take_profit),
            `<span class="${(p.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}">${Number(p.unrealized_pnl || 0).toFixed(2)}</span>`,
        ]),
        'No open positions'
    );
}

function formatCurrency(val) {
    if (val == null || isNaN(val)) return '—';
    return '$' + Number(val).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatUptime(seconds) {
    if (seconds == null) return '—';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m >= 60) {
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function timeAgo(iso) {
    if (!iso) return '—';
    const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 0) return 'just now';
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    return `${Math.floor(diff / 3600)}h ago`;
}

function showBanner(message, level = 'error') {
    const el = document.getElementById('alert-banner');
    if (!el) return;
    el.textContent = message;
    el.className = 'alert-banner' + (level === 'warn' ? ' warn' : '');
}

function hideBanner() {
    const el = document.getElementById('alert-banner');
    if (el) el.className = 'alert-banner hidden';
}

async function checkServerVersion() {
    try {
        const resp = await fetch('/api/mode');
        if (resp.status === 404) {
            showBanner(
                'Dashboard server is outdated. In Terminal run: bash scripts/start_dashboard.sh',
                'warn'
            );
            return false;
        }
        hideBanner();
        return resp.ok;
    } catch (e) {
        showBanner('Cannot reach dashboard API — is the server running?', 'warn');
        return false;
    }
}

function renderErrors(errors) {
    const container = document.getElementById('errors-log');
    const countEl = document.getElementById('errors-count');
    if (!container) return;
    countEl.textContent = `${errors.length} recent`;
    if (!errors.length) {
        container.innerHTML = '<p class="muted">No errors — system healthy</p>';
        return;
    }
    const rows = errors.map(e => [
        e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '—',
        e.symbol || '—',
        e.strategy || '—',
        `<span class="status-${e.status}">${e.status}</span>`,
        e.message || '',
    ]);
    renderTable('errors-log',
        ['Time', 'Symbol', 'Strategy', 'Type', 'Message'],
        rows,
        'No errors'
    );
}

async function pollErrors() {
    try {
        const data = await fetchJSON('/api/errors');
        renderErrors(data.errors || []);
    } catch (e) {
        if (String(e.message).includes('404')) return;
        console.error('Errors refresh failed:', e);
    }
}

function updateEngineUI(state, stale) {
    const badge = document.getElementById('state-badge');
    const pulse = document.getElementById('pulse-dot');
    const btn = document.getElementById('btn-engine');
    const staleEl = document.getElementById('hb-stale-warning');

    badge.textContent = (state || 'idle').toUpperCase();
    badge.className = 'state-badge ' + (state || 'idle');

    pulse.className = 'pulse-dot';
    const activeStates = ['running', 'scanning', 'degraded'];
    if (activeStates.includes(state)) pulse.classList.add('running');
    if (state === 'error') pulse.classList.add('error');

    engineRunning = activeStates.includes(state);

    if (engineRunning) {
        btn.textContent = 'Stop Engine';
        btn.className = 'btn btn-stop';
    } else {
        btn.textContent = 'Start Engine';
        btn.className = 'btn btn-start';
    }

    staleEl.classList.toggle('hidden', !stale);
}

function updateHeartbeat(hb) {
    updateEngineUI(hb.state, hb.stale);
    if (hb.mode && hb.mode !== currentMode) {
        currentMode = hb.mode;
        updateModeTabs(currentMode);
    }
    document.getElementById('hb-uptime').textContent = formatUptime(hb.uptime_seconds);
    document.getElementById('hb-last-scan').textContent = timeAgo(hb.last_scan_at);
    document.getElementById('hb-loops').textContent = hb.loop_count ?? 0;
    document.getElementById('hb-symbols').textContent =
        (hb.last_scan_symbols && hb.last_scan_symbols.length)
            ? hb.last_scan_symbols.join(', ')
            : '—';

    const errEl = document.getElementById('hb-errors');
    errEl.textContent = hb.errors ?? 0;
    errEl.className = 'hb-value' + ((hb.errors || 0) > 0 ? ' error-count' : '');
}

function updateModeTabs(mode) {
    document.querySelectorAll('.mode-tab').forEach(el => {
        const active = el.dataset.mode === mode;
        el.classList.toggle('active', active);
        el.setAttribute('aria-selected', String(active));
    });
}

async function switchMode(mode) {
    if (mode === currentMode) return;
    if (mode === 'live') {
        const ok = confirm(
            'Switch to LIVE mode? Real orders will be placed on your live Delta Exchange account. Continue?'
        );
        if (!ok) return;
    }
    try {
        const result = await fetchJSON('/api/mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });
        currentMode = result.mode || mode;
        updateModeTabs(currentMode);
        await loadProducts();
        await pollHeartbeat();
        await pollData();
        await pollUniverse();
    } catch (e) {
        alert('Mode switch failed: ' + e.message);
    }
}

async function rescanUniverse() {
    const btn = document.getElementById('btn-rescan');
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Rescanning…';
    try {
        await fetchJSON('/api/universe/rescan', { method: 'POST' });
        await pollUniverse();
    } catch (e) {
        alert('Rescan failed: ' + e.message);
    } finally {
        btn.textContent = original;
        btn.disabled = false;
    }
}

function renderUniverse(snapshot) {
    const metaEl = document.getElementById('universe-meta');
    const container = document.getElementById('universe-top');
    const scoreEl = document.getElementById('scoreboard');
    if (!snapshot) return;

    if (snapshot.last_scan_at) {
        const nextIn = snapshot.rescan_interval_hours;
        let text = `Last scan ${timeAgo(snapshot.last_scan_at)} · universe ${snapshot.universe_size} symbols · rescan every ${nextIn}h`;
        if (snapshot.last_error === 'scanner_empty' || snapshot.universe_size === 0) {
            text += ' · ⚠ exchange unreachable — showing fallback symbols';
            metaEl.style.color = 'var(--warning)';
        } else {
            metaEl.style.color = '';
        }
        metaEl.textContent = text;
    } else {
        metaEl.textContent = 'Universe not scanned yet — start the engine or click Rescan';
        metaEl.style.color = '';
    }

    const top = snapshot.top_active || [];
    if (top.length === 0) {
        container.innerHTML = '<p class="muted">No active assignments yet. Start the engine or click Rescan.</p>';
    } else {
        const rows = top.map((a, i) => [
            i + 1,
            `<strong>${a.symbol}</strong>`,
            `<span class="strategy-tag">${a.strategy}</span>`,
            `<span class="regime-tag">${a.regime}</span>`,
            `<span class="score-cell ${a.score >= 0 ? 'positive' : 'negative'}">${Number(a.score).toFixed(2)}</span>`,
            a.trades,
            (a.win_rate * 100).toFixed(0) + '%',
            Number(a.expectancy_pct).toFixed(2) + '%',
            Number(a.sharpe).toFixed(2),
            '$' + Number(a.turnover_usd_24h || 0).toLocaleString('en-US', { maximumFractionDigits: 0 }),
        ]);
        renderTable('universe-top',
            ['#', 'Symbol', 'Strategy', 'Regime', 'Score', 'Trades', 'Win%', 'Expectancy', 'Sharpe', '24h Turnover'],
            rows,
            'No assignments'
        );
    }

    const board = snapshot.scoreboard || [];
    if (board.length === 0) {
        scoreEl.innerHTML = '<p class="muted">No scoreboard yet.</p>';
    } else {
        const sorted = [...board].sort((a, b) => b.score - a.score).slice(0, 100);
        const rows = sorted.map(r => [
            `<strong>${r.symbol}</strong>`,
            `<span class="strategy-tag">${r.strategy}</span>`,
            `<span class="score-cell ${r.score >= 0 ? 'positive' : 'negative'}">${Number(r.score).toFixed(2)}</span>`,
            r.trades,
            (r.win_rate * 100).toFixed(0) + '%',
            Number(r.total_return_pct).toFixed(2) + '%',
            Number(r.expectancy_pct).toFixed(2) + '%',
            Number(r.sharpe).toFixed(2),
            Number(r.max_drawdown_pct).toFixed(1) + '%',
        ]);
        renderTable('scoreboard',
            ['Symbol', 'Strategy', 'Score', 'Trades', 'Win%', 'Total Ret', 'Expectancy', 'Sharpe', 'Max DD'],
            rows,
            'Empty scoreboard'
        );
    }
}

async function pollUniverse() {
    try {
        const snap = await fetchJSON('/api/universe');
        renderUniverse(snap);
    } catch (e) {
        console.error('Universe refresh failed:', e);
    }
}

function renderTable(containerId, headers, rows, emptyMsg = 'No data') {
    const container = document.getElementById(containerId);
    if (!rows.length) {
        container.innerHTML = `<p class="muted">${emptyMsg}</p>`;
        return;
    }
    let html = '<table><thead><tr>';
    headers.forEach(h => { html += `<th>${h}</th>`; });
    html += '</tr></thead><tbody>';
    rows.forEach(row => {
        html += '<tr>';
        row.forEach(cell => { html += `<td>${cell}</td>`; });
        html += '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function renderSignalLog(signals) {
    const container = document.getElementById('signal-log');
    if (!signals.length) {
        container.innerHTML = '<p class="muted">No signals yet — start the engine to scan</p>';
        return;
    }
    const rows = signals.map(s => {
        const time = s.timestamp ? new Date(s.timestamp).toLocaleTimeString() : '—';
        const conf = s.confidence != null ? (s.confidence * 100).toFixed(0) + '%' : '—';
        const statusClass = 'status-' + (s.status || 'scan');
        return [
            time,
            s.symbol || '—',
            s.strategy || '—',
            s.direction || '—',
            conf,
            `<span class="${statusClass}">${s.status}</span>`,
            s.message || '',
        ];
    });
    renderTable('signal-log',
        ['Time', 'Symbol', 'Strategy', 'Dir', 'Conf', 'Status', 'Detail'],
        rows,
        'No signals yet'
    );
}

function renderActivePairs(pairs) {
    const container = document.getElementById('active-pairs');
    if (!pairs.length) {
        container.innerHTML = '<p class="muted">No active pairs — add one above</p>';
        return;
    }
    container.innerHTML = pairs.map(p => {
        const regime = p.regime || 'unknown';
        const price = p.price != null ? '$' + Number(p.price).toLocaleString() : '—';
        const selected = p.symbol === selectedSymbol ? ' selected' : '';
        return `
            <div class="pair-card regime-${regime}${selected}" data-symbol="${p.symbol}">
                <div class="pair-info">
                    <span class="pair-symbol">${p.symbol}</span>
                    <span class="pair-meta">${price} · <span class="regime-tag">${regime}</span></span>
                </div>
                <button type="button" class="btn-remove" data-remove="${p.symbol}" title="Remove">×</button>
            </div>
        `;
    }).join('');

    container.querySelectorAll('.pair-card').forEach(el => {
        el.addEventListener('click', (e) => {
            if (e.target.classList.contains('btn-remove')) return;
            selectPair(el.dataset.symbol);
        });
    });
    container.querySelectorAll('.btn-remove').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await removePair(btn.dataset.remove);
        });
    });
}

async function loadProducts() {
    const select = document.getElementById('product-select');
    try {
        const data = await fetchJSON('/api/products');
        const products = data.products || [];
        select.innerHTML = '<option value="">Select perpetual…</option>';
        if (products.length === 0) {
            select.innerHTML = '<option value="">No products available</option>';
            return;
        }
        products.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.symbol;
            opt.textContent = p.symbol;
            select.appendChild(opt);
        });
        if (data.error) {
            console.warn('Products loaded from fallback — exchange unreachable:', data.error);
        }
    } catch (e) {
        console.error('Failed to load products:', e);
        select.innerHTML = '<option value="">Exchange unreachable</option>';
    }
}

async function addPair() {
    const select = document.getElementById('product-select');
    const symbol = select.value;
    if (!symbol) return;
    try {
        await fetchJSON('/api/pairs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol }),
        });
        select.value = '';
        await refreshPairs();
    } catch (e) {
        alert('Could not add pair: ' + e.message);
    }
}

async function removePair(symbol) {
    try {
        await fetchJSON(`/api/pairs/${symbol}`, { method: 'DELETE' });
        if (selectedSymbol === symbol) selectedSymbol = null;
        await refreshPairs();
    } catch (e) {
        alert('Could not remove pair: ' + e.message);
    }
}

async function refreshPairs() {
    try {
        const data = await fetchJSON('/api/pairs');
        renderActivePairs(data.pairs || []);
    } catch (e) {
        console.error('Pairs refresh failed:', e);
    }
}

async function selectPair(symbol) {
    selectedSymbol = symbol;
    await refreshPairs();
}

async function pollHeartbeat() {
    try {
        const hb = await fetchJSON('/api/heartbeat');
        updateHeartbeat(hb);
    } catch (e) {
        console.error('Heartbeat failed:', e);
    }
}

async function pollData() {
    try {
        const status = await fetchJSON('/api/status');
        document.getElementById('equity').textContent = formatCurrency(status.equity);
        const pnlEl = document.getElementById('daily-pnl');
        pnlEl.textContent = formatCurrency(status.daily_pnl || 0);
        pnlEl.className = 'metric ' + ((status.daily_pnl || 0) >= 0 ? 'positive' : 'negative');
        document.getElementById('drawdown').textContent =
            (status.drawdown_pct || 0).toFixed(1) + '%';
        document.getElementById('regime').textContent = status.regime || '—';

        const positions = status.positions || [];
        const openCount = positions.length;
        const openTradesEl = document.getElementById('open-trades');
        openTradesEl.textContent = openCount;
        openTradesEl.className = 'metric' + (openCount > 0 ? ' positive' : '');
        const detailEl = document.getElementById('open-trades-detail');
        if (openCount > 0) {
            detailEl.textContent = positions.map(p => `${p.symbol} ${p.side}`).join(', ');
        } else {
            detailEl.textContent = 'No open positions';
        }

        renderPositionStatusPanel(positions);
        renderPositionsTable(positions);
    } catch (e) {
        console.error('Status refresh failed:', e);
    }

    try {
        const strategies = await fetchJSON('/api/strategies');
        renderTable('strategies',
            ['Strategy', 'Score'],
            (strategies.strategies || []).map(s => [
                s.name, (s.score * 100).toFixed(0) + '%',
            ]),
            'No strategy data'
        );
    } catch (e) {
        console.error('Strategies refresh failed:', e);
    }

    try {
        const trades = await fetchJSON('/api/trades');
        renderTable('trades',
            ['Symbol', 'Side', 'Strategy', 'PnL', 'PnL %'],
            (trades.trades || []).map(t => [
                t.symbol, t.side, t.strategy,
                Number(t.pnl).toFixed(2), Number(t.pnl_pct).toFixed(1) + '%',
            ]),
            'No trades yet'
        );
    } catch (e) {
        console.error('Trades refresh failed:', e);
    }

    try {
        const signals = await fetchJSON('/api/signals');
        renderSignalLog(signals.signals || []);
    } catch (e) {
        console.error('Signals refresh failed:', e);
    }

    try {
        await pollErrors();
    } catch (e) {
        console.error('Errors refresh failed:', e);
    }

    try {
        await refreshPairs();
    } catch (e) {
        console.error('Pairs refresh failed:', e);
    }
}

async function toggleEngine() {
    const btn = document.getElementById('btn-engine');
    btn.disabled = true;
    try {
        if (engineRunning) {
            await fetchJSON('/api/engine/stop', { method: 'POST' });
        } else {
            await fetchJSON('/api/engine/start', { method: 'POST' });
        }
        await pollHeartbeat();
        await pollData();
    } catch (e) {
        alert('Engine control failed: ' + e.message);
    } finally {
        btn.disabled = false;
    }
}

function bootDashboard() {
    checkServerVersion();
    loadMode();
    loadProducts();
    pollHeartbeat();
    pollData();
    pollUniverse();
    pollErrors();

    document.getElementById('btn-engine').addEventListener('click', toggleEngine);
    document.getElementById('btn-add-pair').addEventListener('click', addPair);
    const rescanBtn = document.getElementById('btn-rescan');
    if (rescanBtn) rescanBtn.addEventListener('click', rescanUniverse);

    document.querySelectorAll('.mode-tab').forEach(el => {
        el.addEventListener('click', () => switchMode(el.dataset.mode));
    });

    heartbeatTimer = setInterval(pollHeartbeat, HEARTBEAT_INTERVAL);
    dataTimer = setInterval(pollData, DATA_INTERVAL);
    universeTimer = setInterval(pollUniverse, UNIVERSE_INTERVAL);
}

document.addEventListener('DOMContentLoaded', () => {
    bootDashboard();
});

async function loadMode() {
    try {
        const data = await fetchJSON('/api/mode');
        currentMode = data.mode || 'demo';
        updateModeTabs(currentMode);
    } catch (e) {
        console.error('Mode load failed:', e);
    }
}
