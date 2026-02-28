/**
 * EDINET Large Shareholding Monitor - Frontend Application
 * Bloomberg terminal-style real-time dashboard
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a Date object as YYYY-MM-DD in **local** time (not UTC). */
function toLocalDateStr(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    filings: [],
    watchlist: [],
    tobs: [],       // tender offer (公開買付) filings
    stats: {},
    connected: false,
    soundEnabled: true,
    notificationsEnabled: false,
    searchQuery: '',
    filterMode: 'all', // all | new | change | amendment
    sortMode: 'time-desc',
    viewMode: 'cards', // cards | table
    selectedDate: toLocalDateStr(new Date()), // YYYY-MM-DD
};

let eventSource = null;
let audioCtx = null;
let pollCountdownInterval = null;
let clockInterval = null;
let lastPollTime = Date.now();
let currentModalDocId = null; // tracks which filing is shown in the modal
let filingsAbortController = null; // AbortController for date navigation race condition
let _filteredFilings = []; // filtered list for modal arrow navigation
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
// Shared UI Helpers (reduce code duplication)
// ---------------------------------------------------------------------------

/** Normalize 5-digit sec code to 4-digit */
function normalizeSecCode(code) {
    if (!code) return '';
    return code.length === 5 ? code.slice(0, 4) : code;
}

/** Get cached stock data for a sec code */
function getCachedStock(secCode) {
    const code = normalizeSecCode(secCode);
    if (!code) return null;
    const cached = stockCache[code];
    return cached && cached.data ? cached.data : null;
}

/** Determine CSS class for ratio change */
function ratioChangeClass(change) {
    return change > 0 ? 'positive' : change < 0 ? 'negative' : 'neutral';
}

/** Prepare common data fields shared across card/table/mobile renderers */
function prepareFilingData(f) {
    const time = f.submit_date_time
        ? f.submit_date_time.split(' ').pop() || f.submit_date_time
        : '-';
    const filerName = f.holder_name || f.filer_name || '(不明)';
    const targetName = f.target_company_name || extractTargetFromDescription(f.doc_description) || '(対象不明)';
    const secCode = f.target_sec_code || f.sec_code || '';
    const cls = ratioChangeClass(f.ratio_change);
    const sd = getCachedStock(secCode);
    const code = normalizeSecCode(secCode);
    const stockLoading = code && !stockCache[code];
    const isChange = f.doc_description && f.doc_description.includes('変更');
    return { time, filerName, targetName, secCode, cls, sd, code, stockLoading, isChange };
}

/** Build PDF + EDINET link HTML */
function buildDocLinks(f, linkClass) {
    let html = '';
    if (f.pdf_url) html += `<a href="${escapeHtml(f.pdf_url)}" target="_blank" rel="noopener" class="${linkClass}" onclick="event.stopPropagation()">PDF</a>`;
    if (f.edinet_url) html += `<a href="${escapeHtml(f.edinet_url)}" target="_blank" rel="noopener" class="${linkClass}" onclick="event.stopPropagation()">EDINET</a>`;
    return html;
}

/** Escape a string for safe use inside a JavaScript single-quoted string literal. */
function escapeJsString(s) {
    return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

/** Build clickable filer link HTML */
function filerLinkHtml(name, edinetCode) {
    const safe = escapeHtml(name || '(不明)');
    if (!edinetCode) return safe;
    return `<a href="#" class="filer-link" onclick="event.preventDefault();event.stopPropagation();openFilerProfile('${escapeJsString(escapeHtml(edinetCode))}')">${safe}</a>`;
}

/** Build clickable company link HTML */
function companyLinkHtml(name, secCode, showCode) {
    const safe = escapeHtml(name || '(対象不明)');
    const codeStr = showCode && secCode ? ` ${escapeHtml('[' + secCode + ']')}` : '';
    if (!secCode) return safe + codeStr;
    return `<a href="#" class="filer-link" onclick="event.preventDefault();event.stopPropagation();openCompanyProfile('${escapeJsString(escapeHtml(secCode))}')">${safe}${codeStr}</a>`;
}

/** Build badge HTML for a filing */
function buildBadges(f, cssPrefix) {
    // cssPrefix: 'card-badge badge-' for desktop cards, 'tbl-badge badge-' for table, 'm-badge m-badge-' for mobile
    const isChange = f.doc_description && f.doc_description.includes('変更');
    const badges = [];
    if (f.is_amendment) badges.push([`${cssPrefix}amendment`, '訂正']);
    else if (isChange) badges.push([`${cssPrefix}change`, '変更']);
    else badges.push([`${cssPrefix}new`, '新規']);
    if (f.is_special_exemption) badges.push([`${cssPrefix}special`, '特例']);
    if (f.english_doc_flag) badges.push([`${cssPrefix}english`, 'EN']);
    if (f.withdrawal_status === '1') badges.push([`${cssPrefix}withdrawn`, '取下']);
    return badges.map(([cls, txt]) => `<span class="${cls}">${txt}</span>`).join('');
}

/** Build change display HTML */
function changeDisplayHtml(change, cls) {
    if (change == null || change === 0) return '';
    const arrow = change > 0 ? '▲' : '▼';
    const sign = change > 0 ? '+' : '';
    return `${arrow}${sign}${change.toFixed(2)}%`;
}

/** Update a toggle button's visual state */
function updateToggleBtn(btn, active, onTitle, offTitle, onAria, offAria) {
    if (!btn) return;
    btn.classList.toggle('active', active);
    btn.title = active ? onTitle : offTitle;
    btn.setAttribute('aria-pressed', active);
    btn.setAttribute('aria-label', active ? onAria : offAria);
}

/** Toggle sound on/off — shared by desktop and mobile buttons */
function toggleSound() {
    state.soundEnabled = !state.soundEnabled;
    updateToggleBtn(document.getElementById('btn-sound'), state.soundEnabled,
        'サウンド ON', 'サウンド OFF', 'サウンドアラート: 有効', 'サウンドアラート: 無効');
    const msb = document.getElementById('mobile-btn-sound');
    if (msb) { msb.classList.toggle('active', state.soundEnabled); msb.textContent = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF'; }
    savePreferences();
}

/** Toggle notifications on/off — shared by desktop and mobile buttons */
async function toggleNotify() {
    if (!('Notification' in window)) {
        alert('このブラウザはデスクトップ通知に対応していません');
        return;
    }
    const perm = await Notification.requestPermission();
    state.notificationsEnabled = perm === 'granted';
    if (perm === 'denied') {
        showToast('通知が拒否されました。ブラウザの設定から許可してください。');
    }
    updateToggleBtn(document.getElementById('btn-notify'), state.notificationsEnabled,
        '通知 ON', '通知 OFF', 'デスクトップ通知: 有効', 'デスクトップ通知: 無効');
    const mnb = document.getElementById('mobile-btn-notify');
    if (mnb) { mnb.classList.toggle('active', state.notificationsEnabled); mnb.textContent = state.notificationsEnabled ? '通知 ON' : '通知 OFF'; }
    savePreferences();
}

// ---------------------------------------------------------------------------
// Toast Notifications
// ---------------------------------------------------------------------------

function showToast(message, type = 'error') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    // Trigger slide-in animation
    requestAnimationFrame(() => {
        requestAnimationFrame(() => { toast.classList.add('toast-visible'); });
    });
    setTimeout(() => {
        toast.classList.remove('toast-visible');
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
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
            sortMode: state.sortMode,
            selectedDate: state.selectedDate,
            // M3: searchQuery intentionally NOT persisted (stale queries cause confusion)
            soundEnabled: state.soundEnabled,
            notificationsEnabled: state.notificationsEnabled,
            viewMode: state.viewMode,
            watchlistPanelOpen: !(document.getElementById('watchlist-panel')?.classList.contains('panel-collapsed')),
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

        // Restore sort mode
        if (prefs.sortMode) {
            state.sortMode = prefs.sortMode;
            const sortEl = document.getElementById('feed-sort');
            if (sortEl) sortEl.value = prefs.sortMode;
        }

        // Restore selected date (only if valid and not in the future)
        if (prefs.selectedDate && /^\d{4}-\d{2}-\d{2}$/.test(prefs.selectedDate)
            && prefs.selectedDate <= toLocalDateStr(new Date())) {
            state.selectedDate = prefs.selectedDate;
            const pickerEl = document.getElementById('date-picker');
            if (pickerEl) pickerEl.value = prefs.selectedDate;
        }

        // M3: Do NOT restore search query — stale queries from days ago
        // cause confusing "no results" on next visit. Search is session-only.

        // Restore sound preference
        if (typeof prefs.soundEnabled === 'boolean') {
            state.soundEnabled = prefs.soundEnabled;
            updateToggleBtn(document.getElementById('btn-sound'), state.soundEnabled,
                'サウンド ON', 'サウンド OFF', 'サウンドアラート: 有効', 'サウンドアラート: 無効');
            const msb = document.getElementById('mobile-btn-sound');
            if (msb) { msb.classList.toggle('active', state.soundEnabled); msb.textContent = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF'; }
        }

        // Restore notification preference
        if (typeof prefs.notificationsEnabled === 'boolean') {
            state.notificationsEnabled = prefs.notificationsEnabled;
            updateToggleBtn(document.getElementById('btn-notify'), state.notificationsEnabled,
                '通知 ON', '通知 OFF', 'デスクトップ通知: 有効', 'デスクトップ通知: 無効');
            const mnb = document.getElementById('mobile-btn-notify');
            if (mnb) { mnb.classList.toggle('active', state.notificationsEnabled); mnb.textContent = state.notificationsEnabled ? '通知 ON' : '通知 OFF'; }
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

    // M11: Redraw stock charts on window resize
    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            document.querySelectorAll('canvas[id$="-canvas"]').forEach(canvas => {
                if (canvas._chartMeta && canvas._chartMeta.prices) {
                    const options = canvas.id === 'stock-view-canvas'
                        ? { height: 400, showSMA: true }
                        : { showSMA: true };
                    renderStockChart(canvas, canvas._chartMeta.prices, options);
                }
            });
        }, 250);
    });

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

    // C4: Initialize AudioContext on first user gesture to avoid browser autoplay restrictions
    document.addEventListener('click', () => {
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioCtx.state === 'suspended') {
            audioCtx.resume();
        }
    }, { once: true });

    // H5: Mobile back button closes modal
    window.addEventListener('popstate', () => {
        const modal = document.getElementById('detail-modal');
        if (modal && !modal.classList.contains('hidden')) {
            closeModal();
        }
    });
});

