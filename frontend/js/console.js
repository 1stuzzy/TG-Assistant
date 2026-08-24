/**
 * Консоль разработчика: живой поток логов backend через WebSocket.
 */
class DevConsole {
  constructor(apiBase) {
    this.apiBase = apiBase.replace(/\/$/, '');
    this.overlay = document.getElementById('consoleOverlay');
    this.body = document.getElementById('consoleLog');
    this.ws = null;
    this.autoScroll = true;
    document.getElementById('consoleBtn')?.addEventListener('click', () => this.open());
    document.getElementById('consoleCloseBtn')?.addEventListener('click', () => this.close());
    document.getElementById('consoleClearBtn')?.addEventListener('click', () => { if (this.body) this.body.innerHTML = ''; });
    this.overlay?.addEventListener('click', (e) => { if (e.target === this.overlay) this.close(); });
  }

  open() {
    this.overlay?.classList.add('open');
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
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    const wsUrl = this.apiBase.replace(/^http/, 'ws') + '/api/logs/ws';
    this._line('info', 'system', 'Подключение к логам: ' + wsUrl);
    try {
      this.ws = new WebSocket(wsUrl);
    } catch (e) {
      this._line('error', 'system', e.message);
      return;
    }
    this.ws.onmessage = (ev) => {
      try {
        const row = JSON.parse(ev.data);
        this._line(row.level, row.logger, row.message, row.ts);
      } catch (_) {
        this._line('info', 'raw', ev.data);
      }
    };
    this.ws.onerror = () => this._line('error', 'system', 'Ошибка WebSocket логов');
    this.ws.onclose = () => {
      if (this.overlay?.classList.contains('open')) {
        this._line('warning', 'system', 'Соединение с логами закрыто');
      }
    };
  }

  _line(level, logger, message, ts) {
    if (!this.body) return;
    const el = document.createElement('div');
    el.className = 'console-line ' + String(level || 'info').toLowerCase();
    el.innerHTML = `<span class="c-ts">${ts || ''}</span><span class="c-lvl">${level || ''}</span><span class="c-lg">${logger || ''}</span><span class="c-msg"></span>`;
    el.querySelector('.c-msg').textContent = message || '';
    this.body.appendChild(el);
    if (this.autoScroll) this.body.scrollTop = this.body.scrollHeight;
  }
}
