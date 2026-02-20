/**
 * EDINET Large Shareholding Monitor - Frontend Application
 * Bloomberg terminal-style real-time dashboard
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    filings: [],
    watchlist: [],
    stats: {},
    connected: false,
    soundEnabled: true,
    notificationsEnabled: false,
    searchQuery: '',
    filterMode: 'all', // all | new | change | amendment
    sortMode: 'time-desc',
    viewMode: 'cards', // cards | table
    selectedDate: new Date().toISOString().slice(0, 10), // YYYY-MM-DD
};

let eventSource = null;
let audioCtx = null;
let pollCountdownInterval = null;
let lastPollTime = Date.now();
let currentModalDocId = null; // tracks which filing is shown in the modal
const POLL_INTERVAL_MS = 60000; // matches server default

// ---------------------------------------------------------------------------
// Stock Data Cache & Fetcher
// ---------------------------------------------------------------------------

const stockCache = {}; // { secCode: { data, fetchedAt } }
const STOCK_CACHE_TTL = 30 * 60 * 1000; // 30 minutes

async function fetchStockData(secCode) {
    if (!secCode) return null;
    // Normalize: strip trailing 0 if 5-digit code
    const code = secCode.length === 5 ? secCode.slice(0, 4) : secCode;

    // Check cache
    const cached = stockCache[code];
    if (cached && Date.now() - cached.fetchedAt < STOCK_CACHE_TTL) {
        return cached.data;
    }

    try {
        const resp = await fetch(`/api/stock/${code}`);
        if (!resp.ok) return null;
        const data = await resp.json();
        stockCache[code] = { data, fetchedAt: Date.now() };
        return data;
    } catch (e) {
        console.warn('Stock data fetch failed:', e);
        return null;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract target company name from doc_description when target_company_name is null.
 * Typical format: "変更報告書（トヨタ自動車株式）" or "大量保有報告書（ソニーグループ株式）"
 */
function extractTargetFromDescription(desc) {
    if (!desc) return null;
    const m = desc.match(/[（(]([^）)]+?)(?:株式|株券)[）)]/);
    return m ? m[1] : null;
}

// ---------------------------------------------------------------------------
// Mobile Detection
// ---------------------------------------------------------------------------

function isMobile() {
    return window.innerWidth <= 480;
}

// ---------------------------------------------------------------------------
// localStorage Persistence
// ---------------------------------------------------------------------------

const PREFS_PREFIX = 'edinet_';

function savePreferences() {
    try {
        const prefs = {
            filterMode: state.filterMode,
            searchQuery: state.searchQuery,
            soundEnabled: state.soundEnabled,
            notificationsEnabled: state.notificationsEnabled,
            viewMode: state.viewMode,
            watchlistPanelOpen: !document.getElementById('watchlist-panel').classList.contains('panel-collapsed'),
        };
        localStorage.setItem(PREFS_PREFIX + 'preferences', JSON.stringify(prefs));
    } catch (e) {
        console.warn('Failed to save preferences:', e);
    }
}

function loadPreferences() {
    try {
        const raw = localStorage.getItem(PREFS_PREFIX + 'preferences');
        if (!raw) return;
        const prefs = JSON.parse(raw);

        // Restore filter mode
        if (prefs.filterMode) {
            state.filterMode = prefs.filterMode;
            const filterEl = document.getElementById('feed-filter');
            if (filterEl) filterEl.value = prefs.filterMode;
        }

        // Restore search text
        if (prefs.searchQuery) {
            state.searchQuery = prefs.searchQuery;
            const searchEl = document.getElementById('feed-search');
            if (searchEl) searchEl.value = prefs.searchQuery;
        }

        // Restore sound preference
        if (typeof prefs.soundEnabled === 'boolean') {
            state.soundEnabled = prefs.soundEnabled;
            const btn = document.getElementById('btn-sound');
            if (btn) {
                btn.classList.toggle('active', state.soundEnabled);
                btn.title = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF';
                btn.setAttribute('aria-pressed', state.soundEnabled);
                btn.setAttribute('aria-label',
                    state.soundEnabled ? 'サウンドアラート: 有効' : 'サウンドアラート: 無効');
            }
        }

        // Restore notification preference
        if (typeof prefs.notificationsEnabled === 'boolean') {
            state.notificationsEnabled = prefs.notificationsEnabled;
            const btn = document.getElementById('btn-notify');
            if (btn) {
                btn.classList.toggle('active', state.notificationsEnabled);
                btn.title = state.notificationsEnabled ? '通知 ON' : '通知 OFF';
                btn.setAttribute('aria-pressed', state.notificationsEnabled);
                btn.setAttribute('aria-label',
                    state.notificationsEnabled ? 'デスクトップ通知: 有効' : 'デスクトップ通知: 無効');
            }
        }

        // Restore view mode
        if (prefs.viewMode === 'table' || prefs.viewMode === 'cards') {
            state.viewMode = prefs.viewMode;
            const cardsBtn = document.getElementById('view-cards');
            const tableBtn = document.getElementById('view-table');
            if (cardsBtn && tableBtn) {
                cardsBtn.classList.toggle('active', state.viewMode === 'cards');
                cardsBtn.setAttribute('aria-pressed', state.viewMode === 'cards');
                tableBtn.classList.toggle('active', state.viewMode === 'table');
                tableBtn.setAttribute('aria-pressed', state.viewMode === 'table');
            }
        }

        // Restore watchlist panel open/closed state
        if (typeof prefs.watchlistPanelOpen === 'boolean' && !prefs.watchlistPanelOpen) {
            const panel = document.getElementById('watchlist-panel');
            if (panel) panel.classList.add('panel-collapsed');
        }
    } catch (e) {
        console.warn('Failed to load preferences:', e);
    }
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    // iOS viewport height fix (100vh includes address bar)
    function setVH() {
        const vh = window.innerHeight * 0.01;
        document.documentElement.style.setProperty('--vh', `${vh}px`);
    }
    setVH();
    window.addEventListener('resize', setVH);

    loadPreferences();
    initClock();
    initPollCountdown();
    initSSE();
    initEventListeners();
    initDateNav();
    loadInitialData();
    initMobileNav();
    initPullToRefresh();
    initStockView();
});

function initClock() {
    const clockEl = document.getElementById('current-time');
    function update() {
        const now = new Date();
        clockEl.textContent = now.toLocaleString('ja-JP', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
        // Update mobile clock
        const mobileClockEl = document.getElementById('mobile-clock');
        if (mobileClockEl) mobileClockEl.textContent = clockEl.textContent;
    }
    update();
    setInterval(update, 1000);
}

function initPollCountdown() {
    const el = document.getElementById('poll-countdown');
    if (!el) return;

    function update() {
        const elapsed = Date.now() - lastPollTime;
        const remaining = Math.max(0, Math.ceil((POLL_INTERVAL_MS - elapsed) / 1000));
        el.textContent = `${remaining}s`;
        el.classList.toggle('soon', remaining <= 10);
    }

    update();
    pollCountdownInterval = setInterval(update, 1000);
}

function initEventListeners() {
    // Sound toggle
    document.getElementById('btn-sound').addEventListener('click', () => {
        state.soundEnabled = !state.soundEnabled;
        const btn = document.getElementById('btn-sound');
        btn.classList.toggle('active', state.soundEnabled);
        btn.title = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF';
        btn.setAttribute('aria-pressed', state.soundEnabled);
        btn.setAttribute('aria-label',
            state.soundEnabled ? 'サウンドアラート: 有効' : 'サウンドアラート: 無効');
        savePreferences();
    });

    // Notification permission
    document.getElementById('btn-notify').addEventListener('click', async () => {
        if (!('Notification' in window)) {
            alert('このブラウザはデスクトップ通知に対応していません');
            return;
        }
        const perm = await Notification.requestPermission();
        state.notificationsEnabled = perm === 'granted';
        const btn = document.getElementById('btn-notify');
        btn.classList.toggle('active', state.notificationsEnabled);
        btn.title = state.notificationsEnabled ? '通知 ON' : '通知 OFF';
        btn.setAttribute('aria-pressed', state.notificationsEnabled);
        btn.setAttribute('aria-label',
            state.notificationsEnabled ? 'デスクトップ通知: 有効' : 'デスクトップ通知: 無効');
        savePreferences();
    });

    // Manual poll
    document.getElementById('btn-poll').addEventListener('click', async () => {
        const btn = document.getElementById('btn-poll');
        btn.disabled = true;
        btn.style.opacity = '0.5';
        try {
            await fetch('/api/poll', { method: 'POST' });
            lastPollTime = Date.now();
        } catch (e) {
            console.error('Poll trigger failed:', e);
        }
        setTimeout(() => {
            btn.disabled = false;
            btn.style.opacity = '1';
        }, 3000);
    });

    // Feed search
    document.getElementById('feed-search').addEventListener('input', (e) => {
        state.searchQuery = e.target.value.toLowerCase();
        renderFeed();
        savePreferences();
    });

    // Feed filter
    document.getElementById('feed-filter').addEventListener('change', (e) => {
        state.filterMode = e.target.value;
        renderFeed();
        savePreferences();
    });

    document.getElementById('feed-sort').addEventListener('change', (e) => {
        state.sortMode = e.target.value;
        renderFeed();
    });

    // View toggle (cards / table)
    document.getElementById('view-cards').addEventListener('click', () => {
        state.viewMode = 'cards';
        document.getElementById('view-cards').classList.add('active');
        document.getElementById('view-cards').setAttribute('aria-pressed', 'true');
        document.getElementById('view-table').classList.remove('active');
        document.getElementById('view-table').setAttribute('aria-pressed', 'false');
        renderFeed();
        savePreferences();
    });
    document.getElementById('view-table').addEventListener('click', () => {
        state.viewMode = 'table';
        document.getElementById('view-table').classList.add('active');
        document.getElementById('view-table').setAttribute('aria-pressed', 'true');
        document.getElementById('view-cards').classList.remove('active');
        document.getElementById('view-cards').setAttribute('aria-pressed', 'false');
        renderFeed();
        savePreferences();
    });

    // Watchlist add
    document.getElementById('btn-add-watch').addEventListener('click', () => {
        document.getElementById('watchlist-form').classList.toggle('hidden');
        document.getElementById('watch-name').focus();
    });

    // Watchlist panel stock chart button
    const watchlistStockBtn = document.getElementById('btn-watchlist-stock');
    if (watchlistStockBtn) {
        watchlistStockBtn.addEventListener('click', () => showStockView());
    }

    document.getElementById('btn-cancel-watch').addEventListener('click', () => {
        document.getElementById('watchlist-form').classList.add('hidden');
    });

    document.getElementById('btn-save-watch').addEventListener('click', saveWatchItem);

    // Enter key in watchlist form
    document.getElementById('watch-name').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') saveWatchItem();
    });
    document.getElementById('watch-code').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') saveWatchItem();
    });

    // Modal close
    document.querySelector('#detail-modal .modal-close').addEventListener('click', closeModal);
    document.querySelector('#detail-modal .modal-overlay').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeModal();
            closeConfirmDialog();
        }
        // Arrow key navigation in modal
        const modal = document.getElementById('detail-modal');
        if (modal && !modal.classList.contains('hidden')) {
            const idx = parseInt(modal.dataset.filingIndex, 10);
            if (isNaN(idx) || idx < 0) return;
            if (e.key === 'ArrowLeft' && idx > 0) {
                openModal(state.filings[idx - 1]);
            } else if (e.key === 'ArrowRight' && idx < state.filings.length - 1) {
                openModal(state.filings[idx + 1]);
            }
        }
    });
}