function initClock() {
    const clockEl = document.getElementById('current-time');
    if (!clockEl) return;
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
    if (clockInterval) clearInterval(clockInterval);
    clockInterval = setInterval(update, 1000);
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
    document.getElementById('btn-sound').addEventListener('click', toggleSound);

    // Notification permission
    document.getElementById('btn-notify').addEventListener('click', toggleNotify);

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

    // Feed search (debounced to avoid excessive re-renders on fast typing)
    let _searchDebounce = null;
    document.getElementById('feed-search').addEventListener('input', (e) => {
        state.searchQuery = e.target.value.toLowerCase();
        clearTimeout(_searchDebounce);
        _searchDebounce = setTimeout(() => {
            renderFeed();
            savePreferences();
        }, 150);
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
        savePreferences();
    });

    // View toggle (cards / table)
    for (const mode of ['cards', 'table']) {
        document.getElementById(`view-${mode}`).addEventListener('click', () => {
            state.viewMode = mode;
            const other = mode === 'cards' ? 'table' : 'cards';
            document.getElementById(`view-${mode}`).classList.add('active');
            document.getElementById(`view-${mode}`).setAttribute('aria-pressed', 'true');
            document.getElementById(`view-${other}`).classList.remove('active');
            document.getElementById(`view-${other}`).setAttribute('aria-pressed', 'false');
            renderFeed();
            savePreferences();
        });
    }

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

    // Rankings period selector (desktop + mobile sync)
    for (const id of ['rankings-period', 'mobile-rankings-period']) {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('change', () => {
                // Sync the other selector
                const otherId = id === 'rankings-period' ? 'mobile-rankings-period' : 'rankings-period';
                const other = document.getElementById(otherId);
                if (other) other.value = el.value;
                loadAnalytics();
            });
        }
    }

    // Modal close
    document.querySelector('#detail-modal .modal-close').addEventListener('click', closeModal);
    document.querySelector('#detail-modal .modal-overlay').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeModal();
            closeConfirmDialog();
        }
        // Arrow key / Tab navigation in modal (uses filtered list, not full list)
        const modal = document.getElementById('detail-modal');
        if (modal && !modal.classList.contains('hidden')) {
            // L5: Focus trap — keep Tab within the modal
            if (e.key === 'Tab') {
                const focusable = modal.querySelectorAll('a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])');
                if (focusable.length > 0) {
                    const first = focusable[0];
                    const last = focusable[focusable.length - 1];
                    if (e.shiftKey && document.activeElement === first) {
                        e.preventDefault();
                        last.focus();
                    } else if (!e.shiftKey && document.activeElement === last) {
                        e.preventDefault();
                        first.focus();
                    }
                }
            }
            const docId = modal.dataset.filingDocId;
            if (!docId) return;
            const idx = _filteredFilings.findIndex(f => f.doc_id === docId);
            if (idx < 0) return;
            if (e.key === 'ArrowLeft' && idx > 0) {
                openModal(_filteredFilings[idx - 1]);
            } else if (e.key === 'ArrowRight' && idx < _filteredFilings.length - 1) {
                openModal(_filteredFilings[idx + 1]);
            }
            return;
        }
        // Date navigation shortcuts (only when no input focused)
        const tag = document.activeElement?.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
        if (e.key === '[' || e.key === 'ArrowLeft') { navigateDate(-1); }
        else if (e.key === ']' || e.key === 'ArrowRight') { navigateDate(1); }
        else if (e.key === 't' || e.key === 'T') {
            state.selectedDate = toLocalDateStr(new Date());
            document.getElementById('date-picker').value = state.selectedDate;
            savePreferences(); loadDateAndAutoFetch();
        }
    });
}

// ---------------------------------------------------------------------------
// SSE Connection
// ---------------------------------------------------------------------------

