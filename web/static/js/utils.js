/**
 * Общие JS-утилиты: API-клиент, toast уведомления, хелперы.
 */

// ─── Toast notifications ───
const toastContainer = document.createElement('div');
toastContainer.className = 'toast-container';
document.body.appendChild(toastContainer);

function showToast(message, type = 'success') {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span class="toast-text">${message}</span>
  `;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ─── API client ───
const api = {
  async request(method, path, body = null) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
    };
    if (body !== null) opts.body = JSON.stringify(body);

    const res = await fetch(`/api${path}`, opts);
    if (res.status === 303 || res.status === 401) {
      window.location.href = '/';
      return;
    }
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || data.message || 'Ошибка сервера');
    }
    return data;
  },
  get: (path) => api.request('GET', path),
  post: (path, body) => api.request('POST', path, body),
  put: (path, body) => api.request('PUT', path, body),
  patch: (path, body) => api.request('PATCH', path, body),
  delete: (path) => api.request('DELETE', path),
};

// ─── Formatters ───
function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function formatDuration(seconds) {
  if (!seconds) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d} дн.`;
  if (h > 0) return `${h} ч.`;
  return `${m} мин.`;
}

const punishmentLabels = {
  'ban_role':   '🔨 Бан (роль)',
  'mute_voice': '🔇 Мут голос',
  'mute_text':  '✏️ Мут текст',
  'warn':       '⚠️ Варн',
  'warning':    '🔔 Предупреждение',
};

const reportStatusLabels = {
  'open':     '🟡 Открыта',
  'accepted': '🟢 В работе',
  'rejected': '🔴 Отклонена',
  'closed':   '⚫ Закрыта',
};

// ─── Pagination helper ───
function renderPagination(container, currentPage, total, perPage, onPageChange) {
  const totalPages = Math.ceil(total / perPage);
  if (totalPages <= 1) { container.innerHTML = ''; return; }

  let html = `<button class="page-btn" onclick="(${onPageChange.toString()})(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}>‹</button>`;
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - currentPage) <= 2) {
      html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="(${onPageChange.toString()})(${i})">${i}</button>`;
    } else if (Math.abs(i - currentPage) === 3) {
      html += `<span style="color:var(--text-muted);padding:0 4px;">…</span>`;
    }
  }
  html += `<button class="page-btn" onclick="(${onPageChange.toString()})(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>›</button>`;
  container.innerHTML = html;
}

// ─── Loading state ───
function setLoading(container, text = 'Загрузка...') {
  container.innerHTML = `
    <div class="loading-overlay">
      <div class="spinner"></div>
      <span>${text}</span>
    </div>
  `;
}

// ─── Mobile sidebar toggle ───
document.addEventListener('DOMContentLoaded', () => {
  const menuBtn = document.getElementById('menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  if (menuBtn && sidebar) {
    menuBtn.addEventListener('click', () => {
      sidebar.classList.toggle('open');
    });
  }

  // Active nav highlighting
  const current = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(link => {
    if (link.getAttribute('href') === current) link.classList.add('active');
  });
});