// ---------------------------------------------------------------------------
// SSE Connection
// ---------------------------------------------------------------------------

function initSSE() {
    if (eventSource) {
        eventSource.close();
    }

    setConnectionStatus('reconnecting');
    eventSource = new EventSource('/api/stream');

    eventSource.addEventListener('connected', () => {
        setConnectionStatus('connected');
        console.log('SSE connected');
    });

    eventSource.addEventListener('new_filing', (e) => {
        const filing = JSON.parse(e.data);
        handleNewFiling(filing);
    });

    eventSource.addEventListener('stats_update', (e) => {
        // Refresh stats when notified
        loadStats();
    });

    eventSource.onopen = () => {
        setConnectionStatus('connected');
    };

    eventSource.onerror = () => {
        // EventSource readyState: 0=CONNECTING, 1=OPEN, 2=CLOSED
        if (eventSource.readyState === EventSource.CONNECTING) {
            setConnectionStatus('reconnecting');
        } else {
            setConnectionStatus('disconnected');
        }
        // EventSource auto-reconnects
    };
}

function setConnectionStatus(status) {
    // status: 'connected' | 'disconnected' | 'reconnecting'
    state.connected = status === 'connected';
    const el = document.getElementById('connection-status');
    const dot = el.querySelector('.status-dot');
    const text = el.querySelector('.status-text');

    // Remove all state classes
    el.className = 'connection-status';
    dot.className = 'status-dot';

    if (status === 'connected') {
        el.classList.add('connected');
        dot.classList.add('connected');
        text.textContent = 'LIVE';
    } else if (status === 'reconnecting') {
        el.classList.add('reconnecting');
        dot.classList.add('reconnecting');
        text.textContent = 'RECONNECTING...';
    } else {
        el.classList.add('disconnected');
        dot.classList.add('disconnected');
        text.textContent = 'DISCONNECTED';
    }

    // Update mobile connection status
    const mobileStatus = document.getElementById('mobile-connection-status');
    if (mobileStatus) {
        mobileStatus.textContent = status === 'connected' ? 'LIVE' :
                                    status === 'reconnecting' ? 'RECONNECTING...' : 'DISCONNECTED';
        mobileStatus.className = status === 'connected' ? 'text-green' :
                                  status === 'reconnecting' ? 'text-amber' : 'text-red';
    }
}

// ---------------------------------------------------------------------------
// Data Loading
// ---------------------------------------------------------------------------

async function loadInitialData() {
    await Promise.all([loadFilings(), loadStats(), loadWatchlist()]);
}

async function loadFilings() {
    try {
        const params = new URLSearchParams({ limit: '500' });
        if (state.selectedDate) {
            params.set('date_from', state.selectedDate);
            params.set('date_to', state.selectedDate);
        }
        const resp = await fetch(`/api/filings?${params}`);
        const data = await resp.json();
        state.filings = data.filings || [];
        renderFeed();
        updateTicker();
        preloadStockData();
    } catch (e) {
        console.error('Failed to load filings:', e);
    }
}

function preloadStockData() {
    const codes = new Set();
    for (const f of state.filings) {
        // Try target_sec_code first, then sec_code (EDINET API provides issuer code)
        const code = f.target_sec_code || f.sec_code;
        if (code) {
            const normalized = code.length === 5 ? code.slice(0, 4) : code;
            codes.add(normalized);
        }
    }
    if (codes.size === 0) return;

    // Fetch ALL unique codes — on mobile, market cap is prominently shown on every card
    // Use small stagger to avoid hammering the backend
    const staggerMs = 100;
    const promises = [];
    let delay = 0;
    for (const code of codes) {
        if (stockCache[code]) continue;
        promises.push(new Promise(resolve => {
            setTimeout(() => fetchStockData(code).then(resolve, resolve), delay);
        }));
        delay += staggerMs;
    }
    // Re-render feed once all preloads finish so table/cards show market data
    if (promises.length > 0) {
        Promise.all(promises).then(() => renderFeed());
    }
}

async function loadStats() {
    try {
        const params = state.selectedDate ? `?date=${state.selectedDate}` : '';
        const resp = await fetch(`/api/stats${params}`);
        state.stats = await resp.json();
        renderStats();
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

async function loadWatchlist() {
    try {
        const resp = await fetch('/api/watchlist');
        const data = await resp.json();
        state.watchlist = data.watchlist || [];
        renderWatchlist();
    } catch (e) {
        console.error('Failed to load watchlist:', e);
    }
}

// ---------------------------------------------------------------------------
// New Filing Handler
// ---------------------------------------------------------------------------

function handleNewFiling(filing) {
    lastPollTime = Date.now();
    // Add to top of list
    state.filings.unshift(filing);

    // Re-render
    renderFeed();
    updateTicker();
    loadStats(); // Refresh stats

    // Flash the new card (works for both desktop .feed-card and mobile .m-card)
    setTimeout(() => {
        const firstCard = document.querySelector('.feed-card, .m-card');
        if (firstCard) {
            firstCard.classList.add('flash');
            firstCard.addEventListener('animationend', () => {
                firstCard.classList.remove('flash');
            }, { once: true });
        }
    }, 50);

    // Play sound
    if (state.soundEnabled) {
        playAlertSound();
    }

    // Desktop notification
    if (state.notificationsEnabled) {
        sendDesktopNotification(filing);
    }

    // Check watchlist
    checkWatchlistMatch(filing);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderFeed() {
    const container = document.getElementById('feed-list');
    let filtered = [...state.filings];

    // Apply filter
    if (state.filterMode === 'new') {
        filtered = filtered.filter(f => !f.is_amendment && f.doc_description && !f.doc_description.includes('変更'));
    } else if (state.filterMode === 'change') {
        filtered = filtered.filter(f => !f.is_amendment && f.doc_description && f.doc_description.includes('変更'));
    } else if (state.filterMode === 'amendment') {
        filtered = filtered.filter(f => f.is_amendment);
    }

    // Apply search
    if (state.searchQuery) {
        const q = state.searchQuery;
        filtered = filtered.filter(f =>
            (f.filer_name || '').toLowerCase().includes(q) ||
            (f.holder_name || '').toLowerCase().includes(q) ||
            (f.target_company_name || '').toLowerCase().includes(q) ||
            (f.doc_description || '').toLowerCase().includes(q) ||
            (f.sec_code || '').includes(q) ||
            (f.target_sec_code || '').includes(q)
        );
    }

    // Sort filings
    filtered.sort((a, b) => {
        switch (state.sortMode) {
            case 'time-asc':
                return (a.submit_date_time || '').localeCompare(b.submit_date_time || '');
            case 'ratio-desc':
                return (b.holding_ratio ?? -1) - (a.holding_ratio ?? -1);
            case 'ratio-asc':
                return (a.holding_ratio ?? 999) - (b.holding_ratio ?? 999);
            case 'change-desc':
                return (b.ratio_change ?? -999) - (a.ratio_change ?? -999);
            case 'change-asc':
                return (a.ratio_change ?? 999) - (b.ratio_change ?? 999);
            case 'time-desc':
            default:
                return (b.submit_date_time || '').localeCompare(a.submit_date_time || '');
        }
    });

    if (filtered.length === 0) {
        container.innerHTML = `<div class="feed-empty">
            <div class="empty-icon">&#128196;</div>
            <div class="empty-text">報告書が見つかりません</div>
        </div>`;
        return;
    }

    const mobile = isMobile();

    if (!mobile && state.viewMode === 'table') {
        renderFeedTable(container, filtered);
    } else if (mobile) {
        // Mobile: use dedicated mobile card layout
        container.innerHTML = filtered.map(f => createMobileFeedCard(f)).join('');

        container.querySelectorAll('.m-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.tagName === 'A') return;
                const docId = card.dataset.docId;
                const filing = state.filings.find(f => f.doc_id === docId);
                if (filing) openModal(filing);
            });
        });
    } else {
        container.innerHTML = filtered.map(f => createFeedCard(f)).join('');

        // Add click handlers
        container.querySelectorAll('.feed-card').forEach(card => {
            card.addEventListener('click', (e) => {
                // Don't open modal if clicking a link
                if (e.target.tagName === 'A') return;
                const docId = card.dataset.docId;
                const filing = state.filings.find(f => f.doc_id === docId);
                if (filing) openModal(filing);
            });
        });
    }

    renderSummary();
}

function isWatchlistMatch(f) {
    for (const w of state.watchlist) {
        if (w.sec_code && (f.sec_code === w.sec_code || f.target_sec_code === w.sec_code)) return true;
        if (w.edinet_code && (f.subject_edinet_code === w.edinet_code || f.issuer_edinet_code === w.edinet_code)) return true;
        if (w.company_name && (
            (f.target_company_name || '').includes(w.company_name) ||
            (f.filer_name || '').includes(w.company_name)
        )) return true;
    }
    return false;
}

