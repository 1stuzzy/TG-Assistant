/**
 * Логи действий модели: промт, набор, отправка в чат.
 */
class ModelLogs {
  constructor(apiBase) {
    this.apiBase = apiBase.replace(/\/$/, '');
    this.overlay = document.getElementById('modelLogsOverlay');
    this.body = document.getElementById('modelLogsBody');
    this.filter = document.getElementById('modelLogsFilter');
    this.ws = null;
    document.getElementById('modelLogsBtn')?.addEventListener('click', () => this.open());
    document.getElementById('modelLogsCloseBtn')?.addEventListener('click', () => this.close());
    document.getElementById('modelLogsClearBtn')?.addEventListener('click', () => { if (this.body) this.body.innerHTML = ''; });
    this.filter?.addEventListener('input', () => this._applyFilter());
    this.overlay?.addEventListener('click', (e) => { if (e.target === this.overlay) this.close(); });
  }

  open() {
    this.overlay?.classList.add('open');
    if (this.body) this.body.innerHTML = '';
    this._connect();
  }

  close() {
    this.overlay?.classList.remove('open');
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  _connect() {
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    const wsUrl = this.apiBase.replace(/^http/, 'ws') + '/api/model-logs/ws';
    this._line({ ts: '', kind: 'load', label: 'Система', detail: 'Подключение: ' + wsUrl, peer: '' });
    try {
      this.ws = new WebSocket(wsUrl);
    } catch (e) {
      this._line({ kind: 'error', label: 'Ошибка', detail: e.message });
      return;
    }
    this.ws.onmessage = (ev) => {
      try {
        this._line(JSON.parse(ev.data));
      } catch (_) {
        this._line({ kind: 'error', label: 'raw', detail: ev.data });
      }
    };
    this.ws.onerror = () => this._line({ kind: 'error', label: 'Ошибка', detail: 'WebSocket логов модели' });
  }

  _applyFilter() {
    const q = (this.filter?.value || '').trim().toLowerCase();
    this.body?.querySelectorAll('.mlog-line').forEach((el) => {
      el.hidden = Boolean(q) && !(el.dataset.hay || '').includes(q);
    });
  }

  _line(row) {
    if (!this.body) return;
    const el = document.createElement('div');
    el.className = 'mlog-line kind-' + (row.kind || 'info');
    const peer = row.peer ? ' · ' + row.peer : '';
    const acc = row.account_id && row.account_id !== 'playground' ? row.account_id.slice(0, 8) : (row.account_id || '');
    el.dataset.hay = `${row.kind || ''} ${row.label || ''} ${row.peer || ''} ${row.detail || ''} ${acc}`.toLowerCase();
    el.innerHTML = `<span class="c-ts">${row.ts || ''}</span>
      <span class="mlog-kind">${row.label || row.kind || ''}</span>
      <span class="mlog-peer">${acc}${peer}</span>
      <span class="c-msg"></span>`;
    el.querySelector('.c-msg').textContent = row.detail || '';
    this.body.appendChild(el);
    this.body.scrollTop = this.body.scrollHeight;
    this._applyFilter();
  }
}