let _wasDisconnected = false;

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

    eventSource.addEventListener('new_tob', (e) => {
        const tob = JSON.parse(e.data);
        handleNewTob(tob);
    });

    eventSource.onopen = () => {
        const reconnected = _wasDisconnected;
        _wasDisconnected = false;
        setConnectionStatus('connected');
        // After a reconnection, refresh data to catch filings missed while offline
        if (reconnected) {
            console.log('SSE reconnected — refreshing data');
            loadFilings();
            loadStats();
        }
    };

    eventSource.onerror = () => {
        // EventSource readyState: 0=CONNECTING, 1=OPEN, 2=CLOSED
        if (eventSource.readyState === EventSource.CONNECTING) {
            _wasDisconnected = true;
            setConnectionStatus('reconnecting');
        } else {
            _wasDisconnected = true;
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

    // Show/hide the reconnection banner
    const banner = document.getElementById('sse-banner');
    if (banner) {
        if (status === 'connected') {
            banner.classList.add('hidden');
        } else {
            banner.classList.remove('hidden');
            const bannerText = banner.querySelector('.sse-banner-text');
            if (bannerText) {
                bannerText.textContent = status === 'reconnecting'
                    ? 'サーバーとの接続が切断されました。再接続を試みています...'
                    : 'サーバーとの接続が失われました。ページを再読み込みしてください。';
            }
        }
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
    await Promise.all([loadFilings(), loadStats(), loadWatchlist(), loadAnalytics(), loadTobs()]);
}

async function loadFilings() {
    // H2: Cancel any in-flight request to prevent race conditions
    if (filingsAbortController) filingsAbortController.abort();
    filingsAbortController = new AbortController();
    const signal = filingsAbortController.signal;

    // H1: Show loading indicator
    const container = document.getElementById('feed-list');
    if (!container) return;
    if (state.filings.length === 0) {
        container.innerHTML = '<div class="feed-empty"><div class="empty-icon" style="animation:pulse 1s infinite">&#8987;</div><div class="empty-text">読み込み中...</div></div>';
    }

    try {
        const params = new URLSearchParams({ limit: '500' });
        if (state.selectedDate) {
            params.set('date_from', state.selectedDate);
            params.set('date_to', state.selectedDate);
        }
        const resp = await fetch(`/api/filings?${params}`, { signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        state.filings = data.filings || [];
        renderFeed();
        updateTicker();
        preloadStockData();
    } catch (e) {
        if (e.name === 'AbortError') return; // Superseded by newer request
        console.error('Failed to load filings:', e);
        showToast('報告書の読み込みに失敗しました');
    }
}

function preloadStockData() {
    const codes = new Set();
    for (const f of state.filings) {
        const code = f.target_sec_code || f.sec_code;
        if (code) {
            const normalized = code.length === 5 ? code.slice(0, 4) : code;
            codes.add(normalized);
        }
    }
    if (codes.size === 0) return;

    // Fetch in concurrent batches of 5 for faster loading
    const uncached = [...codes].filter(c => !stockCache[c]);
    if (uncached.length === 0) return;
    const BATCH_SIZE = 5;
    let rendered = false;
    (async () => {
        for (let i = 0; i < uncached.length; i += BATCH_SIZE) {
            const batch = uncached.slice(i, i + BATCH_SIZE);
            await Promise.all(batch.map(c => fetchStockData(c).catch(() => null)));
            // Re-render after each batch so cards update progressively
            if (!rendered || i + BATCH_SIZE < uncached.length) {
                renderFeed();
                rendered = true;
            }
        }
        renderFeed();
    })();
}

async function loadStats() {
    try {
        const params = state.selectedDate ? `?date=${state.selectedDate}` : '';
        const resp = await fetch(`/api/stats${params}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        state.stats = await resp.json();
        renderStats();
    } catch (e) {
        console.error('Failed to load stats:', e);
        showToast('統計データの読み込みに失敗しました');
    }
}

// ---------------------------------------------------------------------------
// Tender Offer (TOB / 公開買付) Functions
// ---------------------------------------------------------------------------

async function loadTobs() {
    try {
        const resp = await fetch('/api/tob?limit=50');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        state.tobs = data.items || [];
        renderTobPanel();
    } catch (e) {
        console.error('Failed to load TOBs:', e);
    }
}

function handleNewTob(tob) {
    // Avoid duplicates
    if (state.tobs.some(t => t.doc_id === tob.doc_id)) return;
    state.tobs.unshift(tob);
    renderTobPanel();
    // Play alert sound and show notification
    if (state.soundEnabled) playAlertSound();
    showToast(`TOB: ${tob.tob_type} — ${tob.filer_name || '(不明)'}`);
    if (state.notificationsEnabled && Notification.permission === 'granted') {
        new Notification('公開買付 検知', {
            body: `${tob.tob_type}: ${tob.filer_name || ''} → ${tob.target_company_name || tob.doc_description || ''}`,
            tag: tob.doc_id,
        });
    }
}

function renderTobPanel() {
    const container = document.getElementById('tob-list');
    if (!container) return;
    const badge = document.getElementById('tob-count');
    if (badge) badge.textContent = state.tobs.length || '';

    if (state.tobs.length === 0) {
        container.innerHTML = '<div class="tob-empty">公開買付関連の届出はありません</div>';
        return;
    }

    container.innerHTML = state.tobs.slice(0, 30).map(t => {
        const time = t.submit_date_time ? t.submit_date_time.slice(0, 16).replace('T', ' ') : '';
        const typeClass = t.doc_type_code === '260' ? 'tob-withdraw' :
                          t.doc_type_code === '290' || t.doc_type_code === '300' ? 'tob-opinion' : 'tob-filing';
        let links = '';
        if (t.pdf_url) links += `<a href="${escapeHtml(t.pdf_url)}" target="_blank" rel="noopener" class="tob-link" onclick="event.stopPropagation()">PDF</a>`;
        if (t.edinet_url) links += `<a href="${escapeHtml(t.edinet_url)}" target="_blank" rel="noopener" class="tob-link" onclick="event.stopPropagation()">EDINET</a>`;
        return `<div class="tob-item ${typeClass}">
            <div class="tob-header">
                <span class="tob-type-badge">${escapeHtml(t.tob_type)}</span>
                <span class="tob-time">${escapeHtml(time)}</span>
            </div>
            <div class="tob-filer">${escapeHtml(t.filer_name || '(不明)')}</div>
            <div class="tob-target">${escapeHtml(t.target_company_name || t.doc_description || '')}</div>
            <div class="tob-links">${links}</div>
        </div>`;
    }).join('');
}

async function loadWatchlist() {
    try {
        const resp = await fetch('/api/watchlist');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        state.watchlist = data.watchlist || [];
        renderWatchlist();
    } catch (e) {
        console.error('Failed to load watchlist:', e);
        showToast('ウォッチリストの読み込みに失敗しました');
    }
}

// ---------------------------------------------------------------------------
// New Filing Handler
// ---------------------------------------------------------------------------

function handleNewFiling(filing) {
    lastPollTime = Date.now();

    // Only inject into feed if viewing the same date as the filing
    const filingDate = (filing.submit_date_time || '').slice(0, 10);
    if (state.selectedDate !== filingDate) {
        loadStats();
        return;
    }

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

    // Play sound — watchlist match gets a different tone, skip normal alert for those
    const isWatch = isWatchlistMatch(filing);
    if (state.soundEnabled && !isWatch) {
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
    if (!container) return;
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

    // Store filtered list for modal arrow navigation (H3)
    _filteredFilings = filtered;

    if (filtered.length === 0) {
        let emptyIcon = '&#128196;';
        let emptyMsg = '報告書が見つかりません';
        let emptyHint = '';
        if (state.searchQuery) {
            emptyMsg = `「${escapeHtml(state.searchQuery)}」に一致する報告書がありません`;
            emptyHint = '<div class="empty-hint">検索条件を変更してください</div>';
        } else if (state.filterMode !== 'all') {
            emptyMsg = 'この条件に一致する報告書がありません';
            emptyHint = '<div class="empty-hint">フィルターを「すべて」に変更してください</div>';
        } else if (state.filings.length === 0) {
            emptyIcon = '&#8987;';
            emptyMsg = 'この日のデータはまだ取得されていません';
            emptyHint = '<div class="empty-hint">FETCHボタンで取得できます</div>';
        }
        container.innerHTML = `<div class="feed-empty">
            <div class="empty-icon">${emptyIcon}</div>
            <div class="empty-text">${emptyMsg}</div>
            ${emptyHint}
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
                if (e.target.closest('a')) return;
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
                if (e.target.closest('a')) return;
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
        const { time, filerName, targetName, secCode, cls, sd, stockLoading } = prepareFilingData(f);
        const typeBadge = buildBadges(f, 'tbl-badge badge-');
        const filer = filerLinkHtml(filerName, f.edinet_code);
        const target = companyLinkHtml(targetName, secCode, false);
        const codeDisplay = secCode ? `<span class="tbl-code">${escapeHtml(secCode)}</span>` : '';

        // Ratio
        let ratioHtml = '<span class="text-dim">-</span>';
        if (f.holding_ratio != null) {
            ratioHtml = `<span class="${cls}">${f.holding_ratio.toFixed(2)}%</span>`;
        } else if (f.xbrl_flag && !f.xbrl_parsed) {
            ratioHtml = '<span class="xbrl-pending">取得中...</span>';
        }

        // Change
        let changeHtml = '<span class="text-dim">-</span>';
        if (f.ratio_change != null && f.ratio_change !== 0) {
            changeHtml = `<span class="tbl-change ${cls}">${changeDisplayHtml(f.ratio_change, cls)}</span>`;
        }

        // Previous ratio
        const prev = f.previous_holding_ratio != null ? f.previous_holding_ratio.toFixed(2) + '%' : '-';

        // Market data from stock cache
        const loadingHint = stockLoading ? '<span class="text-dim tbl-loading">...</span>' : '<span class="text-dim">-</span>';
        const mcap = sd && sd.market_cap_display ? sd.market_cap_display : loadingHint;
        const pbr = sd && sd.pbr != null ? Number(sd.pbr).toFixed(2) + '倍' : (stockLoading ? '...' : '-');
        const price = sd && sd.current_price != null ? '\u00a5' + Math.round(sd.current_price).toLocaleString() : (stockLoading ? '...' : '-');

        const links = buildDocLinks(f, 'tbl-link');

        // Row class
        let rowClass = '';
        if (f.ratio_change > 0) rowClass = 'row-up';
        else if (f.ratio_change < 0) rowClass = 'row-down';
        if (isWatchlistMatch(f)) rowClass += ' row-watch';

        html += `<tr class="${rowClass}" data-doc-id="${escapeHtml(f.doc_id)}">
            ${phone ? '' : `<td class="col-type">${typeBadge}</td>`}
            <td class="col-filer" title="${escapeHtml(filerName)}">${filer}</td>
            <td class="col-target" title="${escapeHtml(targetName)}">${target}${codeDisplay ? ' ' + codeDisplay : ''}</td>
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
            if (e.target.closest('a')) return;
            const docId = row.dataset.docId;
            const filing = state.filings.find(f => f.doc_id === docId);
            if (filing) openModal(filing);
        });
    });
}

function createFeedCard(f) {
    const { time, filerName, targetName, secCode, cls, sd, isChange } = prepareFilingData(f);
    let cardClass = f.is_amendment ? 'amendment' : isChange ? 'change-report' : 'new-report';
    if (f.ratio_change > 0) cardClass += ' ratio-up';
    else if (f.ratio_change < 0) cardClass += ' ratio-down';

    let badge = buildBadges(f, 'card-badge badge-');
    if (isWatchlistMatch(f)) {
        badge += '<span class="card-badge badge-watchlist">WATCH</span>';
    }

    const filerHtml = filerLinkHtml(filerName, f.edinet_code);
    const targetHtml = companyLinkHtml(targetName, secCode, true);

    // Ratio with before→after flow display
    let ratioHtml = '';
    if (f.holding_ratio != null) {

        let changeHtml = '';
        if (f.ratio_change != null && f.ratio_change !== 0) {
            changeHtml = `<span class="ratio-change-pill ${cls}">${changeDisplayHtml(f.ratio_change)}</span>`;
        }

        // Flow row: prev → curr [change pill], or just curr if no prev
        let flowHtml;
        if (f.previous_holding_ratio != null) {
            flowHtml = `<div class="ratio-flow">
                <span class="ratio-flow-prev">${f.previous_holding_ratio.toFixed(2)}%</span>
                <span class="ratio-flow-arrow ${cls}">→</span>
                <span class="ratio-flow-curr ${cls}">${f.holding_ratio.toFixed(2)}%</span>
                ${changeHtml}
            </div>`;
        } else {
            flowHtml = `<div class="ratio-flow">
                <span class="ratio-flow-curr ${cls}">${f.holding_ratio.toFixed(2)}%</span>
                ${changeHtml}
            </div>`;
        }

        // Delta bar: base(prev) + delta zone + curr marker
        const currW = Math.min(f.holding_ratio, 100);
        const prevW = f.previous_holding_ratio != null ? Math.min(f.previous_holding_ratio, 100) : 0;
        let barInner = '';
        if (prevW > 0 && f.ratio_change != null && f.ratio_change !== 0) {
            const minW = Math.min(prevW, currW);
            const maxW = Math.max(prevW, currW);
            barInner += `<div class="ratio-bar ratio-bar-prev" style="width: ${minW}%"></div>`;
            barInner += `<div class="ratio-bar ratio-bar-delta ${cls}" style="left: ${minW}%; width: ${maxW - minW}%"></div>`;
        } else {
            barInner += `<div class="ratio-bar ratio-bar-curr ${cls}" style="width: ${currW}%"></div>`;
        }
        const barHtml = `<div class="ratio-bar-container">${barInner}</div>`;

        ratioHtml = `<div class="ratio-display ${cls}">
            ${flowHtml}
            ${barHtml}
        </div>`;
    } else if (f.xbrl_flag && !f.xbrl_parsed) {
        ratioHtml = '<div class="ratio-display neutral"><span class="card-ratio xbrl-pending">取得中...</span></div>';
    } else {
        ratioHtml = '<div class="ratio-display neutral"><span class="card-ratio neutral">-</span></div>';
    }

    // Purpose of holding (short values like 純投資, 経営参加)
    let purposeHtml = '';
    if (f.purpose_of_holding) {
        const short = f.purpose_of_holding.length <= 20 ? f.purpose_of_holding : f.purpose_of_holding.slice(0, 18) + '…';
        purposeHtml = `<span class="card-purpose">${escapeHtml(short)}</span>`;
    }

    const links = buildDocLinks(f, 'card-link');

    // Market data from cache (desktop only)
    let marketDataHtml = '';
    if (sd) {
        const parts = [];
        if (sd.market_cap_display) parts.push(`時価:${sd.market_cap_display}`);
        if (sd.pbr != null) parts.push(`PBR:${Number(sd.pbr).toFixed(2)}倍`);
        if (sd.current_price != null) parts.push(`\u00a5${Math.round(sd.current_price).toLocaleString()}`);
        if (parts.length > 0) {
            marketDataHtml = `<div class="card-market-data">${parts.map(p => `<span>${p}</span>`).join('')}</div>`;
        }
    }

    return `
        <div class="feed-card ${cardClass}" data-doc-id="${escapeHtml(f.doc_id)}" role="article">
            <div class="card-top">
                <div>${badge}</div>
                <span class="card-time">${escapeHtml(time)}</span>
            </div>
            <div class="card-main">
                <span class="card-filer">${filerHtml}</span>
                <span class="card-arrow">&#x2192;</span>
                <span class="card-target">${targetHtml}</span>
                <div class="card-desc">${escapeHtml(f.doc_description || '')}</div>
                ${marketDataHtml}
            </div>
            <div class="card-bottom">
                ${ratioHtml}
                <div class="card-links">${purposeHtml}${links}</div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Mobile Feed Card - completely different layout optimized for phone screens
// Target company as headline, ratio+market cap as hero metrics
// ---------------------------------------------------------------------------

function createMobileFeedCard(f) {
    const { time: rawTime, filerName, targetName, secCode, cls, sd, stockLoading, isChange } = prepareFilingData(f);
    const time = rawTime === '-' ? '' : rawTime;
    let cardClass = f.is_amendment ? 'amendment' : isChange ? 'change-report' : 'new-report';
    if (f.ratio_change > 0) cardClass += ' ratio-up';
    else if (f.ratio_change < 0) cardClass += ' ratio-down';

    // Badge (mobile uses m-badge prefix; amendment → amend)
    let badge = buildBadges(f, 'm-badge m-badge-');
    badge = badge.replace('m-badge-amendment', 'm-badge-amend');
    if (isWatchlistMatch(f)) {
        badge += '<span class="m-badge m-badge-watch">&#9733;</span>';
    }

    const targetCode = secCode ? `[${secCode}]` : '';
    const mTargetHtml = companyLinkHtml(targetName, secCode, false);
    const mFilerHtml = filerLinkHtml(filerName, f.edinet_code);

    let ratioVal;
    if (f.holding_ratio != null && f.previous_holding_ratio != null) {
        ratioVal = `<span class="text-dim" style="font-size:11px">${f.previous_holding_ratio.toFixed(2)}%</span>`
            + `<span class="${cls}" style="font-size:11px;font-weight:700;margin:0 2px">→</span>`
            + `${f.holding_ratio.toFixed(2)}%`;
    } else if (f.holding_ratio != null) {
        ratioVal = `${f.holding_ratio.toFixed(2)}%`;
    } else if (f.xbrl_flag && !f.xbrl_parsed) {
        ratioVal = '<span class="xbrl-pending" style="font-size:11px">取得中...</span>';
    } else {
        ratioVal = '<span class="text-dim" style="font-size:11px">割合未取得</span>';
    }

    let changeHtml = '';
    if (f.ratio_change != null && f.ratio_change !== 0) {
        changeHtml = `<span class="m-change ${cls}">${changeDisplayHtml(f.ratio_change)}</span>`;
    }

    // Market data from stock cache
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
    const linkHtml = buildDocLinks(f, 'm-link');

    return `<div class="m-card ${cardClass}" data-doc-id="${escapeHtml(f.doc_id)}">
    <div class="m-card-head">
        ${badge}
        <span class="m-target">${mTargetHtml}</span>
        <span class="m-code">${escapeHtml(targetCode)}</span>
        <span class="m-time">${escapeHtml(time)}</span>
    </div>
    <div class="m-card-data">
        <span class="m-ratio ${cls}">${ratioVal}</span>
        ${changeHtml}
        ${sep}${mcapHtml}${priceHtml}${pbrHtml}
    </div>
    <div class="m-card-foot">
        <span class="m-filer">${mFilerHtml}</span>
        ${linkHtml}
    </div>
</div>`;
}

function renderStats() {
    const s = state.stats;
    const fmt = v => v != null ? Number(v).toLocaleString() : '-';
    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setEl('stat-total', fmt(s.today_total));
    // Update header filing count badges (desktop + mobile)
    const badge = document.getElementById('filing-count-badge');
    if (badge) badge.textContent = fmt(s.today_total);
    const badgeMobile = document.getElementById('filing-count-badge-mobile');
    if (badgeMobile) badgeMobile.textContent = fmt(s.today_total);
    setEl('stat-new', fmt(s.today_new_reports));
    setEl('stat-amendments', fmt(s.today_amendments));
    setEl('stat-clients', fmt(s.connected_clients));

    // Update panel title to show the selected date
    const isToday = state.selectedDate === toLocalDateStr(new Date());
    const statsTitle = document.querySelector('#stats-panel .panel-title');
    if (statsTitle) {
        statsTitle.textContent = isToday ? 'TODAY' : state.selectedDate;
    }

    // Top filers
    const filersList = document.getElementById('top-filers-list');
    if (!filersList) return;
    if (s.top_filers && s.top_filers.length > 0) {
        filersList.innerHTML = s.top_filers.map(f => {
            const name = f.name || '(不明)';
            return `<div class="filer-row">
                <span class="filer-name" title="${escapeHtml(name)}">${filerLinkHtml(name, f.edinet_code)}</span>
                <span class="filer-count">${f.count}</span>
            </div>`;
        }).join('');
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

    for (const [item, label, cls] of [
        [largestIncrease, '最大増加', 'positive'],
        [largestDecrease, '最大減少', 'negative'],
    ]) {
        if (!item || (cls === 'positive' ? item.ratio_change <= 0 : item.ratio_change >= 0)) continue;
        const name = item.target_company_name || item.filer_name || '不明';
        const sign = item.ratio_change > 0 ? '+' : '';
        html += `
        <div class="summary-highlight">
            <div class="summary-highlight-label">${label}</div>
            <div class="summary-highlight-value ${cls}">${sign}${item.ratio_change.toFixed(2)}%</div>
            <div class="summary-highlight-company">${companyLinkHtml(name, item.target_sec_code || item.sec_code, false)}</div>
        </div>`;
    }

    container.innerHTML = html;
}

function renderWatchlist() {
    const container = document.getElementById('watchlist-items');
    if (!container) return;
    if (state.watchlist.length === 0) {
        container.innerHTML = `<div class="watchlist-empty">
            <div class="empty-icon">&#9734;</div>
            <div class="empty-text">ウォッチリストに企業を追加してください</div>
        </div>`;
        return;
    }

    container.innerHTML = state.watchlist.map(w => {
        const code = normalizeSecCode(w.sec_code);
        const sd = getCachedStock(w.sec_code);
        let priceHtml = '';
        if (sd && sd.current_price != null) {
            priceHtml = `<span class="watch-price">&yen;${Math.round(sd.current_price).toLocaleString()}</span>`;
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

    if (!name) {
        showToast('企業名を入力してください');
        document.getElementById('watch-name').focus();
        return;
    }

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
        } else {
            const errData = await resp.json().catch(() => ({}));
            showToast(errData.error || 'ウォッチリスト登録に失敗しました');
        }
    } catch (e) {
        console.error('Failed to save watchlist item:', e);
        showToast('ウォッチリスト登録に失敗しました');
    }
}

async function deleteWatchItem(id) {
    try {
        const resp = await fetch(`/api/watchlist/${id}`, { method: 'DELETE' });
        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            showToast(errData.error || 'ウォッチリスト削除に失敗しました');
            return;
        }
        await loadWatchlist();
    } catch (e) {
        console.error('Failed to delete watchlist item:', e);
        showToast('ウォッチリスト削除に失敗しました');
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
    // M7: Play watchlist-specific sound (higher tone) — normal alert is
    // suppressed in handleNewFiling when this is a watchlist match to avoid double beep
    if (state.soundEnabled) {
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
        // Clean up stale event listeners to prevent previous delete handlers from firing
        const confirmBtn = document.getElementById('dialog-confirm');
        const cancelBtn = document.getElementById('dialog-cancel');
        if (confirmBtn) confirmBtn.replaceWith(confirmBtn.cloneNode(true));
        if (cancelBtn) cancelBtn.replaceWith(cancelBtn.cloneNode(true));
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
        ctx.fillStyle = '#8a8aa8';
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
    ctx.fillStyle = '#8a8aa8';
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
 * Attach hover/touch tooltip to a chart canvas (call once after first render).
 */
function attachChartTooltip(canvas, tooltipEl) {
    if (canvas._tooltipAttached) return;
    canvas._tooltipAttached = true;

    function showTooltipAt(clientX, clientY) {
        const meta = canvas._chartMeta;
        if (!meta) return;

        const rect = canvas.getBoundingClientRect();
        const mx = clientX - rect.left;

        const { prices, padding, gap } = meta;
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

        const tooltipW = tooltipEl.offsetWidth;
        const tooltipH = tooltipEl.offsetHeight;
        const containerRect = canvas.parentElement.getBoundingClientRect();
        let left = clientX - containerRect.left + 12;
        let top = clientY - containerRect.top - tooltipH / 2;

        if (left + tooltipW > containerRect.width - 5) {
            left = clientX - containerRect.left - tooltipW - 12;
        }
        top = Math.max(4, Math.min(top, containerRect.height - tooltipH - 4));

        tooltipEl.style.left = left + 'px';
        tooltipEl.style.top = top + 'px';
    }

    // Mouse events (desktop)
    canvas.addEventListener('mousemove', (e) => {
        showTooltipAt(e.clientX, e.clientY);
    });

    canvas.addEventListener('mouseleave', () => {
        tooltipEl.style.display = 'none';
    });

    // H7: Touch events (mobile)
    canvas.addEventListener('touchstart', (e) => {
        e.preventDefault();
        const touch = e.touches[0];
        showTooltipAt(touch.clientX, touch.clientY);
    }, { passive: false });

    canvas.addEventListener('touchmove', (e) => {
        e.preventDefault();
        const touch = e.touches[0];
        showTooltipAt(touch.clientX, touch.clientY);
    }, { passive: false });

    canvas.addEventListener('touchend', () => {
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
        if (code.length < 4) {
            showToast('4桁以上の証券コードを入力してください');
            return;
        }
        loadStockView(code);
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

async function retryXbrl(docId) {
    const btn = document.querySelector('.btn-retry-xbrl');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '取得中...';
    }
    try {
        const resp = await fetch(`/api/documents/${docId}/retry-xbrl`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            // Refresh filing data and re-open modal
            const freshResp = await fetch(`/api/filings/${docId}`);
            if (freshResp.ok) {
                const freshFiling = await freshResp.json();
                // Update in state
                const idx = state.filings.findIndex(f => f.doc_id === docId);
                if (idx >= 0) state.filings[idx] = freshFiling;
                renderFeed();
                openModal(freshFiling);
            }
        } else {
            if (btn) btn.textContent = data.error || '取得失敗';
        }
    } catch (e) {
        console.error('XBRL retry failed:', e);
        if (btn) btn.textContent = '取得失敗';
    }
}

function exportFilingsCSV() {
    // H4: Export filtered/searched results, not full list
    const filings = _filteredFilings.length > 0 ? _filteredFilings : state.filings;
    if (!filings || filings.length === 0) {
        showToast('エクスポートするデータがありません');
        return;
    }

    const headers = ['提出日時','提出者','対象企業','証券コード','保有割合(%)','前回保有割合(%)','変動(%)','保有株数','保有目的','書類種別','書類ID'];
    const rows = filings.map(f => [
        f.submit_date_time || '',
        f.holder_name || f.filer_name || '',
        f.target_company_name || '',
        f.target_sec_code || f.sec_code || '',
        f.holding_ratio != null ? f.holding_ratio : '',
        f.previous_holding_ratio != null ? f.previous_holding_ratio : '',
        f.ratio_change != null ? f.ratio_change : '',
        f.shares_held != null ? f.shares_held : '',
        f.purpose_of_holding || '',
        f.doc_description || '',
        f.doc_id || '',
    ]);

    const csvContent = '\uFEFF' + [headers, ...rows].map(r =>
        r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')
    ).join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `edinet_filings_${state.selectedDate}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

async function batchRetryXbrl() {
    const btn = document.getElementById('btn-batch-retry');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '一括再取得中...';
    try {
        const resp = await fetch('/api/documents/batch-retry-xbrl', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            btn.textContent = `完了 (${data.enriched}/${data.processed}件)`;
            // Reload filings to reflect updated data
            await loadInitialData();
        } else {
            btn.textContent = data.error || '失敗';
        }
    } catch (e) {
        console.error('Batch retry failed:', e);
        btn.textContent = '失敗';
    }
    setTimeout(() => {
        btn.disabled = false;
        btn.textContent = 'XBRL 一括再取得';
    }, 5000);
}

function openModal(filing) {
    // L5: Save current focus for restoration on close
    if (!_preFocusedElement) _preFocusedElement = document.activeElement;
    currentModalDocId = filing.doc_id;
    const body = document.getElementById('modal-body');

    // Clickable filer name -> filer profile
    const filerDisplay = filing.edinet_code
        ? { html: `<span class="detail-value"><a href="#" onclick="event.preventDefault();openFilerProfile('${escapeHtml(filing.edinet_code)}')">${escapeHtml(filing.filer_name || '-')}</a></span>` }
        : (filing.filer_name || '-');

    const rows = [
        ['書類ID', filing.doc_id],
        ['書類種別', filing.doc_description || '-'],
        ['提出日時', filing.submit_date_time || '-'],
        ['提出者', filerDisplay],
    ];
    if (filing.holder_name && filing.holder_name !== filing.filer_name) {
        rows.push(['報告者 (XBRL)', filing.holder_name]);
    }

    // Clickable target company -> company profile
    const targetName = filing.target_company_name || extractTargetFromDescription(filing.doc_description) || '-';
    const targetSecCode = filing.target_sec_code || filing.sec_code;
    const targetDisplay = targetSecCode
        ? { html: `<span class="detail-value"><a href="#" onclick="event.preventDefault();openCompanyProfile('${escapeHtml(targetSecCode)}')">${escapeHtml(targetName)}</a></span>` }
        : targetName;

    rows.push(
        ['EDINET コード', filing.edinet_code || '-'],
        ['対象会社', targetDisplay],
        ['対象証券コード', targetSecCode || '-'],
    );

    // Visual ratio gauge section with before/after comparison
    let ratioGaugeHtml = '';
    if (filing.holding_ratio != null) {
        const ratioClass = filing.ratio_change > 0 ? 'positive' : filing.ratio_change < 0 ? 'negative' : 'neutral';
        const currWidth = Math.min(filing.holding_ratio, 100);
        const prevWidth = filing.previous_holding_ratio != null ? Math.min(filing.previous_holding_ratio, 100) : 0;
        const hasPrev = filing.previous_holding_ratio != null;

        // Change badge
        let changeBadge = '';
        if (filing.ratio_change != null && filing.ratio_change !== 0) {
            const arrow = filing.ratio_change > 0 ? '▲' : '▼';
            const sign = filing.ratio_change > 0 ? '+' : '';
            changeBadge = `<div class="modal-ratio-change-row">
                <span class="modal-ratio-change ${ratioClass}">${arrow} ${sign}${filing.ratio_change.toFixed(3)}%</span>
            </div>`;
        }

        // Header: before → after comparison or just current value
        let headerHtml;
        if (hasPrev) {
            headerHtml = `
                <div class="modal-ratio-compare">
                    <div class="modal-ratio-side">
                        <span class="modal-ratio-side-label">前回</span>
                        <span class="modal-ratio-side-value prev">${filing.previous_holding_ratio.toFixed(3)}%</span>
                    </div>
                    <span class="modal-ratio-arrow ${ratioClass}">→</span>
                    <div class="modal-ratio-side">
                        <span class="modal-ratio-side-label">今回</span>
                        <span class="modal-ratio-side-value curr ${ratioClass}">${filing.holding_ratio.toFixed(3)}%</span>
                    </div>
                </div>
                ${changeBadge}`;
        } else {
            headerHtml = `
                <div class="modal-ratio-header">
                    <span class="modal-ratio-value ${ratioClass}">${filing.holding_ratio.toFixed(3)}%</span>
                </div>`;
        }

        // Gauge with delta zone and 5% threshold marker
        const minW = Math.min(prevWidth, currWidth);
        const maxW = Math.max(prevWidth, currWidth);
        let gaugeInner = '';
        if (hasPrev && filing.ratio_change != null && filing.ratio_change !== 0) {
            // Base bar up to smaller value
            gaugeInner += `<div class="gauge-bar gauge-prev" style="width: ${minW}%"></div>`;
            // Delta zone (glowing)
            gaugeInner += `<div class="gauge-bar gauge-delta ${ratioClass}" style="left: ${minW}%; width: ${maxW - minW}%"></div>`;
        } else {
            gaugeInner += `<div class="gauge-bar gauge-curr ${ratioClass}" style="width: ${currWidth}%"></div>`;
        }

        // 5% threshold marker (the reporting threshold for large shareholding)
        const maxRatio = Math.max(filing.holding_ratio, filing.previous_holding_ratio || 0, 5);
        const gaugeScale = Math.min(maxRatio * 2, 100); // Scale gauge so bars aren't tiny
        const thresholdPos = Math.min((5 / gaugeScale) * 100, 100);
        // Only show if threshold is within visible range
        const thresholdHtml = gaugeScale >= 5
            ? `<div class="gauge-threshold" style="left: ${thresholdPos}%"><span class="gauge-threshold-label">5%</span></div>`
            : '';

        ratioGaugeHtml = `
            <div class="modal-ratio-section">
                ${headerHtml}
                <div class="modal-ratio-gauge">
                    ${gaugeInner}
                    ${thresholdHtml}
                    <div class="gauge-labels">
                        ${hasPrev ? `<span class="gauge-label" style="left: ${prevWidth}%">前回</span>` : ''}
                    </div>
                </div>
                <div class="modal-ratio-footer">
                    <span>0%</span>
                    <span>50%</span>
                    <span>100%</span>
                </div>
            </div>
        `;
    } else if (filing.xbrl_flag) {
        // xbrl_flag is true but no holding_ratio — either not yet parsed, or parsed but empty
        const label = filing.xbrl_parsed ? 'XBRL 割合データなし' : 'XBRL データ未取得';
        ratioGaugeHtml = `
            <div class="modal-ratio-section">
                <div class="modal-ratio-header">
                    <span class="xbrl-pending-label">${label}</span>
                    <button class="btn-retry-xbrl" onclick="retryXbrl('${escapeHtml(filing.doc_id)}')">再取得</button>
                </div>
            </div>
        `;
    }

    // XBRL holding detail section
    if (filing.shares_held != null || filing.purpose_of_holding) {
        if (filing.shares_held != null) {
            rows.push(['保有株数', filing.shares_held.toLocaleString() + ' 株']);
        }
        if (filing.purpose_of_holding) {
            rows.push(['保有目的', filing.purpose_of_holding]);
        }
    }

    // Fund source (取得資金)
    if (filing.fund_source) {
        rows.push(['取得資金', filing.fund_source]);
    }

    // Joint holders (共同保有者)
    if (filing.joint_holders) {
        try {
            const jh = JSON.parse(filing.joint_holders);
            if (Array.isArray(jh) && jh.length > 0) {
                const jhHtml = jh.map(h => {
                    const ratio = h.ratio != null ? ` (${h.ratio}%)` : '';
                    return `<div class="joint-holder-item">${escapeHtml(h.name)}${ratio}</div>`;
                }).join('');
                rows.push(['共同保有者', { html: `<span class="detail-value">${jhHtml}</span>` }]);
            }
        } catch (e) {
            rows.push(['共同保有者', filing.joint_holders]);
        }
    }

    // Amendment → original link
    if (filing.is_amendment && filing.parent_doc_id) {
        const parentUrl = `https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?${filing.parent_doc_id},,,`;
        rows.push(['原本書類', {
            html: `<span class="detail-value"><a href="${parentUrl}" target="_blank" rel="noopener" class="parent-doc-link">${escapeHtml(filing.parent_doc_id)}</a>
                <a href="/api/documents/${escapeHtml(filing.parent_doc_id)}/pdf" target="_blank" rel="noopener" class="card-link" style="margin-left:8px">原本PDF</a></span>`,
        }]);
    }

    // Links
    const links = [];
    if (filing.pdf_url) {
        links.push(`<a href="${escapeHtml(filing.pdf_url)}" target="_blank" rel="noopener">PDF ダウンロード</a>`);
    }
    if (filing.english_doc_flag && filing.edinet_url) {
        links.push(`<a href="${escapeHtml(filing.edinet_url)}" target="_blank" rel="noopener">英文書類 (EDINET)</a>`);
    }
    if (filing.edinet_url) {
        links.push(`<a href="${escapeHtml(filing.edinet_url)}" target="_blank" rel="noopener">EDINET で閲覧</a>`);
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
                let priceStr = `\u00a5${Math.round(stockData.current_price).toLocaleString()}`;
                // Show change from previous close
                if (stockData.price_change != null) {
                    const cls = stockData.price_change > 0 ? 'positive' : stockData.price_change < 0 ? 'negative' : '';
                    const sign = stockData.price_change > 0 ? '+' : '';
                    const pctStr = stockData.price_change_pct != null ? ` (${sign}${stockData.price_change_pct.toFixed(2)}%)` : '';
                    priceStr += ` <span class="${cls}" style="font-size:0.85em">${sign}${stockData.price_change.toFixed(1)}${pctStr}</span>`;
                }
                infoHtml += `<span class="stock-price">${priceStr}</span>`;
            }
            if (stockData.market_cap_display) {
                infoHtml += `<span class="stock-metric">時価: <strong>${stockData.market_cap_display}</strong></span>`;
            }
            if (stockData.pbr != null) {
                infoHtml += `<span class="stock-metric">PBR: <strong>${Number(stockData.pbr).toFixed(2)}倍</strong></span>`;
            }
            if (stockData.per != null) {
                infoHtml += `<span class="stock-metric">PER: <strong>${Number(stockData.per).toFixed(1)}倍</strong></span>`;
            }
            if (stockData.dividend_yield != null) {
                infoHtml += `<span class="stock-metric">配当: <strong>${stockData.dividend_yield.toFixed(2)}%</strong></span>`;
            }
            if (stockData.week52_high != null && stockData.week52_low != null) {
                infoHtml += `<span class="stock-metric">52W: <strong>\u00a5${Math.round(stockData.week52_low).toLocaleString()}-${Math.round(stockData.week52_high).toLocaleString()}</strong></span>`;
            }
            if (stockData.volume != null) {
                const volStr = stockData.volume >= 1000000 ? (stockData.volume / 1000000).toFixed(1) + 'M' : stockData.volume >= 1000 ? (stockData.volume / 1000).toFixed(0) + 'K' : stockData.volume.toString();
                infoHtml += `<span class="stock-metric">出来高: <strong>${volStr}</strong></span>`;
            }
            infoHtml += '</div>';

            // Price source indicator (small, for transparency)
            if (stockData.price_source && stockData.price_source !== 'fallback') {
                infoHtml += `<div style="text-align:right;font-size:10px;opacity:0.4;margin-top:2px">source: ${escapeHtml(stockData.price_source)}</div>`;
            }

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

    // Store current filing doc_id for keyboard navigation (uses filtered list)
    document.getElementById('detail-modal').dataset.filingDocId = filing.doc_id;

    // H5: Push history state for mobile back button support
    const modal = document.getElementById('detail-modal');
    if (modal.classList.contains('hidden')) {
        history.pushState({ modal: true }, '');
    }
    modal.classList.remove('hidden');

    // L5: Focus trap — focus the modal close button
    requestAnimationFrame(() => {
        const closeBtn = modal.querySelector('.modal-close');
        if (closeBtn) closeBtn.focus();
    });
}

let _preFocusedElement = null; // L5: Store element to restore focus on modal close

function closeModal() {
    currentModalDocId = null;
    const modal = document.getElementById('detail-modal');
    modal.classList.add('hidden');
    // L5: Restore focus
    if (_preFocusedElement) {
        _preFocusedElement.focus();
        _preFocusedElement = null;
    }
}

// ---------------------------------------------------------------------------
// Profile Views (Filer / Company)
// ---------------------------------------------------------------------------

async function openProfileModal(apiPath, loadingMsg, errorMsg, renderFn) {
    const body = document.getElementById('modal-body');
    const modal = document.getElementById('detail-modal');
    if (!body || !modal) return;
    currentModalDocId = null;
    body.innerHTML = `<div class="stock-loading">${loadingMsg}</div>`;
    modal.classList.remove('hidden');
    try {
        const resp = await fetch(apiPath);
        if (!resp.ok) { body.innerHTML = `<div class="stock-no-data">${errorMsg}</div>`; return; }
        const data = await resp.json();
        body.innerHTML = renderFn(data);
        attachProfileFilingHandlers();
    } catch (e) {
        console.error('Profile error:', e);
        body.innerHTML = '<div class="stock-no-data">プロフィールの読み込みに失敗しました</div>';
    }
}

function _profileItemsTable(title, headers, items, rowFn) {
    if (!items || items.length === 0) return '';
    let html = `<div class="profile-section-title">${title}</div>`;
    html += `<div class="feed-table-wrapper"><table class="feed-table"><thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>`;
    for (const item of items) {
        const ratio = item.latest_ratio != null ? item.latest_ratio.toFixed(2) + '%' : '-';
        let trend = '';
        if (item.history && item.history.length >= 2) {
            const vals = item.history.slice().reverse().slice(-10).map(p => p.ratio);
            trend = miniSparkline(vals);
        }
        html += rowFn(item, ratio, trend);
    }
    return html + '</tbody></table></div>';
}

async function openFilerProfile(edinetCode) {
    if (!edinetCode) return;
    openProfileModal(
        `/api/analytics/filer/${encodeURIComponent(edinetCode)}`,
        '提出者プロフィール読み込み中...', '提出者データが見つかりません',
        (data) => {
            const s = data.summary;
            let html = `<div class="profile-header">
                <div class="profile-name">${escapeHtml(data.filer_name)}</div>
                <div class="profile-meta">${escapeHtml(data.edinet_code)}</div>
            </div>`;
            html += `<div class="profile-stats">
                <div class="profile-stat"><span class="profile-stat-value">${s.total_filings}</span><span class="profile-stat-label">提出件数</span></div>
                <div class="profile-stat"><span class="profile-stat-value">${s.unique_targets}</span><span class="profile-stat-label">対象企業数</span></div>
                <div class="profile-stat"><span class="profile-stat-value">${s.avg_holding_ratio != null ? s.avg_holding_ratio + '%' : '-'}</span><span class="profile-stat-label">平均保有割合</span></div>
            </div>`;
            if (s.first_filing && s.last_filing) {
                html += `<div class="profile-period">${escapeHtml(s.first_filing.slice(0,10))} 〜 ${escapeHtml(s.last_filing.slice(0,10))}</div>`;
            }
            // Timeline chart
            if (data.timeline && data.timeline.length >= 2) {
                html += renderTimelineChart(data.timeline, 'filer');
            }
            html += _profileItemsTable('保有銘柄一覧', ['対象企業','コード','最新割合','件数','推移'], data.targets, (t, ratio, trend) => {
                const link = t.sec_code
                    ? `<a href="#" onclick="event.preventDefault();openCompanyProfile('${escapeJsString(t.sec_code)}')">${escapeHtml(t.company_name || '不明')}</a>`
                    : escapeHtml(t.company_name || '不明');
                return `<tr><td>${link}</td><td>${escapeHtml(t.sec_code || '-')}</td><td>${ratio}</td><td>${t.filing_count}</td><td>${trend}</td></tr>`;
            });
            // Related TOB filings
            if (data.related_tobs && data.related_tobs.length > 0) {
                html += renderRelatedTobs(data.related_tobs);
            }
            // All filings (paginated)
            if (data.recent_filings && data.recent_filings.length > 0) html += renderProfileFilings(data.recent_filings);
            if (data.has_more) {
                html += `<div class="profile-load-more" data-profile-type="filer" data-profile-key="${escapeHtml(data.edinet_code)}" data-offset="${data.recent_filings.length}">さらに読み込む...</div>`;
            }
            return html;
        }
    );
}

async function openCompanyProfile(secCode) {
    if (!secCode) return;
    openProfileModal(
        `/api/analytics/company/${encodeURIComponent(secCode)}`,
        '企業プロフィール読み込み中...', '企業データが見つかりません',
        (data) => {
            let html = `<div class="profile-header">
                <div class="profile-name">${escapeHtml(data.company_name || secCode)}</div>
                <div class="profile-meta">[${escapeHtml(data.sec_code)}] ${escapeHtml(data.sector || '')}</div>
            </div>`;
            // Company info panel (from CompanyInfo model)
            if (data.company_info) {
                html += renderCompanyInfoPanel(data.company_info);
            }
            html += `<div class="profile-stats">
                <div class="profile-stat"><span class="profile-stat-value">${data.holder_count}</span><span class="profile-stat-label">大量保有者数</span></div>
                <div class="profile-stat"><span class="profile-stat-value">${data.total_filings}</span><span class="profile-stat-label">報告件数</span></div>
            </div>`;
            // Timeline chart
            if (data.timeline && data.timeline.length >= 2) {
                html += renderTimelineChart(data.timeline, 'company');
            }
            html += _profileItemsTable('大量保有者一覧', ['保有者','最新割合','件数','推移'], data.holders, (h, ratio, trend) => {
                const link = h.edinet_code
                    ? `<a href="#" onclick="event.preventDefault();openFilerProfile('${escapeJsString(h.edinet_code)}')">${escapeHtml(h.filer_name || '不明')}</a>`
                    : escapeHtml(h.filer_name || '不明');
                return `<tr><td>${link}</td><td>${ratio}</td><td>${h.filing_count}</td><td>${trend}</td></tr>`;
            });
            // Related TOB filings
            if (data.related_tobs && data.related_tobs.length > 0) {
                html += renderRelatedTobs(data.related_tobs);
            }
            // All filings (paginated)
            if (data.recent_filings && data.recent_filings.length > 0) html += renderProfileFilings(data.recent_filings);
            if (data.has_more) {
                html += `<div class="profile-load-more" data-profile-type="company" data-profile-key="${escapeHtml(data.sec_code)}" data-offset="${data.recent_filings.length}">さらに読み込む...</div>`;
            }
            return html;
        }
    );
}

/** Generate an inline SVG sparkline from an array of numbers. */
function miniSparkline(values) {
    if (!values || values.length < 2) return '';
    const w = 80, h = 20, pad = 2;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const points = values.map((v, i) => {
        const x = pad + (i / (values.length - 1)) * (w - 2 * pad);
        const y = h - pad - ((v - min) / range) * (h - 2 * pad);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const color = values[values.length - 1] >= values[0] ? 'var(--green)' : 'var(--red)';
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

/** Render a compact filing list for profile views (all filings, collapsible). */
function renderProfileFilings(filings) {
    // Store filings for click access
    window._profileFilings = filings;
    const initialShow = 20;
    const total = filings.length;
    let html = `<div class="profile-section-title">報告書一覧 <span class="profile-filing-count">(${total}件)</span></div>`;
    html += '<div class="profile-filings">';
    for (let i = 0; i < total; i++) {
        const f = filings[i];
        const date = f.submit_date_time ? f.submit_date_time.slice(0, 10) : '-';
        const desc = f.doc_description || '';
        const ratio = f.holding_ratio != null ? f.holding_ratio.toFixed(2) + '%' : '-';
        let changeHtml = '';
        if (f.ratio_change != null && f.ratio_change !== 0) {
            const cls = f.ratio_change > 0 ? 'positive' : 'negative';
            const sign = f.ratio_change > 0 ? '+' : '';
            changeHtml = `<span class="${cls}">${sign}${f.ratio_change.toFixed(2)}%</span>`;
        }
        const hiddenClass = i >= initialShow ? ' profile-filing-hidden' : '';
        html += `<div class="profile-filing-row${hiddenClass}" data-pf-idx="${i}">
            <span class="profile-filing-date">${escapeHtml(date)}</span>
            <span class="profile-filing-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</span>
            <span class="profile-filing-ratio">${ratio}</span>
            <span class="profile-filing-change">${changeHtml}</span>
        </div>`;
    }
    html += '</div>';
    if (total > initialShow) {
        html += `<div class="profile-show-all-btn" onclick="document.querySelectorAll('.profile-filing-hidden').forEach(el=>el.classList.remove('profile-filing-hidden'));this.remove()">全${total}件を表示</div>`;
    }
    return html;
}

/** Attach click handlers for profile filing rows and load-more buttons. */
function attachProfileFilingHandlers() {
    document.querySelectorAll('.profile-filing-row[data-pf-idx]').forEach(row => {
        row.addEventListener('click', () => {
            const idx = parseInt(row.dataset.pfIdx, 10);
            const f = window._profileFilings && window._profileFilings[idx];
            if (f) openModal(f);
        });
    });
    // Load more button for paginated profiles
    const loadMoreBtn = document.querySelector('.profile-load-more');
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener('click', async () => {
            const type = loadMoreBtn.dataset.profileType;
            const key = loadMoreBtn.dataset.profileKey;
            const offset = parseInt(loadMoreBtn.dataset.offset, 10);
            const apiBase = type === 'filer' ? '/api/analytics/filer/' : '/api/analytics/company/';
            loadMoreBtn.textContent = '読み込み中...';
            try {
                const resp = await fetch(`${apiBase}${encodeURIComponent(key)}?offset=${offset}`);
                if (!resp.ok) { loadMoreBtn.textContent = '読み込み失敗'; return; }
                const data = await resp.json();
                if (data.recent_filings && data.recent_filings.length > 0) {
                    const container = document.querySelector('.profile-filings');
                    if (container) {
                        const baseIdx = window._profileFilings.length;
                        window._profileFilings.push(...data.recent_filings);
                        for (let i = 0; i < data.recent_filings.length; i++) {
                            const f = data.recent_filings[i];
                            const date = f.submit_date_time ? f.submit_date_time.slice(0, 10) : '-';
                            const desc = f.doc_description || '';
                            const ratio = f.holding_ratio != null ? f.holding_ratio.toFixed(2) + '%' : '-';
                            let changeHtml = '';
                            if (f.ratio_change != null && f.ratio_change !== 0) {
                                const cls = f.ratio_change > 0 ? 'positive' : 'negative';
                                const sign = f.ratio_change > 0 ? '+' : '';
                                changeHtml = `<span class="${cls}">${sign}${f.ratio_change.toFixed(2)}%</span>`;
                            }
                            const row = document.createElement('div');
                            row.className = 'profile-filing-row';
                            row.dataset.pfIdx = baseIdx + i;
                            row.innerHTML = `<span class="profile-filing-date">${escapeHtml(date)}</span><span class="profile-filing-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</span><span class="profile-filing-ratio">${ratio}</span><span class="profile-filing-change">${changeHtml}</span>`;
                            row.addEventListener('click', () => {
                                const idx = baseIdx + i;
                                const filing = window._profileFilings[idx];
                                if (filing) openModal(filing);
                            });
                            container.appendChild(row);
                        }
                    }
                    // Update count display
                    const countEl = document.querySelector('.profile-filing-count');
                    if (countEl) countEl.textContent = `(${window._profileFilings.length}件)`;
                }
                if (data.has_more) {
                    loadMoreBtn.dataset.offset = offset + data.recent_filings.length;
                    loadMoreBtn.textContent = 'さらに読み込む...';
                } else {
                    loadMoreBtn.remove();
                }
            } catch (e) {
                console.error('Load more error:', e);
                loadMoreBtn.textContent = '読み込み失敗';
            }
        });
    }
}

/** Render an SVG timeline chart showing holding ratio changes over time. */
function renderTimelineChart(timeline, mode) {
    // Filter entries with holding_ratio data
    const dataPoints = timeline.filter(t => t.holding_ratio != null);
    if (dataPoints.length < 2) return '';

    const w = 600, h = 180, padL = 50, padR = 20, padT = 20, padB = 35;
    const chartW = w - padL - padR;
    const chartH = h - padT - padB;

    // For company profiles, group by filer and render multiple lines
    if (mode === 'company') {
        const byFiler = {};
        for (const p of dataPoints) {
            const key = p.edinet_code || p.filer_name || 'unknown';
            if (!byFiler[key]) byFiler[key] = { name: p.filer_name || key, points: [] };
            byFiler[key].points.push(p);
        }

        const allRatios = dataPoints.map(p => p.holding_ratio);
        const minR = Math.max(0, Math.min(...allRatios) - 1);
        const maxR = Math.max(...allRatios) + 1;
        const rangeR = maxR - minR || 1;

        const allDates = dataPoints.map(p => new Date(p.date).getTime());
        const minDate = Math.min(...allDates);
        const maxDate = Math.max(...allDates);
        const rangeDate = maxDate - minDate || 1;

        const colors = ['#00d4aa', '#ff6b6b', '#4ecdc4', '#ffe66d', '#a29bfe', '#fd79a8', '#74b9ff', '#ffeaa7'];
        const filerKeys = Object.keys(byFiler);

        let svg = `<div class="profile-section-title">保有割合推移</div>`;
        svg += `<div class="timeline-chart-wrapper"><svg width="100%" viewBox="0 0 ${w} ${h}" class="timeline-chart">`;

        // Grid lines
        const gridSteps = 5;
        for (let i = 0; i <= gridSteps; i++) {
            const y = padT + (i / gridSteps) * chartH;
            const val = (maxR - (i / gridSteps) * rangeR).toFixed(1);
            svg += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="var(--border)" stroke-width="0.5"/>`;
            svg += `<text x="${padL - 5}" y="${y + 4}" text-anchor="end" fill="var(--muted)" font-size="10">${val}%</text>`;
        }

        // Date labels
        const dateLabels = 4;
        for (let i = 0; i <= dateLabels; i++) {
            const x = padL + (i / dateLabels) * chartW;
            const ts = minDate + (i / dateLabels) * rangeDate;
            const d = new Date(ts);
            svg += `<text x="${x}" y="${h - 5}" text-anchor="middle" fill="var(--muted)" font-size="10">${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}</text>`;
        }

        // Plot each filer's line
        filerKeys.forEach((key, fIdx) => {
            const filer = byFiler[key];
            const color = colors[fIdx % colors.length];
            const sorted = filer.points.slice().sort((a, b) => new Date(a.date) - new Date(b.date));
            const pts = sorted.map(p => {
                const x = padL + ((new Date(p.date).getTime() - minDate) / rangeDate) * chartW;
                const y = padT + ((maxR - p.holding_ratio) / rangeR) * chartH;
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
            svg += `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
            // Dots at each data point
            for (const p of sorted) {
                const x = padL + ((new Date(p.date).getTime() - minDate) / rangeDate) * chartW;
                const y = padT + ((maxR - p.holding_ratio) / rangeR) * chartH;
                svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" fill="${color}"><title>${escapeHtml(filer.name)}: ${p.holding_ratio.toFixed(2)}% (${p.date ? p.date.slice(0,10) : ''})</title></circle>`;
            }
        });

        svg += '</svg>';
        // Legend
        if (filerKeys.length > 1) {
            svg += '<div class="timeline-legend">';
            filerKeys.slice(0, 8).forEach((key, i) => {
                const color = colors[i % colors.length];
                const name = byFiler[key].name;
                svg += `<span class="timeline-legend-item"><span class="timeline-legend-dot" style="background:${color}"></span>${escapeHtml(name.length > 20 ? name.slice(0,18) + '…' : name)}</span>`;
            });
            svg += '</div>';
        }
        svg += '</div>';
        return svg;
    }

    // Filer mode: single line chart across all targets
    const allRatios = dataPoints.map(p => p.holding_ratio);
    const minR = Math.max(0, Math.min(...allRatios) - 1);
    const maxR = Math.max(...allRatios) + 1;
    const rangeR = maxR - minR || 1;

    const allDates = dataPoints.map(p => new Date(p.date).getTime());
    const minDate = Math.min(...allDates);
    const maxDate = Math.max(...allDates);
    const rangeDate = maxDate - minDate || 1;

    let svg = `<div class="profile-section-title">保有割合推移</div>`;
    svg += `<div class="timeline-chart-wrapper"><svg width="100%" viewBox="0 0 ${w} ${h}" class="timeline-chart">`;

    // Grid
    const gridSteps = 5;
    for (let i = 0; i <= gridSteps; i++) {
        const y = padT + (i / gridSteps) * chartH;
        const val = (maxR - (i / gridSteps) * rangeR).toFixed(1);
        svg += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="var(--border)" stroke-width="0.5"/>`;
        svg += `<text x="${padL - 5}" y="${y + 4}" text-anchor="end" fill="var(--muted)" font-size="10">${val}%</text>`;
    }
    const dateLabels = 4;
    for (let i = 0; i <= dateLabels; i++) {
        const x = padL + (i / dateLabels) * chartW;
        const ts = minDate + (i / dateLabels) * rangeDate;
        const d = new Date(ts);
        svg += `<text x="${x}" y="${h - 5}" text-anchor="middle" fill="var(--muted)" font-size="10">${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}</text>`;
    }

    // Plot line and dots
    const pts = dataPoints.map(p => {
        const x = padL + ((new Date(p.date).getTime() - minDate) / rangeDate) * chartW;
        const y = padT + ((maxR - p.holding_ratio) / rangeR) * chartH;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const endColor = dataPoints[dataPoints.length - 1].holding_ratio >= dataPoints[0].holding_ratio ? 'var(--green)' : 'var(--red)';
    svg += `<polyline points="${pts}" fill="none" stroke="${endColor}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
    for (const p of dataPoints) {
        const x = padL + ((new Date(p.date).getTime() - minDate) / rangeDate) * chartW;
        const y = padT + ((maxR - p.holding_ratio) / rangeR) * chartH;
        const target = p.target_company_name || '';
        svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${endColor}"><title>${escapeHtml(target)}: ${p.holding_ratio.toFixed(2)}% (${p.date ? p.date.slice(0,10) : ''})</title></circle>`;
    }
    svg += '</svg></div>';
    return svg;
}

/** Render related tender offer filings section. */
function renderRelatedTobs(tobs) {
    let html = '<div class="profile-section-title">関連TOB (公開買付)</div>';
    html += '<div class="profile-tob-list">';
    for (const t of tobs) {
        const date = t.submit_date_time ? t.submit_date_time.slice(0, 10) : '-';
        const tobType = t.tob_type || 'TOB関連';
        const target = t.target_company_name || '';
        const filer = t.filer_name || '';
        html += `<div class="profile-tob-item">
            <span class="profile-tob-date">${escapeHtml(date)}</span>
            <span class="tob-type-badge tob-type-${escapeHtml(t.doc_type_code || '')}">${escapeHtml(tobType)}</span>
            <span class="profile-tob-filer">${escapeHtml(filer)}</span>
            <span class="profile-tob-target">→ ${escapeHtml(target)}</span>
            ${t.pdf_url ? `<a href="${escapeHtml(t.pdf_url)}" class="profile-tob-pdf" target="_blank">PDF</a>` : ''}
        </div>`;
    }
    html += '</div>';
    return html;
}

/** Render company fundamental info panel. */
function renderCompanyInfoPanel(ci) {
    const items = [];
    if (ci.industry) items.push(['業種', ci.industry]);
    if (ci.shares_outstanding != null) items.push(['発行済株式数', ci.shares_outstanding.toLocaleString()]);
    if (ci.net_assets != null) items.push(['純資産', '¥' + ci.net_assets.toLocaleString()]);
    if (ci.bps != null) items.push(['BPS', '¥' + ci.bps.toLocaleString()]);
    if (ci.period_end) items.push(['決算期末', ci.period_end]);
    if (items.length === 0) return '';
    let html = '<div class="profile-company-info">';
    for (const [label, val] of items) {
        html += `<div class="profile-ci-item"><span class="profile-ci-label">${escapeHtml(label)}</span><span class="profile-ci-value">${escapeHtml(String(val))}</span></div>`;
    }
    html += '</div>';
    return html;
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

function playAlertSound(freq = 660) {
    try {
        if (!audioCtx) {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        // C4: Resume if suspended by browser autoplay policy
        if (audioCtx.state === 'suspended') {
            audioCtx.resume();
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

        // Clone market summary
        const summaryContent = document.getElementById('summary-content');
        if (summaryContent) {
            html += '<div class="summary-section"><h3 class="section-title">SUMMARY</h3>';
            html += summaryContent.outerHTML;
            html += '</div>';
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
        const msb = document.getElementById('mobile-btn-sound');
        if (msb) { msb.classList.toggle('active', state.soundEnabled); msb.textContent = state.soundEnabled ? 'サウンド ON' : 'サウンド OFF'; }
        const mnb = document.getElementById('mobile-btn-notify');
        if (mnb) { mnb.classList.toggle('active', state.notificationsEnabled); mnb.textContent = state.notificationsEnabled ? '通知 ON' : '通知 OFF'; }
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
            } else if (panel === 'analytics') {
                closeAllMobileOverlays();
                hideStockView();
                loadAnalytics();
                openMobileOverlay('mobile-analytics-panel');
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

    // Overlay backdrop & close button handlers
    for (const sel of ['.overlay-backdrop', '.overlay-close']) {
        document.querySelectorAll(sel).forEach(el => {
            el.addEventListener('click', () => {
                const overlay = el.closest('.sidebar-overlay');
                if (overlay) closeMobileOverlay(overlay.id);
            });
        });
    }

    // Initialize swipe-to-close on all overlays
    document.querySelectorAll('.sidebar-overlay').forEach(overlayEl => {
        initOverlaySwipe(overlayEl);
    });

    // Mobile settings panel handlers
    const mobileSoundBtn = document.getElementById('mobile-btn-sound');
    if (mobileSoundBtn) mobileSoundBtn.addEventListener('click', toggleSound);

    const mobileNotifyBtn = document.getElementById('mobile-btn-notify');
    if (mobileNotifyBtn) mobileNotifyBtn.addEventListener('click', toggleNotify);

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
                indicator.textContent = '離すと更新';
            } else {
                indicator.textContent = '引っ張って更新...';
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
                indicator.textContent = '更新中...';
            }
            loadInitialData().then(() => {
                if (indicator) {
                    indicator.textContent = '更新完了！';
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
    picker.max = toLocalDateStr(new Date());
    picker.min = EDINET_MIN_DATE;

    picker.addEventListener('change', (e) => {
        state.selectedDate = e.target.value;
        savePreferences();
        loadDateAndAutoFetch();
    });

    prevBtn.addEventListener('click', () => navigateDate(-1));
    nextBtn.addEventListener('click', () => navigateDate(1));

    todayBtn.addEventListener('click', () => {
        state.selectedDate = toLocalDateStr(new Date());
        picker.value = state.selectedDate;
        savePreferences();
        loadDateAndAutoFetch();
    });

    fetchBtn.addEventListener('click', async () => {
        fetchBtn.disabled = true;
        const origText = fetchBtn.textContent;
        fetchBtn.textContent = 'FETCHING...';
        const prevCount = state.filings.length;
        try {
            const resp = await fetch(`/api/poll?date=${state.selectedDate}`, { method: 'POST' });
            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                showToast(body.error || 'データ取得に失敗しました');
                fetchBtn.disabled = false;
                fetchBtn.textContent = origText;
                return;
            }
            // Wait for SSE stats_update (poll complete) or timeout after 60s.
            // Individual filings arrive via SSE new_filing events and are
            // rendered one-by-one by handleNewFiling.
            const fetchDate = state.selectedDate;
            const onComplete = () => {
                const newCount = state.filings.length;
                loadStats();
                loadAnalytics();
                fetchBtn.disabled = false;
                fetchBtn.textContent = newCount > prevCount
                    ? `${newCount - prevCount}件取得`
                    : origText;
                if (newCount > prevCount) {
                    setTimeout(() => { fetchBtn.textContent = origText; }, 3000);
                }
            };
            // Listen for stats_update from SSE
            const statsHandler = (e) => {
                try {
                    const data = JSON.parse(e.data);
                    if (data.date === fetchDate) {
                        clearTimeout(timeout);
                        eventSource.removeEventListener('stats_update', statsHandler);
                        onComplete();
                    }
                } catch (_) { /* ignore parse errors */ }
            };
            // Timeout fallback: if no stats_update arrives within 60s,
            // reload filings and finish.
            const timeout = setTimeout(async () => {
                eventSource.removeEventListener('stats_update', statsHandler);
                await loadFilings();
                onComplete();
            }, 60000);
            if (eventSource && eventSource.readyState !== EventSource.CLOSED) {
                eventSource.addEventListener('stats_update', statsHandler);
            } else {
                // SSE not connected — fall back to polling
                clearTimeout(timeout);
                const checkInterval = setInterval(async () => {
                    await loadFilings();
                    const newCount = state.filings.length;
                    if (newCount !== prevCount) {
                        clearInterval(checkInterval);
                        onComplete();
                    }
                }, 2000);
                setTimeout(() => { clearInterval(checkInterval); onComplete(); }, 60000);
            }
        } catch (e) {
            console.error('Fetch failed:', e);
            showToast('データ取得に失敗しました');
            fetchBtn.disabled = false;
            fetchBtn.textContent = origText;
        }
    });

}

// M10: EDINET API v2 data starts from 2019-03-01
const EDINET_MIN_DATE = '2019-03-01';

function navigateDate(days) {
    const d = new Date(state.selectedDate + 'T00:00:00');
    d.setDate(d.getDate() + days);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    if (d > today) return;
    if (toLocalDateStr(d) < EDINET_MIN_DATE) return;

    state.selectedDate = toLocalDateStr(d);
    document.getElementById('date-picker').value = state.selectedDate;
    savePreferences();
    loadDateAndAutoFetch();
}

// ---------------------------------------------------------------------------
// Auto-fetch: load data for selected date, then auto-trigger EDINET fetch
// if no filings are found (i.e. the date hasn't been fetched yet).
// ---------------------------------------------------------------------------

async function loadDateAndAutoFetch() {
    // Load existing data in parallel
    const [filingsResult] = await Promise.all([
        loadFilings().then(() => state.filings),
        loadStats(),
        loadAnalytics(),
    ]);
    // If no filings exist for this date, auto-trigger EDINET API fetch
    if (state.filings.length === 0) {
        autoFetchForDate(state.selectedDate);
    }
}

async function autoFetchForDate(dateStr) {
    const fetchBtn = document.getElementById('date-fetch');
    if (!fetchBtn || fetchBtn.disabled) return;
    fetchBtn.disabled = true;
    const origText = fetchBtn.textContent;
    fetchBtn.textContent = 'AUTO FETCH...';
    try {
        const resp = await fetch(`/api/poll?date=${dateStr}`, { method: 'POST' });
        if (!resp.ok) {
            fetchBtn.disabled = false;
            fetchBtn.textContent = origText;
            return;
        }
        // Wait for data to arrive via SSE or poll with timeout
        const maxWait = 30000;
        const checkInterval = 2000;
        const start = Date.now();
        const poll = async () => {
            while (Date.now() - start < maxWait) {
                await new Promise(r => setTimeout(r, checkInterval));
                // Check if date is still the selected one (user may have navigated away)
                if (state.selectedDate !== dateStr) break;
                await loadFilings();
                if (state.filings.length > 0) break;
            }
            loadStats();
            loadAnalytics();
            fetchBtn.disabled = false;
            fetchBtn.textContent = state.filings.length > 0
                ? `${state.filings.length}件取得`
                : origText;
            if (state.filings.length > 0) {
                setTimeout(() => { fetchBtn.textContent = origText; }, 3000);
            }
        };
        poll();
    } catch (e) {
        console.warn('Auto-fetch failed:', e);
        fetchBtn.disabled = false;
        fetchBtn.textContent = origText;
    }
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------


async function loadAnalytics() {
    const period = document.getElementById('rankings-period')?.value
        || document.getElementById('mobile-rankings-period')?.value
        || '30d';
    try {
        const [rankingsResp, sectorsResp, movementsResp] = await Promise.all([
            fetch(`/api/analytics/rankings?period=${period}`),
            fetch('/api/analytics/sectors'),
            fetch(`/api/analytics/movements?date=${state.selectedDate}`),
        ]);
        if (!rankingsResp.ok || !sectorsResp.ok || !movementsResp.ok) {
            throw new Error('Analytics API returned non-OK status');
        }
        const rankings = await rankingsResp.json();
        const sectors = await sectorsResp.json();
        const movements = await movementsResp.json();

        renderRankings(rankings, movements);
        renderSectors(sectors);
    } catch (e) {
        console.warn('Analytics load failed:', e);
        // Clear loading indicators so user doesn't see perpetual "読み込み中..."
        for (const id of ['rankings-content', 'mobile-rankings-content']) {
            const el = document.getElementById(id);
            if (el) el.innerHTML = '<div class="summary-empty">分析データなし</div>';
        }
        for (const id of ['sector-content', 'mobile-sector-content']) {
            const el = document.getElementById(id);
            if (el) el.innerHTML = '<div class="summary-empty">セクターデータなし</div>';
        }
    }
}

/** Build a rankings section HTML block */
function rankingSection(title, items, limit, rowFn) {
    if (!items || items.length === 0) return '';
    let html = `<div class="rankings-section"><div class="rankings-section-title">${title}</div>`;
    for (const item of items.slice(0, limit)) html += rowFn(item);
    return html + '</div>';
}

function renderRankings(rankings, movements) {
    const targets = ['rankings-content', 'mobile-rankings-content'];
    for (const targetId of targets) {
        const container = document.getElementById(targetId);
        if (!container) continue;

        let html = '';

        // Market direction indicator
        if (movements && movements.total_filings > 0) {
            const dir = movements.net_direction;
            const dirLabel = dir === 'bullish' ? '買い優勢' : dir === 'bearish' ? '売り優勢' : '中立';
            const dirClass = dir === 'bullish' ? 'positive' : dir === 'bearish' ? 'negative' : 'neutral';
            html += `<div class="rankings-direction">
                <span class="rankings-dir-label">方向性</span>
                <span class="rankings-dir-value ${dirClass}">${dirLabel}</span>
                <span class="rankings-dir-detail">
                    <span class="positive">+${movements.increases}</span> /
                    <span class="negative">-${movements.decreases}</span>
                </span>
            </div>`;
        }

        html += rankingSection('活発な提出者', rankings.most_active_filers, 5, f => {
            const name = f.filer_name || '(不明)';
            return `<div class="filer-row" data-edinet="${escapeHtml(f.edinet_code || '')}">
                <span class="filer-name" title="${escapeHtml(name)}">${filerLinkHtml(name, f.edinet_code)}</span>
                <span class="filer-count">${f.filing_count}</span>
            </div>`;
        });

        html += rankingSection('注目銘柄', rankings.most_targeted_companies, 5, c => {
            const name = c.company_name || '(不明)';
            return `<div class="filer-row">
                <span class="filer-name" title="${escapeHtml(name)}">${companyLinkHtml(name, c.sec_code, true)}</span>
                <span class="filer-count">${c.filing_count}</span>
            </div>`;
        });

        // Largest increases / decreases (shared renderer)
        const renderRatioChange = (items, title, limit) => rankingSection(title, items, limit, f => {
            const name = f.target_company_name || f.filer_name || '?';
            const secCode = f.target_sec_code || f.sec_code;
            const cls = ratioChangeClass(f.ratio_change);
            const change = f.ratio_change != null ? `${f.ratio_change > 0 ? '+' : ''}${f.ratio_change.toFixed(2)}%` : '';
            return `<div class="filer-row">
                <span class="filer-name ${cls}" title="${escapeHtml(name)}">${companyLinkHtml(name, secCode, false)}</span>
                <span class="filer-count ${cls}">${change}</span>
            </div>`;
        });
        html += renderRatioChange(rankings.largest_increases, '最大増加', 3);
        html += renderRatioChange(rankings.largest_decreases, '最大減少', 3);

        html += rankingSection('活発な日', rankings.busiest_days, 5, d =>
            `<div class="filer-row">
                <span class="filer-name">${escapeHtml(d.date || '')}</span>
                <span class="filer-count">${d.filing_count}件</span>
            </div>`
        );

        html += rankingSection('セクター動向', movements?.sector_movements, 5, s => {
            const cls = ratioChangeClass(s.avg_change);
            const avgText = s.avg_change != null ? `${s.avg_change > 0 ? '+' : ''}${s.avg_change.toFixed(2)}%` : '';
            return `<div class="filer-row">
                <span class="filer-name">${escapeHtml(s.sector)} <span class="text-dim">(${s.count}件)</span></span>
                <span class="filer-count ${cls}">${avgText}</span>
            </div>`;
        });

        html += rankingSection('注目変動', movements?.notable_moves, 5, m => {
            const name = m.target_company_name || m.filer_name || '?';
            const cls = ratioChangeClass(m.ratio_change);
            const change = m.ratio_change != null ? `${m.ratio_change > 0 ? '+' : ''}${m.ratio_change.toFixed(2)}%` : '';
            return `<div class="filer-row">
                <span class="filer-name ${cls}" title="${escapeHtml(name)}">${companyLinkHtml(name, m.target_sec_code || m.sec_code, false)}</span>
                <span class="filer-count ${cls}">${change}</span>
            </div>`;
        });

        container.innerHTML = html || '<div class="summary-empty">データなし</div>';
    }
}

function renderSectors(data) {
    const targets = ['sector-content', 'mobile-sector-content'];
    for (const targetId of targets) {
        const container = document.getElementById(targetId);
        if (!container) continue;

        if (!data.sectors || data.sectors.length === 0) {
            container.innerHTML = '<div class="summary-empty">データなし</div>';
            continue;
        }

        const maxCount = Math.max(...data.sectors.map(s => s.filing_count));

        let html = '<div class="sector-list">';
        for (const s of data.sectors.slice(0, 10)) {
            const barWidth = maxCount > 0 ? Math.round((s.filing_count / maxCount) * 100) : 0;
            const avgRatio = s.avg_ratio != null ? `${s.avg_ratio.toFixed(1)}%` : '-';
            html += `<div class="sector-row">
                <div class="sector-info">
                    <span class="sector-name">${escapeHtml(s.sector)}</span>
                    <span class="sector-stats">${s.filing_count}件 / ${s.company_count}社 / avg ${avgRatio}</span>
                </div>
                <div class="sector-bar-bg"><div class="sector-bar" style="width:${barWidth}%"></div></div>
            </div>`;
        }
        html += '</div>';
        container.innerHTML = html;
    }
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

const _escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const _escapeRe = /[&<>"']/g;
function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(_escapeRe, ch => _escapeMap[ch]);
}
