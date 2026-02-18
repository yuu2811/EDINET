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
// Initialization
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initSSE();
    initEventListeners();
    loadInitialData();
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
    });

    // Feed filter
    document.getElementById('feed-filter').addEventListener('change', (e) => {
        state.filterMode = e.target.value;
        renderFeed();
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
    document.querySelector('.modal-close').addEventListener('click', closeModal);
    document.querySelector('.modal-overlay').addEventListener('click', closeModal);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });
}

// ---------------------------------------------------------------------------
// SSE Connection
// ---------------------------------------------------------------------------

function initSSE() {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource('/api/stream');

    eventSource.addEventListener('connected', () => {
        setConnectionStatus(true);
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
        setConnectionStatus(true);
    };

    eventSource.onerror = () => {
        setConnectionStatus(false);
        // EventSource auto-reconnects
    };
}

function setConnectionStatus(connected) {
    state.connected = connected;
    const el = document.getElementById('connection-status');
    el.className = `status ${connected ? 'connected' : 'disconnected'}`;
    el.querySelector('.status-text').textContent = connected ? '接続中' : '切断';
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
        if (firstCard) firstCard.classList.add('flash');
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
        container.innerHTML = '<div class="feed-empty">該当する報告書はありません</div>';
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

    // Time
    const time = f.submit_date_time
        ? f.submit_date_time.split(' ').pop() || f.submit_date_time
        : '-';

    // Filer / Target
    const filer = f.holder_name || f.filer_name || '(不明)';
    const target = f.target_company_name || '(対象不明)';
    const targetCode = f.target_sec_code ? `[${f.target_sec_code}]` : '';

    // Ratio
    let ratioHtml = '';
    if (f.holding_ratio != null) {
        const ratioClass = f.ratio_change > 0 ? 'positive' : f.ratio_change < 0 ? 'negative' : 'neutral';
        const changeStr = f.ratio_change != null
            ? `<span class="card-ratio-change">(${f.ratio_change > 0 ? '+' : ''}${f.ratio_change.toFixed(2)}%)</span>`
            : '';
        ratioHtml = `<span class="card-ratio ${ratioClass}">${f.holding_ratio.toFixed(2)}% ${changeStr}</span>`;
    } else {
        ratioHtml = '<span class="card-ratio neutral">-</span>';
    }

    // Links
    let links = '';
    if (f.pdf_url) {
        links += `<a href="${f.pdf_url}" target="_blank" class="card-link" onclick="event.stopPropagation()">PDF</a>`;
    }
    if (f.edinet_url) {
        links += `<a href="${f.edinet_url}" target="_blank" class="card-link" onclick="event.stopPropagation()">EDINET</a>`;
    }

    return `
        <div class="feed-card ${cardClass}" data-doc-id="${f.doc_id}">
            <div class="card-top">
                <span class="card-time">${escapeHtml(time)}</span>
                <div>${badge}</div>
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
        container.innerHTML = '<div class="watchlist-empty">ウォッチリストは空です</div>';
        return;
    }

    container.innerHTML = state.watchlist.map(w =>
        `<div class="watch-item">
            <div class="watch-info">
                <span class="watch-name">${escapeHtml(w.company_name)}</span>
                <span class="watch-code">${escapeHtml(w.sec_code || '')}</span>
            </div>
            <button class="watch-delete" data-id="${w.id}" title="削除">&times;</button>
        </div>`
    ).join('');

    // Delete handlers
    container.querySelectorAll('.watch-delete').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const id = e.target.dataset.id;
            await deleteWatchItem(id);
        });
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
    for (const w of state.watchlist) {
        const match =
            (w.sec_code && (filing.sec_code === w.sec_code || filing.target_sec_code === w.sec_code)) ||
            (w.company_name && (
                (filing.target_company_name || '').includes(w.company_name) ||
                (filing.filer_name || '').includes(w.company_name)
            ));

        if (match) {
            // Extra notification for watchlist match
            if (state.notificationsEnabled) {
                new Notification(`WATCHLIST: ${w.company_name}`, {
                    body: `${filing.filer_name || ''} - ${filing.doc_description || ''}`,
                    icon: '📊',
                    tag: `watchlist-${filing.doc_id}`,
                });
            }
            if (state.soundEnabled) {
                // Play a different (higher) alert for watchlist
                playAlertSound(880);
            }
            break;
        }
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
        rows.push(['変動', {
            html: `<span class="detail-value ${cls}">${filing.ratio_change > 0 ? '+' : ''}${filing.ratio_change.toFixed(2)}%</span>`,
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
        links.push(`<a href="${filing.pdf_url}" target="_blank">PDF ダウンロード</a>`);
    }
    if (filing.edinet_url) {
        links.push(`<a href="${filing.edinet_url}" target="_blank">EDINET で閲覧</a>`);
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
// Utilities
// ---------------------------------------------------------------------------

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