function renderFeedTable(container, filings) {
    const mobile = window.innerWidth <= 768;
    const phone = window.innerWidth <= 480;
    let html = `<div class="feed-table-wrapper"><table class="feed-table">
        <thead><tr>
            ${phone ? '' : '<th class="col-type">種別</th>'}
            <th class="col-filer">提出者</th>
            <th class="col-target">対象</th>
            <th class="col-ratio">割合</th>
            <th class="col-change">変動</th>
            <th class="col-mcap">時価総額</th>
            ${mobile ? '' : '<th class="col-prev">前回</th>'}
            ${mobile ? '' : '<th class="col-pbr">PBR</th>'}
            ${mobile ? '' : '<th class="col-price">株価</th>'}
            ${mobile ? '' : '<th class="col-time">時刻</th>'}
            <th class="col-links"></th>
        </tr></thead><tbody>`;

    for (const f of filings) {
        const time = f.submit_date_time
            ? f.submit_date_time.split(' ').pop() || f.submit_date_time
            : '-';

        // Type badge
        let typeBadge;
        if (f.is_amendment) {
            typeBadge = '<span class="badge-amendment tbl-badge">訂正</span>';
        } else if (f.doc_description && f.doc_description.includes('変更')) {
            typeBadge = '<span class="badge-change tbl-badge">変更</span>';
        } else {
            typeBadge = '<span class="badge-new tbl-badge">新規</span>';
        }
        if (f.is_special_exemption) {
            typeBadge += ' <span class="badge-special tbl-badge">特例</span>';
        }

        const filer = escapeHtml(f.holder_name || f.filer_name || '(不明)');
        const target = escapeHtml(f.target_company_name || extractTargetFromDescription(f.doc_description) || '(対象不明)');
        const secCode = f.target_sec_code || f.sec_code || '';
        const codeDisplay = secCode ? `<span class="tbl-code">${escapeHtml(secCode)}</span>` : '';

        // Ratio
        let ratioHtml = '<span class="text-dim">-</span>';
        if (f.holding_ratio != null) {
            const cls = f.ratio_change > 0 ? 'positive' : f.ratio_change < 0 ? 'negative' : '';
            ratioHtml = `<span class="${cls}">${f.holding_ratio.toFixed(2)}%</span>`;
        }

        // Change
        let changeHtml = '<span class="text-dim">-</span>';
        if (f.ratio_change != null && f.ratio_change !== 0) {
            const cls = f.ratio_change > 0 ? 'positive' : 'negative';
            const arrow = f.ratio_change > 0 ? '▲' : '▼';
            const sign = f.ratio_change > 0 ? '+' : '';
            changeHtml = `<span class="tbl-change ${cls}">${arrow}${sign}${f.ratio_change.toFixed(2)}%</span>`;
        }

        // Previous ratio
        const prev = f.previous_holding_ratio != null ? f.previous_holding_ratio.toFixed(2) + '%' : '-';

        // Market data from stock cache
        const code = secCode ? (secCode.length === 5 ? secCode.slice(0, 4) : secCode) : '';
        const cached = code ? stockCache[code] : null;
        const sd = cached && cached.data ? cached.data : null;
        const loadingHint = code && !cached ? '<span class="text-dim tbl-loading">...</span>' : '<span class="text-dim">-</span>';
        const mcap = sd && sd.market_cap_display ? sd.market_cap_display : loadingHint;
        const pbr = sd && sd.pbr != null ? Number(sd.pbr).toFixed(2) + '倍' : (code && !cached ? '...' : '-');
        const price = sd && sd.current_price != null ? '\u00a5' + Math.round(sd.current_price).toLocaleString() : (code && !cached ? '...' : '-');

        // Links
        let links = '';
        if (f.pdf_url) {
            links += `<a href="${f.pdf_url}" target="_blank" rel="noopener" class="tbl-link" onclick="event.stopPropagation()">PDF</a>`;
        }

        // Row class
        let rowClass = '';
        if (f.ratio_change > 0) rowClass = 'row-up';
        else if (f.ratio_change < 0) rowClass = 'row-down';
        if (isWatchlistMatch(f)) rowClass += ' row-watch';

        html += `<tr class="${rowClass}" data-doc-id="${escapeHtml(f.doc_id)}">
            ${phone ? '' : `<td class="col-type">${typeBadge}</td>`}
            <td class="col-filer" title="${filer}">${filer}</td>
            <td class="col-target" title="${target}">${target}${codeDisplay ? ' ' + codeDisplay : ''}</td>
            <td class="col-ratio">${ratioHtml}</td>
            <td class="col-change">${changeHtml}</td>
            <td class="col-mcap">${mcap}</td>
            ${mobile ? '' : `<td class="col-prev">${prev}</td>`}
            ${mobile ? '' : `<td class="col-pbr">${pbr}</td>`}
            ${mobile ? '' : `<td class="col-price">${price}</td>`}
            ${mobile ? '' : `<td class="col-time">${escapeHtml(time)}</td>`}
            <td class="col-links">${links}</td>
        </tr>`;
    }

    html += '</tbody></table></div>';
    container.innerHTML = html;

    // Row click handler
    container.querySelectorAll('.feed-table tbody tr').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.tagName === 'A') return;
            const docId = row.dataset.docId;
            const filing = state.filings.find(f => f.doc_id === docId);
            if (filing) openModal(filing);
        });
    });
}

