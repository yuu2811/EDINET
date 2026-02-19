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
};

let eventSource = null;
let audioCtx = null;

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
    initSSE();
    initEventListeners();
    loadInitialData();
    initMobileNav();
    initPullToRefresh();
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

    // Watchlist add
    document.getElementById('btn-add-watch').addEventListener('click', () => {
        document.getElementById('watchlist-form').classList.toggle('hidden');
        document.getElementById('watch-name').focus();
    });

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
        const resp = await fetch('/api/filings?limit=200');
        const data = await resp.json();
        state.filings = data.filings || [];
        renderFeed();
        updateTicker();
    } catch (e) {
        console.error('Failed to load filings:', e);
    }
}

async function loadStats() {
    try {
        const resp = await fetch('/api/stats');
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
    // Add to top of list
    state.filings.unshift(filing);

    // Re-render
    renderFeed();
    updateTicker();
    loadStats(); // Refresh stats

    // Flash the new card
    setTimeout(() => {
        const firstCard = document.querySelector('.feed-card');
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

    if (filtered.length === 0) {
        container.innerHTML = `<div class="feed-empty">
            <div class="empty-icon">&#128196;</div>
            <div class="empty-text">報告書が見つかりません</div>
        </div>`;
        return;
    }

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

function createFeedCard(f) {
    const isChange = f.doc_description && f.doc_description.includes('変更');
    const cardClass = f.is_amendment ? 'amendment' : isChange ? 'change-report' : 'new-report';

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
    const target = f.target_company_name || '(対象不明)';
    const targetCode = f.target_sec_code ? `[${f.target_sec_code}]` : '';

    // Ratio with change indicator
    let ratioHtml = '';
    if (f.holding_ratio != null) {
        const ratioClass = f.ratio_change > 0 ? 'positive' : f.ratio_change < 0 ? 'negative' : 'neutral';
        let changeStr = '';
        if (f.ratio_change != null) {
            const arrow = f.ratio_change > 0 ? '▲' : '▼';
            changeStr = `<span class="card-ratio-change">${arrow}${Math.abs(f.ratio_change).toFixed(2)}%</span>`;
        }
        ratioHtml = `<span class="card-ratio ${ratioClass}">${f.holding_ratio.toFixed(2)}% ${changeStr}</span>`;
    } else {
        ratioHtml = '<span class="card-ratio neutral">-</span>';
    }

    // Links
    let links = '';
    if (f.pdf_url) {
        links += `<a href="${f.pdf_url}" target="_blank" rel="noopener" class="card-link" onclick="event.stopPropagation()">PDF</a>`;
    }
    if (f.edinet_url) {
        links += `<a href="${f.edinet_url}" target="_blank" rel="noopener" class="card-link" onclick="event.stopPropagation()">EDINET</a>`;
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
            </div>
            <div class="card-bottom">
                ${ratioHtml}
                <div class="card-links">${links}</div>
            </div>
        </div>
    `;
}

function renderStats() {
    const s = state.stats;
    document.getElementById('stat-total').textContent = s.today_total ?? '-';
    document.getElementById('stat-new').textContent = s.today_new_reports ?? '-';
    document.getElementById('stat-amendments').textContent = s.today_amendments ?? '-';
    document.getElementById('stat-clients').textContent = s.connected_clients ?? '-';

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

    container.innerHTML = state.watchlist.map(w =>
        `<div class="watch-item">
            <div class="watch-info">
                <span class="watch-name">${escapeHtml(w.company_name)}</span>
                <span class="watch-code">${escapeHtml(w.sec_code || '')}</span>
            </div>
            <button class="watch-delete" data-id="${w.id}"
                    aria-label="削除: ${escapeHtml(w.company_name)}"
                    title="削除">&times;</button>
        </div>`
    ).join('');

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

    // Re-render feed to update watchlist badges
    renderFeed();
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
// Modal
// ---------------------------------------------------------------------------

function openModal(filing) {
    const body = document.getElementById('modal-body');

    const rows = [
        ['書類ID', filing.doc_id],
        ['書類種別', filing.doc_description || '-'],
        ['提出日時', filing.submit_date_time || '-'],
        ['提出者', filing.filer_name || '-'],
        ['EDINET コード', filing.edinet_code || '-'],
        ['対象会社', filing.target_company_name || '-'],
        ['対象証券コード', filing.target_sec_code || '-'],
    ];

    // Ratio section
    if (filing.holding_ratio != null) {
        const ratioClass = filing.ratio_change > 0 ? 'positive' : filing.ratio_change < 0 ? 'negative' : '';
        rows.push(['保有割合', {
            html: `<span class="detail-value large ${ratioClass}">${filing.holding_ratio.toFixed(2)}%</span>`,
        }]);
    }
    if (filing.previous_holding_ratio != null) {
        rows.push(['前回保有割合', `${filing.previous_holding_ratio.toFixed(2)}%`]);
    }
    if (filing.ratio_change != null) {
        const cls = filing.ratio_change > 0 ? 'positive' : filing.ratio_change < 0 ? 'negative' : '';
        const arrow = filing.ratio_change > 0 ? '▲' : '▼';
        rows.push(['変動', {
            html: `<span class="detail-value ${cls}">${arrow}${Math.abs(filing.ratio_change).toFixed(2)}%</span>`,
        }]);
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

    body.innerHTML = rows.map(([label, value]) => {
        const valHtml = typeof value === 'object' && value.html
            ? value.html
            : `<span class="detail-value">${escapeHtml(String(value))}</span>`;
        return `<div class="detail-row">
            <span class="detail-label">${escapeHtml(label)}</span>
            ${valHtml}
        </div>`;
    }).join('');

    document.getElementById('detail-modal').classList.remove('hidden');
}

function closeModal() {
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
    // Trigger animation
    requestAnimationFrame(() => {
        const sheet = panel.querySelector('.overlay-sheet');
        if (sheet) sheet.classList.add('sheet-visible');
    });
    // Sync content
    syncMobilePanel(panelId);
}

function closeMobileOverlay(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const sheet = panel.querySelector('.overlay-sheet');
    if (sheet) {
        sheet.classList.remove('sheet-visible');
        sheet.addEventListener('transitionend', () => {
            panel.classList.add('hidden');
        }, { once: true });
    } else {
        panel.classList.add('hidden');
    }
    // Reset nav active state to feed
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.toggle('active', item.dataset.panel === 'feed');
    });
}

function closeAllMobileOverlays() {
    document.querySelectorAll('.sidebar-overlay').forEach(panel => {
        if (!panel.classList.contains('hidden')) {
            const sheet = panel.querySelector('.overlay-sheet');
            if (sheet) sheet.classList.remove('sheet-visible');
            panel.classList.add('hidden');
        }
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
            } else if (panel === 'stats') {
                closeAllMobileOverlays();
                openMobileOverlay('mobile-stats-panel');
            } else if (panel === 'watchlist') {
                closeAllMobileOverlays();
                openMobileOverlay('mobile-watchlist-panel');
            } else if (panel === 'settings') {
                closeAllMobileOverlays();
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
                indicator.style.cssText = 'text-align:center;padding:10px;color:#00ff88;font-size:12px;font-family:monospace;';
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
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