function createFeedCard(f) {
    const isChange = f.doc_description && f.doc_description.includes('変更');
    let cardClass = f.is_amendment ? 'amendment' : isChange ? 'change-report' : 'new-report';
    if (f.ratio_change > 0) cardClass += ' ratio-up';
    else if (f.ratio_change < 0) cardClass += ' ratio-down';

    // Badge
    let badge = '';
    if (f.is_amendment) {
        badge = '<span class="card-badge badge-amendment">訂正</span>';
    } else if (isChange) {
        badge = '<span class="card-badge badge-change">変更</span>';
    } else {
        badge = '<span class="card-badge badge-new">新規</span>';
    }

    if (f.is_special_exemption) {
        badge += '<span class="card-badge badge-special">特例</span>';
    }

    // Watchlist match badge
    if (isWatchlistMatch(f)) {
        badge += '<span class="card-badge badge-watchlist">WATCH</span>';
    }

    // Time
    const time = f.submit_date_time
        ? f.submit_date_time.split(' ').pop() || f.submit_date_time
        : '-';

    // Filer / Target
    const filer = f.holder_name || f.filer_name || '(不明)';
    const target = f.target_company_name || extractTargetFromDescription(f.doc_description) || '(対象不明)';
    const targetCode = (f.target_sec_code || f.sec_code) ? `[${f.target_sec_code || f.sec_code}]` : '';

    // Ratio with change indicator (improved visibility)
    let ratioHtml = '';
    if (f.holding_ratio != null) {
        const ratioClass = f.ratio_change > 0 ? 'positive' : f.ratio_change < 0 ? 'negative' : 'neutral';

        let changeHtml = '';
        if (f.ratio_change != null && f.ratio_change !== 0) {
            const arrow = f.ratio_change > 0 ? '▲' : '▼';
            const sign = f.ratio_change > 0 ? '+' : '';
            changeHtml = `<span class="ratio-change-pill ${ratioClass}">${arrow} ${sign}${f.ratio_change.toFixed(2)}%</span>`;
        }

        let prevHtml = '';
        if (f.previous_holding_ratio != null) {
            prevHtml = `<span class="ratio-prev">前回 ${f.previous_holding_ratio.toFixed(2)}%</span>`;
        }

        const barWidth = Math.min(f.holding_ratio, 100);
        const prevBarWidth = f.previous_holding_ratio != null ? Math.min(f.previous_holding_ratio, 100) : 0;
        const barHtml = `<div class="ratio-bar-container">${
            prevBarWidth > 0 ? `<div class="ratio-bar ratio-bar-prev" style="width: ${prevBarWidth}%"></div>` : ''
        }<div class="ratio-bar ratio-bar-curr ${ratioClass}" style="width: ${barWidth}%"></div></div>`;

        ratioHtml = `<div class="ratio-display ${ratioClass}">
            <div class="ratio-main-row">
                <span class="card-ratio ${ratioClass}">${f.holding_ratio.toFixed(2)}%</span>
                ${changeHtml}
            </div>
            ${prevHtml}
            ${barHtml}
        </div>`;
    } else {
        ratioHtml = '<div class="ratio-display neutral"><span class="card-ratio neutral">-</span></div>';
    }

    // Links
    let links = '';
    if (f.pdf_url) {
        links += `<a href="${f.pdf_url}" target="_blank" rel="noopener" class="card-link" onclick="event.stopPropagation()">PDF</a>`;
    }
    if (f.edinet_url) {
        links += `<a href="${f.edinet_url}" target="_blank" rel="noopener" class="card-link" onclick="event.stopPropagation()">EDINET</a>`;
    }

    // Market data from cache (desktop only)
    const secCode = f.target_sec_code || f.sec_code;
    let marketDataHtml = '';
    if (secCode) {
        const code = secCode.length === 5 ? secCode.slice(0, 4) : secCode;
        const cached = stockCache[code];
        if (cached && cached.data) {
            const sd = cached.data;
            const parts = [];
            if (sd.market_cap_display) parts.push(`時価:${sd.market_cap_display}`);
            if (sd.pbr != null) parts.push(`PBR:${Number(sd.pbr).toFixed(2)}倍`);
            if (sd.current_price != null) parts.push(`\u00a5${Math.round(sd.current_price).toLocaleString()}`);
            if (parts.length > 0) {
                marketDataHtml = `<div class="card-market-data">${parts.map(p => `<span>${p}</span>`).join('')}</div>`;
            }
        }
    }

    return `
        <div class="feed-card ${cardClass}" data-doc-id="${escapeHtml(f.doc_id)}" role="article">
            <div class="card-top">
                <div>${badge}</div>
                <span class="card-time">${escapeHtml(time)}</span>
            </div>
            <div class="card-main">
                <span class="card-filer">${escapeHtml(filer)}</span>
                <span class="card-arrow">&#x2192;</span>
                <span class="card-target">${escapeHtml(target)} ${escapeHtml(targetCode)}</span>
                <div class="card-desc">${escapeHtml(f.doc_description || '')}</div>
                ${marketDataHtml}
            </div>
            <div class="card-bottom">
                ${ratioHtml}
                <div class="card-links">${links}</div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Mobile Feed Card - completely different layout optimized for phone screens
// Target company as headline, ratio+market cap as hero metrics
// ---------------------------------------------------------------------------

function createMobileFeedCard(f) {
    const isChange = f.doc_description && f.doc_description.includes('変更');
    let cardClass = f.is_amendment ? 'amendment' : isChange ? 'change-report' : 'new-report';
    if (f.ratio_change > 0) cardClass += ' ratio-up';
    else if (f.ratio_change < 0) cardClass += ' ratio-down';

    // Badge
    let badge = '';
    if (f.is_amendment) {
        badge = '<span class="m-badge m-badge-amend">訂正</span>';
    } else if (isChange) {
        badge = '<span class="m-badge m-badge-change">変更</span>';
    } else {
        badge = '<span class="m-badge m-badge-new">新規</span>';
    }
    if (isWatchlistMatch(f)) {
        badge += '<span class="m-badge m-badge-watch">&#9733;</span>';
    }

    // Time
    const time = f.submit_date_time
        ? f.submit_date_time.split(' ').pop() || f.submit_date_time
        : '';

    // Target company = headline (most important on mobile)
    // Fall back to extracting from doc_description when XBRL wasn't parsed
    const target = f.target_company_name
        || extractTargetFromDescription(f.doc_description)
        || '(対象不明)';
    const secCode = f.target_sec_code || f.sec_code;
    const targetCode = secCode ? `[${secCode}]` : '';
    // Filer = secondary
    const filer = f.holder_name || f.filer_name || '(不明)';

    // Ratio metrics
    const ratioClass = f.ratio_change > 0 ? 'positive' : f.ratio_change < 0 ? 'negative' : 'neutral';
    const ratioVal = f.holding_ratio != null ? `${f.holding_ratio.toFixed(2)}%` : '<span class="text-dim" style="font-size:11px">割合未取得</span>';

    let changeHtml = '';
    if (f.ratio_change != null && f.ratio_change !== 0) {
        const arrow = f.ratio_change > 0 ? '▲' : '▼';
        const sign = f.ratio_change > 0 ? '+' : '';
        changeHtml = `<span class="m-change ${ratioClass}">${arrow}${sign}${f.ratio_change.toFixed(2)}%</span>`;
    }

    let prevHtml = '';
    if (f.previous_holding_ratio != null) {
        prevHtml = `<span class="m-prev">前回 ${f.previous_holding_ratio.toFixed(2)}%</span>`;
    }

    // Market data from stock cache (secCode already defined above)
    const code = secCode ? (secCode.length === 5 ? secCode.slice(0, 4) : secCode) : '';
    const cached = code ? stockCache[code] : null;
    const sd = cached && cached.data ? cached.data : null;

    const stockLoading = code && !cached;
    let mcapHtml = '';
    if (sd && sd.market_cap_display) {
        mcapHtml = `<span class="m-mcap">${sd.market_cap_display}</span>`;
    } else if (stockLoading) {
        mcapHtml = '<span class="m-mcap m-loading">時価総額...</span>';
    }

    let priceHtml = '';
    if (sd && sd.current_price != null) {
        priceHtml = `<span class="m-price">\u00a5${Math.round(sd.current_price).toLocaleString()}</span>`;
    } else if (stockLoading) {
        priceHtml = '<span class="m-price m-loading">...</span>';
    }

    let pbrHtml = '';
    if (sd && sd.pbr != null) {
        pbrHtml = `<span class="m-pbr">PBR ${sd.pbr.toFixed(2)}</span>`;
    }

    const hasMktData = mcapHtml || priceHtml || pbrHtml;
    const sep = hasMktData ? '<span class="m-sep"></span>' : '';

    // PDF link
    let linkHtml = '';
    if (f.pdf_url) {
        linkHtml = `<a href="${f.pdf_url}" target="_blank" rel="noopener" class="m-link" onclick="event.stopPropagation()">PDF</a>`;
    }

    return `<div class="m-card ${cardClass}" data-doc-id="${escapeHtml(f.doc_id)}">
    <div class="m-card-head">
        ${badge}
        <span class="m-target">${escapeHtml(target)}</span>
        <span class="m-code">${escapeHtml(targetCode)}</span>
        <span class="m-time">${escapeHtml(time)}</span>
    </div>
    <div class="m-card-data">
        <span class="m-ratio ${ratioClass}">${ratioVal}</span>
        ${changeHtml}
        ${sep}${mcapHtml}${priceHtml}${pbrHtml}
    </div>
    <div class="m-card-foot">
        <span class="m-filer">${escapeHtml(filer)}</span>
        ${prevHtml}
        ${linkHtml}
    </div>
</div>`;
}

function renderStats() {
    const s = state.stats;
    document.getElementById('stat-total').textContent = s.today_total ?? '-';
    // Update header filing count badges (desktop + mobile)
    const badge = document.getElementById('filing-count-badge');
    if (badge) badge.textContent = s.today_total ?? '0';
    const badgeMobile = document.getElementById('filing-count-badge-mobile');
    if (badgeMobile) badgeMobile.textContent = s.today_total ?? '0';
    document.getElementById('stat-new').textContent = s.today_new_reports ?? '-';
    document.getElementById('stat-amendments').textContent = s.today_amendments ?? '-';
    document.getElementById('stat-clients').textContent = s.connected_clients ?? '-';

    // Update panel title to show the selected date
    const isToday = state.selectedDate === new Date().toISOString().slice(0, 10);
    const statsTitle = document.querySelector('#stats-panel .panel-title');
    if (statsTitle) {
        statsTitle.textContent = isToday ? 'TODAY' : state.selectedDate;
    }

    // Top filers
    const filersList = document.getElementById('top-filers-list');
    if (s.top_filers && s.top_filers.length > 0) {
        filersList.innerHTML = s.top_filers.map(f =>
            `<div class="filer-row">
                <span class="filer-name" title="${escapeHtml(f.name || '')}">${escapeHtml(f.name || '(不明)')}</span>
                <span class="filer-count">${f.count}</span>
            </div>`
        ).join('');
    } else {
        filersList.innerHTML = '<div class="filers-empty">本日の提出なし</div>';
    }

    renderSummary();
}

function renderSummary() {
    const container = document.getElementById('summary-content');
    if (!container) return;

    const filings = state.filings.filter(f => f.ratio_change != null && f.ratio_change !== 0);

    if (filings.length === 0) {
        container.innerHTML = '<div class="summary-empty">データなし</div>';
        return;
    }

    const increases = filings.filter(f => f.ratio_change > 0);
    const decreases = filings.filter(f => f.ratio_change < 0);
    const avgChange = filings.reduce((sum, f) => sum + f.ratio_change, 0) / filings.length;

    let largestIncrease = null;
    let largestDecrease = null;

    for (const f of filings) {
        if (!largestIncrease || f.ratio_change > largestIncrease.ratio_change) {
            largestIncrease = f;
        }
        if (!largestDecrease || f.ratio_change < largestDecrease.ratio_change) {
            largestDecrease = f;
        }
    }

    const avgClass = avgChange > 0 ? 'positive' : avgChange < 0 ? 'negative' : 'neutral';
    const avgSign = avgChange > 0 ? '+' : '';

    let html = `
        <div class="summary-row">
            <span class="summary-label">増加</span>
            <span class="summary-value positive">${increases.length}件</span>
        </div>
        <div class="summary-row">
            <span class="summary-label">減少</span>
            <span class="summary-value negative">${decreases.length}件</span>
        </div>
        <div class="summary-row">
            <span class="summary-label">平均変動</span>
            <span class="summary-value ${avgClass}">${avgSign}${avgChange.toFixed(2)}%</span>
        </div>
    `;

    if (largestIncrease && largestIncrease.ratio_change > 0) {
        const name = largestIncrease.target_company_name || largestIncrease.filer_name || '不明';
        html += `
        <div class="summary-highlight">
            <div class="summary-highlight-label">最大増加</div>
            <div class="summary-highlight-value positive">+${largestIncrease.ratio_change.toFixed(2)}%</div>
            <div class="summary-highlight-company">${escapeHtml(name)}</div>
        </div>`;
    }

    if (largestDecrease && largestDecrease.ratio_change < 0) {
        const name = largestDecrease.target_company_name || largestDecrease.filer_name || '不明';
        html += `
        <div class="summary-highlight">
            <div class="summary-highlight-label">最大減少</div>
            <div class="summary-highlight-value negative">${largestDecrease.ratio_change.toFixed(2)}%</div>
            <div class="summary-highlight-company">${escapeHtml(name)}</div>
        </div>`;
    }

    container.innerHTML = html;
}

function renderWatchlist() {
    const container = document.getElementById('watchlist-items');
    if (state.watchlist.length === 0) {
        container.innerHTML = `<div class="watchlist-empty">
            <div class="empty-icon">&#9734;</div>
            <div class="empty-text">ウォッチリストに企業を追加してください</div>
        </div>`;
        return;
    }

    container.innerHTML = state.watchlist.map(w => {
        const code = w.sec_code ? (w.sec_code.length === 5 ? w.sec_code.slice(0, 4) : w.sec_code) : '';
        const cached = code ? stockCache[code] : null;
        let priceHtml = '';
        if (cached && cached.data && cached.data.current_price != null) {
            priceHtml = `<span class="watch-price">&yen;${Math.round(cached.data.current_price).toLocaleString()}</span>`;
        }
        return `<div class="watch-item" data-sec-code="${escapeHtml(code)}">
            <div class="watch-info">
                <span class="watch-name">${escapeHtml(w.company_name)}</span>
                <span class="watch-code">${escapeHtml(w.sec_code || '')} ${priceHtml}</span>
            </div>
            <canvas class="watch-sparkline" data-code="${escapeHtml(code)}" width="60" height="24"></canvas>
            <button class="watch-delete" data-id="${w.id}"
                    aria-label="削除: ${escapeHtml(w.company_name)}"
                    title="削除">&times;</button>
        </div>`;
    }).join('');

    // Delete handlers (with confirm dialog)
    container.querySelectorAll('.watch-delete').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const id = e.target.dataset.id;
            const item = state.watchlist.find(w => String(w.id) === String(id));
            if (item) {
                showDeleteConfirm(item.company_name, id);
            }
        });
    });

    // Click on watchlist item opens stock view
    container.querySelectorAll('.watch-item').forEach(item => {
        item.addEventListener('click', (e) => {
            if (e.target.closest('.watch-delete')) return;
            const code = item.dataset.secCode;
            if (code) {
                document.getElementById('stock-search-input').value = code;
                showStockView();
                loadStockView(code);
                // Activate stock nav on mobile
                document.querySelectorAll('.mobile-bottom-nav .nav-item').forEach(n => {
                    n.classList.toggle('active', n.dataset.panel === 'stock');
                });
            }
        });
    });

    // Render sparkline mini-charts for watchlist items
    renderWatchlistSparklines();

    // Re-render feed to update watchlist badges
    renderFeed();
}

function renderWatchlistSparklines() {
    document.querySelectorAll('.watch-sparkline').forEach(canvas => {
        const code = canvas.dataset.code;
        if (!code) return;
        const cached = stockCache[code];
        if (cached && cached.data && cached.data.weekly_prices) {
            renderSparkline(canvas, cached.data.weekly_prices, 60, 24);
        } else {
            // Fetch data if not cached yet
            fetchStockData(code).then(data => {
                if (data && data.weekly_prices) {
                    renderSparkline(canvas, data.weekly_prices, 60, 24);
                    // Update the price display too
                    const watchItem = canvas.closest('.watch-item');
                    if (watchItem && data.current_price != null) {
                        const codeEl = watchItem.querySelector('.watch-code');
                        if (codeEl && !codeEl.querySelector('.watch-price')) {
                            codeEl.innerHTML += ` <span class="watch-price">&yen;${Math.round(data.current_price).toLocaleString()}</span>`;
                        }
                    }
                }
            });
        }
    });
}

function updateTicker() {
    const recent = state.filings.slice(0, 10);
    if (recent.length === 0) {
        document.getElementById('ticker-text').textContent =
            'EDINET APIからデータを取得しています...';
        return;
    }

    const items = recent.map(f => {
        const filer = f.holder_name || f.filer_name || '?';
        const target = f.target_company_name || '?';
        const ratio = f.holding_ratio != null ? `${f.holding_ratio.toFixed(2)}%` : '';
        const change = f.ratio_change != null
            ? (f.ratio_change > 0 ? ` ▲${f.ratio_change.toFixed(2)}%` : ` ▼${Math.abs(f.ratio_change).toFixed(2)}%`)
            : '';
        const type = f.is_amendment ? '[訂正]' : f.doc_description?.includes('変更') ? '[変更]' : '[新規]';
        return `${type} ${filer} → ${target} ${ratio}${change}`;
    });

    document.getElementById('ticker-text').textContent = items.join('    |    ');
}

// ---------------------------------------------------------------------------
// Watchlist Actions
// ---------------------------------------------------------------------------

async function saveWatchItem() {
    const name = document.getElementById('watch-name').value.trim();
    const code = document.getElementById('watch-code').value.trim();

    if (!name) return;

    try {
        const resp = await fetch('/api/watchlist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company_name: name, sec_code: code || null }),
        });
        if (resp.ok) {
            document.getElementById('watch-name').value = '';
            document.getElementById('watch-code').value = '';
            document.getElementById('watchlist-form').classList.add('hidden');
            await loadWatchlist();
        }
    } catch (e) {
        console.error('Failed to save watchlist item:', e);
    }
}

async function deleteWatchItem(id) {
    try {
        await fetch(`/api/watchlist/${id}`, { method: 'DELETE' });
        await loadWatchlist();
    } catch (e) {
        console.error('Failed to delete watchlist item:', e);
    }
}

function checkWatchlistMatch(filing) {
    if (!isWatchlistMatch(filing)) return;

    // Highlight the card
    setTimeout(() => {
        const card = document.querySelector(`[data-doc-id="${filing.doc_id}"]`);
        if (card) {
            card.classList.add('watchlist-match');
            card.addEventListener('animationend', () => {
                card.classList.remove('watchlist-match');
            }, { once: true });
        }
    }, 100);

    // Extra notification for watchlist match
    if (state.notificationsEnabled) {
        const matched = state.watchlist.find(w =>
            (w.sec_code && (filing.sec_code === w.sec_code || filing.target_sec_code === w.sec_code)) ||
            (w.company_name && (
                (filing.target_company_name || '').includes(w.company_name) ||
                (filing.filer_name || '').includes(w.company_name)
            ))
        );
        if (matched) {
            try {
                new Notification(`WATCHLIST: ${matched.company_name}`, {
                    body: `${filing.filer_name || ''} - ${filing.doc_description || ''}`,
                    tag: `watchlist-${filing.doc_id}`,
                    requireInteraction: true,
                });
            } catch (e) {
                console.warn('Watchlist notification failed:', e);
            }
        }
    }
    if (state.soundEnabled) {
        // Play a different (higher) alert for watchlist
        playAlertSound(880);
    }
}

// ---------------------------------------------------------------------------
// Confirm Dialog
// ---------------------------------------------------------------------------

function showDeleteConfirm(companyName, itemId) {
    const dialog = document.getElementById('confirm-dialog');
    const message = document.getElementById('dialog-message');
    message.textContent = `「${companyName}」をウォッチリストから削除しますか？`;
    dialog.classList.remove('hidden');

    const confirmBtn = document.getElementById('dialog-confirm');
    const cancelBtn = document.getElementById('dialog-cancel');

    const cleanup = () => {
        dialog.classList.add('hidden');
        confirmBtn.replaceWith(confirmBtn.cloneNode(true));
        cancelBtn.replaceWith(cancelBtn.cloneNode(true));
    };

    confirmBtn.addEventListener('click', () => {
        cleanup();
        deleteWatchItem(itemId);
    }, { once: true });

    cancelBtn.addEventListener('click', () => {
        cleanup();
    }, { once: true });

    // Also close on overlay click
    dialog.querySelector('.modal-overlay').addEventListener('click', () => {
        cleanup();
    }, { once: true });

    confirmBtn.focus();
}

function closeConfirmDialog() {
    const dialog = document.getElementById('confirm-dialog');
    if (!dialog.classList.contains('hidden')) {
        dialog.classList.add('hidden');
    }
}

// ---------------------------------------------------------------------------
// Stock Chart Renderer (Canvas-based Candlestick + SMA + Tooltip)
// ---------------------------------------------------------------------------

/**
 * Compute simple moving average for an array of close prices.
 */
function computeSMA(prices, period) {
    const result = [];
    for (let i = 0; i < prices.length; i++) {
        if (i < period - 1) {
            result.push(null);
        } else {
            let sum = 0;
            for (let j = i - period + 1; j <= i; j++) {
                sum += prices[j].close;
            }
            result.push(sum / period);
        }
    }
    return result;
}

function renderStockChart(canvas, prices, options = {}) {
    if (!prices || prices.length === 0) return;

    // Filter out entries with missing OHLC values
    prices = prices.filter(p =>
        p.open != null && p.high != null && p.low != null && p.close != null
    );
    if (prices.length === 0) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width;
    const H = options.height || 250;

    // Container not laid out yet -- defer with retry (up to 3 attempts)
    if (W <= 0) {
        const attempt = (options._retryCount || 0) + 1;
        if (attempt <= 3) {
            requestAnimationFrame(() => {
                renderStockChart(canvas, prices, { ...options, _retryCount: attempt });
            });
        }
        return;
    }

    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.scale(dpr, dpr);

    // Layout
    const padding = { top: 10, right: 60, bottom: 30, left: 10 };
    const chartW = W - padding.left - padding.right;
    const volumeH = H * 0.18;
    const priceH = H - padding.top - padding.bottom - volumeH - 5;

    // Find price range
    let minPrice = Infinity, maxPrice = -Infinity, maxVol = 0;
    for (const p of prices) {
        if (p.low < minPrice) minPrice = p.low;
        if (p.high > maxPrice) maxPrice = p.high;
        const vol = p.volume || 0;
        if (vol > maxVol) maxVol = vol;
    }
    const priceRange = maxPrice - minPrice || 1;
    const pricePad = priceRange * 0.05;
    minPrice -= pricePad;
    maxPrice += pricePad;

    const candleW = Math.max(1, (chartW / prices.length) * 0.7);
    const gap = chartW / prices.length;

    // Clear
    ctx.clearRect(0, 0, W, H);

    // Grid lines (horizontal)
    ctx.strokeStyle = 'rgba(30, 30, 52, 0.6)';
    ctx.lineWidth = 0.5;
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i++) {
        const y = padding.top + (priceH * i / gridLines);
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(W - padding.right, y);
        ctx.stroke();

        // Price label
        const price = maxPrice - (maxPrice - minPrice) * (i / gridLines);
        ctx.fillStyle = '#707088';
        ctx.font = '10px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(price.toFixed(0), W - padding.right + 5, y + 3);
    }

    // Draw candles
    const upColor = '#00e676';
    const downColor = '#ff1744';

    for (let i = 0; i < prices.length; i++) {
        const p = prices[i];
        const x = padding.left + i * gap + gap / 2;
        const isUp = p.close >= p.open;
        const color = isUp ? upColor : downColor;

        // Wick (high-low line)
        const highY = padding.top + (1 - (p.high - minPrice) / (maxPrice - minPrice)) * priceH;
        const lowY = padding.top + (1 - (p.low - minPrice) / (maxPrice - minPrice)) * priceH;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, highY);
        ctx.lineTo(x, lowY);
        ctx.stroke();

        // Body
        const openY = padding.top + (1 - (p.open - minPrice) / (maxPrice - minPrice)) * priceH;
        const closeY = padding.top + (1 - (p.close - minPrice) / (maxPrice - minPrice)) * priceH;
        const bodyTop = Math.min(openY, closeY);
        const bodyH = Math.max(Math.abs(closeY - openY), 1);

        ctx.fillStyle = isUp ? 'rgba(0,230,118,0.85)' : 'rgba(255,23,68,0.85)';
        ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);

        // Volume bar
        const vol = p.volume || 0;
        if (maxVol > 0 && vol > 0) {
            const volH = (vol / maxVol) * volumeH;
            const volY = H - padding.bottom - volH;
            ctx.fillStyle = isUp ? 'rgba(0,230,118,0.18)' : 'rgba(255,23,68,0.18)';
            ctx.fillRect(x - candleW / 2, volY, candleW, volH);
        }
    }

    // --- Moving Averages ---
    if (options.showSMA !== false && prices.length >= 5) {
        const sma25 = computeSMA(prices, Math.min(25, prices.length));
        const sma50 = computeSMA(prices, Math.min(50, prices.length));

        const drawSMA = (smaData, color) => {
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([]);
            ctx.beginPath();
            let started = false;
            for (let i = 0; i < smaData.length; i++) {
                if (smaData[i] == null) continue;
                const x = padding.left + i * gap + gap / 2;
                const y = padding.top + (1 - (smaData[i] - minPrice) / (maxPrice - minPrice)) * priceH;
                if (!started) { ctx.moveTo(x, y); started = true; }
                else { ctx.lineTo(x, y); }
            }
            ctx.stroke();
        };

        drawSMA(sma25, 'rgba(255, 171, 0, 0.7)');  // Amber for SMA25
        if (prices.length >= 50) {
            drawSMA(sma50, 'rgba(179, 136, 255, 0.7)');  // Purple for SMA50
        }
    }

    // Date labels (show every ~3 months)
    ctx.fillStyle = '#707088';
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    let lastMonth = '';
    for (let i = 0; i < prices.length; i++) {
        const month = prices[i].date.slice(0, 7); // YYYY-MM
        if (month !== lastMonth && i % Math.max(1, Math.floor(prices.length / 8)) === 0) {
            lastMonth = month;
            const x = padding.left + i * gap + gap / 2;
            ctx.fillText(month, x, H - padding.bottom + 15);
        }
    }

    // --- SMA Legend ---
    if (options.showSMA !== false && prices.length >= 5) {
        const legendY = padding.top + 4;
        ctx.font = '9px monospace';
        ctx.textAlign = 'left';
        ctx.fillStyle = 'rgba(255, 171, 0, 0.8)';
        ctx.fillText('SMA25', padding.left + 4, legendY + 8);
        if (prices.length >= 50) {
            ctx.fillStyle = 'rgba(179, 136, 255, 0.8)';
            ctx.fillText('SMA50', padding.left + 50, legendY + 8);
        }
    }

    // --- Tooltip support (store chart metadata on canvas for mousemove) ---
    canvas._chartMeta = {
        prices, padding, gap, minPrice, maxPrice, priceH, volumeH, W, H,
        chartW, candleW,
    };
}

/**
 * Attach hover tooltip to a chart canvas (call once after first render).
 */
function attachChartTooltip(canvas, tooltipEl) {
    if (canvas._tooltipAttached) return;
    canvas._tooltipAttached = true;

    canvas.addEventListener('mousemove', (e) => {
        const meta = canvas._chartMeta;
        if (!meta) return;

        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        const { prices, padding, gap, minPrice, maxPrice, priceH } = meta;
        const idx = Math.floor((mx - padding.left) / gap);

        if (idx < 0 || idx >= prices.length) {
            tooltipEl.style.display = 'none';
            return;
        }

        const p = prices[idx];
        const isUp = p.close >= p.open;
        const changeFromOpen = ((p.close - p.open) / p.open * 100).toFixed(2);
        const sign = p.close >= p.open ? '+' : '';

        tooltipEl.innerHTML = `
            <div class="chart-tooltip-date">${p.date}</div>
            <div class="chart-tooltip-row">始 <span>${p.open.toLocaleString()}</span></div>
            <div class="chart-tooltip-row">高 <span style="color:#00e676">${p.high.toLocaleString()}</span></div>
            <div class="chart-tooltip-row">安 <span style="color:#ff1744">${p.low.toLocaleString()}</span></div>
            <div class="chart-tooltip-row">終 <span style="color:${isUp ? '#00e676' : '#ff1744'}">${p.close.toLocaleString()} (${sign}${changeFromOpen}%)</span></div>
            ${p.volume ? `<div class="chart-tooltip-row">出来高 <span>${p.volume.toLocaleString()}</span></div>` : ''}
        `;

        tooltipEl.style.display = 'block';

        // Position tooltip near cursor but keep within bounds
        const tooltipW = tooltipEl.offsetWidth;
        const tooltipH = tooltipEl.offsetHeight;
        const containerRect = canvas.parentElement.getBoundingClientRect();
        let left = e.clientX - containerRect.left + 12;
        let top = e.clientY - containerRect.top - tooltipH / 2;

        if (left + tooltipW > containerRect.width - 5) {
            left = e.clientX - containerRect.left - tooltipW - 12;
        }
        top = Math.max(4, Math.min(top, containerRect.height - tooltipH - 4));

        tooltipEl.style.left = left + 'px';
        tooltipEl.style.top = top + 'px';
    });

    canvas.addEventListener('mouseleave', () => {
        tooltipEl.style.display = 'none';
    });
}

// ---------------------------------------------------------------------------
// Mini Sparkline Chart (for watchlist)
// ---------------------------------------------------------------------------

function renderSparkline(canvas, prices, width, height) {
    if (!prices || prices.length < 2) return;

    const closes = prices.filter(p => p.close != null).map(p => p.close);
    if (closes.length < 2) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.scale(dpr, dpr);

    const minV = Math.min(...closes);
    const maxV = Math.max(...closes);
    const range = maxV - minV || 1;
    const pad = 2;
    const chartH = height - pad * 2;
    const chartW = width - pad * 2;
    const step = chartW / (closes.length - 1);

    // Determine color from overall trend
    const isUp = closes[closes.length - 1] >= closes[0];
    const lineColor = isUp ? '#00e676' : '#ff1744';
    const fillColor = isUp ? 'rgba(0, 230, 118, 0.08)' : 'rgba(255, 23, 68, 0.08)';

    // Draw filled area
    ctx.beginPath();
    ctx.moveTo(pad, height - pad);
    for (let i = 0; i < closes.length; i++) {
        const x = pad + i * step;
        const y = pad + (1 - (closes[i] - minV) / range) * chartH;
        ctx.lineTo(x, y);
    }
    ctx.lineTo(pad + (closes.length - 1) * step, height - pad);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();

    // Draw line
    ctx.beginPath();
    for (let i = 0; i < closes.length; i++) {
        const x = pad + i * step;
        const y = pad + (1 - (closes[i] - minV) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

// ---------------------------------------------------------------------------
// Dedicated Stock Chart View
// ---------------------------------------------------------------------------

let currentStockView = null; // tracks which stock is displayed in stock view

function initStockView() {
    const searchInput = document.getElementById('stock-search-input');
    const searchBtn = document.getElementById('stock-search-btn');
    if (!searchInput || !searchBtn) return;

    const doSearch = () => {
        const code = searchInput.value.trim();
        if (code.length >= 4) {
            loadStockView(code);
        }
    };

    searchBtn.addEventListener('click', doSearch);
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSearch();
    });

    // Header stock view toggle button
    const stockViewBtn = document.getElementById('btn-stock-view');
    if (stockViewBtn) {
        stockViewBtn.addEventListener('click', () => {
            const stockView = document.getElementById('stock-view');
            if (stockView && !stockView.classList.contains('hidden')) {
                hideStockView();
            } else {
                showStockView();
            }
        });
    }

    // Back button in stock view
    const backBtn = document.getElementById('stock-view-back');
    if (backBtn) {
        backBtn.addEventListener('click', () => {
            hideStockView();
            // Reset mobile nav active state
            document.querySelectorAll('.mobile-bottom-nav .nav-item').forEach(n => {
                n.classList.toggle('active', n.dataset.panel === 'feed');
            });
        });
    }
}

function updateStockQuickList() {
    const container = document.getElementById('stock-quick-list');
    if (!container) return;

    if (state.watchlist.length === 0) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = state.watchlist
        .filter(w => w.sec_code)
        .map(w => `<button class="stock-quick-btn" data-code="${escapeHtml(w.sec_code)}" title="${escapeHtml(w.company_name)}">${escapeHtml(w.sec_code)} ${escapeHtml(w.company_name)}</button>`)
        .join('');

    container.querySelectorAll('.stock-quick-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const code = btn.dataset.code;
            document.getElementById('stock-search-input').value = code;
            loadStockView(code);
        });
    });
}

async function loadStockView(secCode) {
    const body = document.getElementById('stock-view-body');
    if (!body) return;

    const code = secCode.length === 5 ? secCode.slice(0, 4) : secCode;
    currentStockView = code;

    body.innerHTML = '<div class="stock-loading">株価データ読み込み中...</div>';

    const stockData = await fetchStockData(code);

    // Check if user switched to another stock while loading
    if (currentStockView !== code) return;

    if (!stockData) {
        body.innerHTML = '<div class="stock-no-data">株価データを取得できませんでした</div>';
        return;
    }

    // Build the full stock view
    let html = '';

    // Company header
    html += '<div class="stock-view-company-header">';
    html += `<div class="stock-view-ticker">${escapeHtml(stockData.ticker || code)}</div>`;
    if (stockData.name) {
        html += `<div class="stock-view-name">${escapeHtml(stockData.name)}</div>`;
    }
    html += '</div>';

    // Price info bar
    html += '<div class="stock-view-metrics">';
    if (stockData.current_price != null) {
        const lastPrice = stockData.weekly_prices && stockData.weekly_prices.length >= 2
            ? stockData.weekly_prices[stockData.weekly_prices.length - 2].close : null;
        let changeHtml = '';
        if (lastPrice != null && lastPrice > 0) {
            const diff = stockData.current_price - lastPrice;
            const pct = (diff / lastPrice * 100).toFixed(2);
            const sign = diff >= 0 ? '+' : '';
            const cls = diff >= 0 ? 'positive' : 'negative';
            changeHtml = `<span class="stock-view-change ${cls}">${sign}${diff.toFixed(1)} (${sign}${pct}%)</span>`;
        }
        html += `<div class="stock-view-price-block">
            <span class="stock-view-current-price">&yen;${Math.round(stockData.current_price).toLocaleString()}</span>
            ${changeHtml}
        </div>`;
    }

    const metricItems = [];
    if (stockData.market_cap_display) metricItems.push(`<div class="stock-view-metric"><span class="metric-label">時価総額</span><span class="metric-value">${stockData.market_cap_display}</span></div>`);
    if (stockData.pbr != null) metricItems.push(`<div class="stock-view-metric"><span class="metric-label">PBR</span><span class="metric-value">${Number(stockData.pbr).toFixed(2)}倍</span></div>`);
    if (stockData.weekly_prices && stockData.weekly_prices.length > 0) {
        const highs = stockData.weekly_prices.map(p => p.high).filter(v => v != null);
        const lows = stockData.weekly_prices.map(p => p.low).filter(v => v != null);
        if (highs.length > 0) metricItems.push(`<div class="stock-view-metric"><span class="metric-label">52W高値</span><span class="metric-value text-green">&yen;${Math.round(Math.max(...highs)).toLocaleString()}</span></div>`);
        if (lows.length > 0) metricItems.push(`<div class="stock-view-metric"><span class="metric-label">52W安値</span><span class="metric-value text-red">&yen;${Math.round(Math.min(...lows)).toLocaleString()}</span></div>`);
    }
    html += `<div class="stock-view-metric-grid">${metricItems.join('')}</div>`;
    html += '</div>';

    // Chart container (large)
    html += '<div class="stock-view-chart-wrapper"><div class="stock-chart-container stock-chart-large"><canvas id="stock-view-canvas"></canvas><div id="stock-view-tooltip" class="chart-tooltip"></div></div></div>';

    body.innerHTML = html;

    // Render chart
    const chartCanvas = document.getElementById('stock-view-canvas');
    if (chartCanvas && stockData.weekly_prices && stockData.weekly_prices.length > 0) {
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                renderStockChart(chartCanvas, stockData.weekly_prices, { height: 400, showSMA: true });
                const tooltip = document.getElementById('stock-view-tooltip');
                if (tooltip) attachChartTooltip(chartCanvas, tooltip);
            });
        });
    }
}

function showStockView() {
    const stockView = document.getElementById('stock-view');
    const mainLayout = document.getElementById('main-layout');
    if (stockView && mainLayout) {
        mainLayout.classList.add('hidden');
        stockView.classList.remove('hidden');
        updateStockQuickList();
        const btn = document.getElementById('btn-stock-view');
        if (btn) btn.classList.add('active');
    }
}

function hideStockView() {
    const stockView = document.getElementById('stock-view');
    const mainLayout = document.getElementById('main-layout');
    if (stockView && mainLayout) {
        stockView.classList.add('hidden');
        mainLayout.classList.remove('hidden');
        const btn = document.getElementById('btn-stock-view');
        if (btn) btn.classList.remove('active');
    }
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

function openModal(filing) {
    currentModalDocId = filing.doc_id;
    const body = document.getElementById('modal-body');

    const rows = [
        ['書類ID', filing.doc_id],
        ['書類種別', filing.doc_description || '-'],
        ['提出日時', filing.submit_date_time || '-'],
        ['提出者', filing.filer_name || '-'],
        ['EDINET コード', filing.edinet_code || '-'],
        ['対象会社', filing.target_company_name || extractTargetFromDescription(filing.doc_description) || '-'],
        ['対象証券コード', filing.target_sec_code || filing.sec_code || '-'],
    ];

    // Visual ratio gauge section
    let ratioGaugeHtml = '';
    if (filing.holding_ratio != null) {
        const ratioClass = filing.ratio_change > 0 ? 'positive' : filing.ratio_change < 0 ? 'negative' : 'neutral';
        const currWidth = Math.min(filing.holding_ratio, 100);
        const prevWidth = filing.previous_holding_ratio != null ? Math.min(filing.previous_holding_ratio, 100) : 0;

        let changeText = '';
        if (filing.ratio_change != null && filing.ratio_change !== 0) {
            const arrow = filing.ratio_change > 0 ? '▲' : '▼';
            const sign = filing.ratio_change > 0 ? '+' : '';
            changeText = `<span class="modal-ratio-change ${ratioClass}">${arrow} ${sign}${filing.ratio_change.toFixed(2)}%</span>`;
        }

        ratioGaugeHtml = `
            <div class="modal-ratio-section">
                <div class="modal-ratio-header">
                    <span class="modal-ratio-value ${ratioClass}">${filing.holding_ratio.toFixed(2)}%</span>
                    ${changeText}
                </div>
                <div class="modal-ratio-gauge">
                    ${prevWidth > 0 ? `<div class="gauge-bar gauge-prev" style="width: ${prevWidth}%"></div>` : ''}
                    <div class="gauge-bar gauge-curr ${ratioClass}" style="width: ${currWidth}%"></div>
                    <div class="gauge-labels">
                        ${filing.previous_holding_ratio != null ?
                            `<span class="gauge-label" style="left: ${prevWidth}%">前回 ${filing.previous_holding_ratio.toFixed(2)}%</span>` : ''}
                    </div>
                </div>
                <div class="modal-ratio-footer">
                    <span>0%</span>
                    <span>50%</span>
                    <span>100%</span>
                </div>
            </div>
        `;
    }

    if (filing.shares_held != null) {
        rows.push(['保有株数', filing.shares_held.toLocaleString()]);
    }
    if (filing.purpose_of_holding) {
        rows.push(['保有目的', filing.purpose_of_holding]);
    }

    // Links
    const links = [];
    if (filing.pdf_url) {
        links.push(`<a href="${filing.pdf_url}" target="_blank" rel="noopener">PDF ダウンロード</a>`);
    }
    if (filing.edinet_url) {
        links.push(`<a href="${filing.edinet_url}" target="_blank" rel="noopener">EDINET で閲覧</a>`);
    }
    if (links.length > 0) {
        rows.push(['リンク', { html: `<span class="detail-value">${links.join(' | ')}</span>` }]);
    }

    body.innerHTML = ratioGaugeHtml + rows.map(([label, value]) => {
        const valHtml = typeof value === 'object' && value.html
            ? value.html
            : `<span class="detail-value">${escapeHtml(String(value))}</span>`;
        return `<div class="detail-row">
            <span class="detail-label">${escapeHtml(label)}</span>
            ${valHtml}
        </div>`;
    }).join('');

    // Add stock data section
    const secCode = filing.target_sec_code || filing.sec_code;
    if (secCode) {
        const stockSection = document.createElement('div');
        stockSection.className = 'modal-stock-section';
        stockSection.innerHTML = '<div class="stock-loading">株価データ読み込み中...</div>';
        body.appendChild(stockSection);

        // Snapshot doc_id to detect stale callbacks when user switches modals
        const docIdSnapshot = filing.doc_id;

        // Fetch async
        fetchStockData(secCode).then(stockData => {
            // Abort if user already opened a different modal
            if (currentModalDocId !== docIdSnapshot) return;

            if (!stockData) {
                stockSection.innerHTML = '<div class="stock-no-data">株価データを取得できませんでした</div>';
                return;
            }

            let infoHtml = '<div class="stock-info-bar">';
            if (stockData.current_price != null) {
                infoHtml += `<span class="stock-price">\u00a5${Math.round(stockData.current_price).toLocaleString()}</span>`;
            }
            if (stockData.market_cap_display) {
                infoHtml += `<span class="stock-metric">時価: <strong>${stockData.market_cap_display}</strong></span>`;
            }
            if (stockData.pbr != null) {
                infoHtml += `<span class="stock-metric">PBR: <strong>${Number(stockData.pbr).toFixed(2)}倍</strong></span>`;
            }
            infoHtml += '</div>';

            stockSection.innerHTML = infoHtml + '<div class="stock-chart-container"><canvas id="stock-chart-canvas"></canvas><div id="modal-chart-tooltip" class="chart-tooltip"></div></div>';

            const chartCanvas = document.getElementById('stock-chart-canvas');
            if (chartCanvas && stockData.weekly_prices && stockData.weekly_prices.length > 0) {
                // Defer rendering: double-rAF ensures the browser has fully
                // reflowed the DOM after innerHTML and completed any CSS
                // animations (e.g. modal scale-in) before we measure width.
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        renderStockChart(chartCanvas, stockData.weekly_prices, { showSMA: true });
                        const tooltip = document.getElementById('modal-chart-tooltip');
                        if (tooltip) attachChartTooltip(chartCanvas, tooltip);
                    });
                });
            }
        });
    }

    // Store current filing index for keyboard navigation
    const currentIndex = state.filings.findIndex(f => f.doc_id === filing.doc_id);
    document.getElementById('detail-modal').dataset.filingIndex = currentIndex;
    document.getElementById('detail-modal').classList.remove('hidden');
}

function closeModal() {
    currentModalDocId = null;
    document.getElementById('detail-modal').classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

function playAlertSound(freq = 660) {
    try {
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }

        const oscillator = audioCtx.createOscillator();
        const gainNode = audioCtx.createGain();

        oscillator.connect(gainNode);
        gainNode.connect(audioCtx.destination);

        oscillator.type = 'sine';
        oscillator.frequency.setValueAtTime(freq, audioCtx.currentTime);

        gainNode.gain.setValueAtTime(0.15, audioCtx.currentTime);
        gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.5);

        oscillator.start(audioCtx.currentTime);
        oscillator.stop(audioCtx.currentTime + 0.5);

        // Second beep for emphasis
        setTimeout(() => {
            const osc2 = audioCtx.createOscillator();
            const gain2 = audioCtx.createGain();
            osc2.connect(gain2);
            gain2.connect(audioCtx.destination);
            osc2.type = 'sine';
            osc2.frequency.setValueAtTime(freq * 1.25, audioCtx.currentTime);
            gain2.gain.setValueAtTime(0.1, audioCtx.currentTime);
            gain2.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
            osc2.start(audioCtx.currentTime);
            osc2.stop(audioCtx.currentTime + 0.3);
        }, 200);
    } catch (e) {
        console.warn('Audio alert failed:', e);
    }
}

// ---------------------------------------------------------------------------
// Desktop Notifications
// ---------------------------------------------------------------------------

function sendDesktopNotification(filing) {
    if (!state.notificationsEnabled) return;

    const filer = filing.holder_name || filing.filer_name || '不明';
    const target = filing.target_company_name || '';
    const ratio = filing.holding_ratio != null ? `${filing.holding_ratio.toFixed(2)}%` : '';
    const type = filing.is_amendment ? '訂正' : '大量保有報告';

    const title = `[${type}] ${filer}`;
    const body = target
        ? `${target} ${ratio}`
        : filing.doc_description || '';

    try {
        const notification = new Notification(title, {
            body: body,
            tag: filing.doc_id,
            requireInteraction: false,
        });

        notification.onclick = () => {
            window.focus();
            openModal(filing);
            notification.close();
        };
    } catch (e) {
        console.warn('Desktop notification failed:', e);
    }
}

// ---------------------------------------------------------------------------
// Mobile Overlay Management
// ---------------------------------------------------------------------------

function openMobileOverlay(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.remove('hidden');
    syncMobilePanel(panelId);
}

function closeMobileOverlay(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.add('hidden');
    // Reset nav active state to feed
    document.querySelectorAll('.mobile-bottom-nav .nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.panel === 'feed');
    });
}

function closeAllMobileOverlays() {
    document.querySelectorAll('.sidebar-overlay').forEach(panel => {
        panel.classList.add('hidden');
    });
}

// ---------------------------------------------------------------------------
// Mobile Panel Content Sync
// ---------------------------------------------------------------------------

function syncMobilePanel(panelId) {
    if (panelId === 'mobile-stats-panel') {
        const mobileStatsBody = document.getElementById('mobile-stats-body');
        if (!mobileStatsBody) return;

        let html = '';

        // Clone stats grid
        const statsGrid = document.querySelector('#stats-panel .stats-grid');
        if (statsGrid) {
            html += statsGrid.outerHTML;
        }

        // Clone top filers
        const topFilers = document.getElementById('top-filers-list');
        if (topFilers) {
            html += '<div class="top-filers-section"><h3 class="section-title">TOP FILERS</h3>';
            html += topFilers.outerHTML;
            html += '</div>';
        }

        mobileStatsBody.innerHTML = html;

    } else if (panelId === 'mobile-watchlist-panel') {
        const mobileWatchBody = document.getElementById('mobile-watchlist-body');
        if (!mobileWatchBody) return;

        let html = '';

        // Clone watchlist items
        const watchlistItems = document.getElementById('watchlist-items');
        if (watchlistItems) {
            html += watchlistItems.innerHTML;
        }

        // Clone watchlist form
        const watchlistForm = document.getElementById('watchlist-form');
        if (watchlistForm) {
            html += `<div class="mobile-watchlist-form">
                <div class="form-group">
                    <input type="text" id="mobile-watch-name" placeholder="企業名" class="form-input" />
                </div>
                <div class="form-group">
                    <input type="text" id="mobile-watch-code" placeholder="証券コード" class="form-input" />
                </div>
                <div class="form-actions">
                    <button id="mobile-btn-save-watch" class="btn btn-primary">追加</button>
                    <button id="mobile-btn-cancel-watch" class="btn btn-secondary">キャンセル</button>
                </div>
            </div>`;
        }

        mobileWatchBody.innerHTML = html;

        // Re-attach delete button listeners
        mobileWatchBody.querySelectorAll('.watch-delete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = e.target.dataset.id;
                const item = state.watchlist.find(w => String(w.id) === String(id));
                if (item) {
                    showDeleteConfirm(item.company_name, id);
                }
            });
        });

        // Re-attach form button listeners
        const mobileSaveBtn = document.getElementById('mobile-btn-save-watch');
        if (mobileSaveBtn) {
            mobileSaveBtn.addEventListener('click', async () => {
                const name = document.getElementById('mobile-watch-name').value.trim();
                const code = document.getElementById('mobile-watch-code').value.trim();
                if (!name) return;
                try {
                    const resp = await fetch('/api/watchlist', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ company_name: name, sec_code: code || null }),
                    });
                    if (resp.ok) {
                        await loadWatchlist();
                        syncMobilePanel('mobile-watchlist-panel');
                    }
                } catch (e) {
                    console.error('Failed to save watchlist item:', e);
                }
            });
        }

        const mobileCancelBtn = document.getElementById('mobile-btn-cancel-watch');
        if (mobileCancelBtn) {
            mobileCancelBtn.addEventListener('click', () => {
                const nameInput = document.getElementById('mobile-watch-name');
                const codeInput = document.getElementById('mobile-watch-code');
                if (nameInput) nameInput.value = '';
                if (codeInput) codeInput.value = '';
            });
        }

    } else if (panelId === 'mobile-settings-panel') {
        // Sync button states with desktop buttons
        const desktopSoundBtn = document.getElementById('btn-sound');
        const mobileSoundBtn = document.getElementById('mobile-btn-sound');
        if (desktopSoundBtn && mobileSoundBtn) {
            mobileSoundBtn.classList.toggle('active', state.soundEnabled);
            mobileSoundBtn.textContent = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF';
        }

        const desktopNotifyBtn = document.getElementById('btn-notify');
        const mobileNotifyBtn = document.getElementById('mobile-btn-notify');
        if (desktopNotifyBtn && mobileNotifyBtn) {
            mobileNotifyBtn.classList.toggle('active', state.notificationsEnabled);
            mobileNotifyBtn.textContent = state.notificationsEnabled ? '通知 ON' : '通知 OFF';
        }
    }
}

// ---------------------------------------------------------------------------
// Overlay Swipe-to-Close Gesture
// ---------------------------------------------------------------------------

function initOverlaySwipe(overlayEl) {
    const sheet = overlayEl.querySelector('.overlay-sheet');
    if (!sheet) return;

    let startY = 0;
    let currentY = 0;
    let isDragging = false;

    sheet.addEventListener('touchstart', (e) => {
        // Only start drag from the handle area (top 40px)
        const rect = sheet.getBoundingClientRect();
        if (e.touches[0].clientY - rect.top > 40) return;
        startY = e.touches[0].clientY;
        isDragging = true;
        sheet.style.transition = 'none';
    }, { passive: true });

    sheet.addEventListener('touchmove', (e) => {
        if (!isDragging) return;
        currentY = e.touches[0].clientY;
        const diff = currentY - startY;
        if (diff > 0) {
            sheet.style.transform = `translateY(${diff}px)`;
        }
    }, { passive: true });

    sheet.addEventListener('touchend', () => {
        if (!isDragging) return;
        isDragging = false;
        sheet.style.transition = '';
        const diff = currentY - startY;
        if (diff > 100) {
            // Close the overlay
            closeMobileOverlay(overlayEl.id);
        }
        sheet.style.transform = '';
    });
}

// ---------------------------------------------------------------------------
// Mobile Bottom Nav
// ---------------------------------------------------------------------------

function initMobileNav() {
    const mobileNav = document.getElementById('mobile-nav');
    if (!mobileNav) return;

    const navItems = mobileNav.querySelectorAll('.nav-item');

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            // Toggle active class: remove from siblings, add to clicked
            navItems.forEach(sibling => sibling.classList.remove('active'));
            item.classList.add('active');

            const panel = item.dataset.panel;

            if (panel === 'feed') {
                // Hide overlays, show feed (default view)
                closeAllMobileOverlays();
                hideStockView();
            } else if (panel === 'stats') {
                closeAllMobileOverlays();
                hideStockView();
                openMobileOverlay('mobile-stats-panel');
            } else if (panel === 'watchlist') {
                closeAllMobileOverlays();
                hideStockView();
                openMobileOverlay('mobile-watchlist-panel');
            } else if (panel === 'stock') {
                closeAllMobileOverlays();
                showStockView();
            } else if (panel === 'settings') {
                closeAllMobileOverlays();
                hideStockView();
                openMobileOverlay('mobile-settings-panel');
            }
        });
    });

    // Overlay backdrop close handlers
    document.querySelectorAll('.overlay-backdrop').forEach(backdrop => {
        backdrop.addEventListener('click', () => {
            const overlay = backdrop.closest('.sidebar-overlay');
            if (overlay) closeMobileOverlay(overlay.id);
        });
    });

    // Overlay close button handlers
    document.querySelectorAll('.overlay-close').forEach(closeBtn => {
        closeBtn.addEventListener('click', () => {
            const overlay = closeBtn.closest('.sidebar-overlay');
            if (overlay) closeMobileOverlay(overlay.id);
        });
    });

    // Initialize swipe-to-close on all overlays
    document.querySelectorAll('.sidebar-overlay').forEach(overlayEl => {
        initOverlaySwipe(overlayEl);
    });

    // Mobile settings panel handlers
    const mobileSoundBtn = document.getElementById('mobile-btn-sound');
    if (mobileSoundBtn) {
        mobileSoundBtn.addEventListener('click', () => {
            state.soundEnabled = !state.soundEnabled;

            // Update mobile button
            mobileSoundBtn.classList.toggle('active', state.soundEnabled);
            mobileSoundBtn.textContent = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF';

            // Sync with desktop button
            const desktopBtn = document.getElementById('btn-sound');
            if (desktopBtn) {
                desktopBtn.classList.toggle('active', state.soundEnabled);
                desktopBtn.title = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF';
                desktopBtn.setAttribute('aria-pressed', state.soundEnabled);
                desktopBtn.setAttribute('aria-label',
                    state.soundEnabled ? 'サウンドアラート: 有効' : 'サウンドアラート: 無効');
            }

            savePreferences();
        });
    }

    const mobileNotifyBtn = document.getElementById('mobile-btn-notify');
    if (mobileNotifyBtn) {
        mobileNotifyBtn.addEventListener('click', async () => {
            if (!('Notification' in window)) {
                alert('このブラウザはデスクトップ通知に対応していません');
                return;
            }
            const perm = await Notification.requestPermission();
            state.notificationsEnabled = perm === 'granted';

            // Update mobile button
            mobileNotifyBtn.classList.toggle('active', state.notificationsEnabled);
            mobileNotifyBtn.textContent = state.notificationsEnabled ? '通知 ON' : '通知 OFF';

            // Sync with desktop button
            const desktopBtn = document.getElementById('btn-notify');
            if (desktopBtn) {
                desktopBtn.classList.toggle('active', state.notificationsEnabled);
                desktopBtn.title = state.notificationsEnabled ? '通知 ON' : '通知 OFF';
                desktopBtn.setAttribute('aria-pressed', state.notificationsEnabled);
                desktopBtn.setAttribute('aria-label',
                    state.notificationsEnabled ? 'デスクトップ通知: 有効' : 'デスクトップ通知: 無効');
            }

            savePreferences();
        });
    }

    const mobilePollBtn = document.getElementById('mobile-btn-poll');
    if (mobilePollBtn) {
        mobilePollBtn.addEventListener('click', async () => {
            mobilePollBtn.disabled = true;
            mobilePollBtn.style.opacity = '0.5';
            try {
                await fetch('/api/poll', { method: 'POST' });
            } catch (e) {
                console.error('Poll trigger failed:', e);
            }
            setTimeout(() => {
                mobilePollBtn.disabled = false;
                mobilePollBtn.style.opacity = '1';
            }, 3000);
        });
    }
}

// ---------------------------------------------------------------------------
// Pull-to-Refresh Gesture
// ---------------------------------------------------------------------------

function initPullToRefresh() {
    const feedList = document.getElementById('feed-list');
    if (!feedList) return;

    let startY = 0;
    let currentY = 0;
    let isDragging = false;
    let indicator = null;

    feedList.addEventListener('touchstart', (e) => {
        if (!isMobile()) return;
        // Only activate when scrolled to top
        if (feedList.scrollTop > 0) return;
        startY = e.touches[0].clientY;
        isDragging = true;
    }, { passive: true });

    feedList.addEventListener('touchmove', (e) => {
        if (!isDragging) return;
        currentY = e.touches[0].clientY;
        const diff = currentY - startY;

        if (diff > 0 && feedList.scrollTop === 0) {
            // Show pull indicator
            if (!indicator) {
                indicator = document.createElement('div');
                indicator.className = 'pull-to-refresh-indicator';
                indicator.style.cssText = 'text-align:center;padding:10px;color:#00e676;font-size:12px;font-family:monospace;';
                feedList.parentNode.insertBefore(indicator, feedList);
            }
            if (diff > 60) {
                indicator.textContent = 'Release to refresh';
            } else {
                indicator.textContent = 'Pull to refresh...';
            }
            indicator.style.opacity = Math.min(diff / 60, 1);
        }
    }, { passive: true });

    feedList.addEventListener('touchend', () => {
        if (!isDragging) return;
        isDragging = false;
        const diff = currentY - startY;

        if (diff > 60 && feedList.scrollTop === 0) {
            // Trigger refresh
            if (indicator) {
                indicator.textContent = 'Refreshing...';
            }
            loadInitialData().then(() => {
                if (indicator) {
                    indicator.textContent = 'Updated!';
                    setTimeout(() => {
                        if (indicator && indicator.parentNode) {
                            indicator.parentNode.removeChild(indicator);
                            indicator = null;
                        }
                    }, 800);
                }
            });
        } else {
            // Remove indicator without refresh
            if (indicator && indicator.parentNode) {
                indicator.parentNode.removeChild(indicator);
                indicator = null;
            }
        }

        startY = 0;
        currentY = 0;
    });
}

// ---------------------------------------------------------------------------
// Date Navigation
// ---------------------------------------------------------------------------

function initDateNav() {
    const picker = document.getElementById('date-picker');
    const prevBtn = document.getElementById('date-prev');
    const nextBtn = document.getElementById('date-next');
    const todayBtn = document.getElementById('date-today');
    const fetchBtn = document.getElementById('date-fetch');

    if (!picker) return;

    picker.value = state.selectedDate;
    picker.max = new Date().toISOString().slice(0, 10);

    picker.addEventListener('change', (e) => {
        state.selectedDate = e.target.value;
        loadFilings();
        loadStats();
    });

    prevBtn.addEventListener('click', () => navigateDate(-1));
    nextBtn.addEventListener('click', () => navigateDate(1));

    todayBtn.addEventListener('click', () => {
        state.selectedDate = new Date().toISOString().slice(0, 10);
        picker.value = state.selectedDate;
        loadFilings();
        loadStats();
    });

    fetchBtn.addEventListener('click', async () => {
        fetchBtn.disabled = true;
        const origText = fetchBtn.textContent;
        fetchBtn.textContent = 'FETCHING...';
        try {
            await fetch(`/api/poll?date=${state.selectedDate}`, { method: 'POST' });
            // Wait for the poll to process, then reload
            setTimeout(async () => {
                await loadFilings();
                await loadStats();
                fetchBtn.disabled = false;
                fetchBtn.textContent = origText;
            }, 5000);
        } catch (e) {
            console.error('Fetch failed:', e);
            fetchBtn.disabled = false;
            fetchBtn.textContent = origText;
        }
    });

    const exportBtn = document.getElementById('date-export');
    if (exportBtn) {
        exportBtn.addEventListener('click', () => exportFilingsCSV());
    }
}

function navigateDate(days) {
    const d = new Date(state.selectedDate + 'T00:00:00');
    d.setDate(d.getDate() + days);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    if (d > today) return;

    state.selectedDate = d.toISOString().slice(0, 10);
    document.getElementById('date-picker').value = state.selectedDate;
    loadFilings();
    loadStats();
}

function csvEscape(val) {
    const s = String(val);
    if (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r')) {
        return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
}

function exportFilingsCSV() {
    if (state.filings.length === 0) return;

    const headers = [
        '提出日時', '提出者', '対象会社', '証券コード',
        '保有割合(%)', '前回保有割合(%)', '変動(%)',
        '保有株数', '書類種別', '訂正', 'EDINET URL'
    ];

    const rows = state.filings.map(f => [
        f.submit_date_time || '',
        f.holder_name || f.filer_name || '',
        f.target_company_name || '',
        f.target_sec_code || f.sec_code || '',
        f.holding_ratio != null ? f.holding_ratio.toFixed(2) : '',
        f.previous_holding_ratio != null ? f.previous_holding_ratio.toFixed(2) : '',
        f.ratio_change != null ? f.ratio_change.toFixed(2) : '',
        f.shares_held != null ? String(f.shares_held) : '',
        f.doc_description || '',
        f.is_amendment ? 'Yes' : 'No',
        f.edinet_url || ''
    ]);

    const bom = '\uFEFF';
    const csvContent = bom + [
        headers.map(csvEscape).join(','),
        ...rows.map(r => r.map(csvEscape).join(','))
    ].join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `edinet_filings_${state.selectedDate}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
