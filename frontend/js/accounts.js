/**
 * AccountsView отвечает за отрисовку списка аккаунтов, статистики,
 * поиск и действия «Проверить» / «Удалить».
 */
class AccountsView {
  static COLORS = ['#5aaec6', '#5aad78', '#c9a45a', '#8b7cc9', '#d07070', '#5ab8a8'];

  constructor({ api, gridEl, searchEl, statTotalEl, statActiveEl, statInactiveEl, statAgentsEl, toast, onStartAgent, onHistory }) {
    this.api = api;
    this.gridEl = gridEl;
    this.searchEl = searchEl;
    this.statTotalEl = statTotalEl;
    this.statActiveEl = statActiveEl;
    this.statInactiveEl = statInactiveEl;
    this.statAgentsEl = statAgentsEl;
    this.toast = toast;
    this.onStartAgent = onStartAgent;
    this.onHistory = onHistory;

    this.accounts = [];

    this.searchEl.addEventListener('input', () => this._render());
    this.gridEl.addEventListener('click', (e) => this._onGridClick(e));
  }

  async load() {
    this.accounts = await this.api.listAccounts();
    this._render();
  }

  async refresh() {
    return this.load();
  }

  async checkAccount(id) {
    const acc = this.accounts.find((a) => a.id === id);
    if (acc) {
      acc.status = 'checking';
      this._render();
    }
    try {
      const updated = await this.api.checkAccount(id);
      this._replace(updated);
      this.toast(
        updated.status === 'active' ? 'ok' : 'err',
        updated.status === 'active'
          ? `${updated.name}: сессия активна`
          : `${updated.name}: сессия недействительна`
      );
    } catch (e) {
      this.toast('err', e.message);
      await this.load();
    }
  }

  async deleteAccount(id) {
    const acc = this.accounts.find((a) => a.id === id);
    if (!acc) return;
    const ok = confirm(
      `Удалить аккаунт ${acc.phone}? Сессия будет уничтожена, повторный вход потребует новой авторизации.`
    );
    if (!ok) return;
    try {
      await this.api.deleteAccount(id);
      this.accounts = this.accounts.filter((a) => a.id !== id);
      this._render();
      this.toast('ok', 'Аккаунт и сессия удалены');
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  // ---------- внутреннее ----------

  _replace(updated) {
    this.accounts = this.accounts.map((a) => (a.id === updated.id ? updated : a));
    this._render();
  }

  _onGridClick(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const id = btn.closest('[data-id]').dataset.id;
    const acc = this.accounts.find((a) => a.id === id);
    const accName = acc ? `${acc.name} ${acc.phone}` : id;
    if (btn.dataset.action === 'check') this.checkAccount(id);
    if (btn.dataset.action === 'delete') this.deleteAccount(id);
    if (btn.dataset.action === 'agent-start' && this.onStartAgent) this.onStartAgent(id);
    if (btn.dataset.action === 'agent-stop') this.stopAgent(id);
    if (btn.dataset.action === 'history' && this.onHistory) this.onHistory(id, accName);
  }

  async stopAgent(id) {
    try {
      await this.api.stopAgent(id);
      this.toast('ok', 'ИИ-агент остановлен');
      await this.load();
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  static _initials(name) {
    return name.trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
  }

  static _colorFor(str) {
    let h = 0;
    for (const c of str) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return AccountsView.COLORS[h % AccountsView.COLORS.length];
  }

  static _escape(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  static _agentLine(agent) {
    if (!agent) return '';
    if (agent.running) {
      const line = {
        starting: 'ИИ запускается',
        loading_model: 'Загружается модель',
        connecting: 'ИИ подключается к Telegram',
        generating: 'ИИ пишет ответ',
        running: 'ИИ слушает чаты',
      }[agent.status] || 'ИИ работает';
      const err = agent.last_error
        ? ` · ${AccountsView._escape(String(agent.last_error).slice(0, 80))}`
        : '';
      return `<span class="agent-log">${line}${err}</span>`;
    }
    if (agent.status === 'error') {
      return `<span class="agent-log error">${AccountsView._escape(agent.last_error || 'ошибка агента')}</span>`;
    }
    return '';
  }

  static _statsBlock(agent) {
    const running = !!(agent && agent.running);
    const received = Number(agent && agent.received) || 0;
    const processed = Number(agent && agent.processed) || 0;
    const replies = Number(agent && agent.replies) || 0;
    const pending = agent && agent.pending != null
      ? Number(agent.pending) || 0
      : Math.max(0, received - processed);
    const scriptCls = running ? 'on' : 'off';
    const scriptLabel = running ? 'активен' : 'неактивен';
    return `
      <div class="account-stats">
        <div class="account-stat"><span>Скрипт</span><b class="${scriptCls}">${scriptLabel}</b></div>
        <div class="account-stat"><span>Получено сообщений</span><b>${received}</b></div>
        <div class="account-stat"><span>Обработано сообщений</span><b>${processed}</b></div>
        <div class="account-stat"><span>Отвечено</span><b>${replies}</b></div>
        <div class="account-stat"><span>Ожидают ответа</span><b>${pending}</b></div>
      </div>`;
  }

  static _folderBlock(account) {
    const folder = account && account.reply_folder;
    if (!folder || !folder.enabled) return '';
    const title = folder.title || 'TG-Assistant';
    const limit = Number(folder.limit) || 0;
    const chats = Array.isArray(folder.chats) ? folder.chats : [];
    const used = chats.length;
    const countLabel = limit ? `${used} / ${limit}` : String(used);
    const hint = folder.hint
      || `В Telegram откройте этот аккаунт и перетащите личные диалоги в папку «${title}». Бот ответит только им.`;
    const list = chats.length
      ? `<ul class="folder-chats">${chats.map((c) => `<li>${AccountsView._escape(c.name || c.id)}</li>`).join('')}</ul>`
      : '<p class="folder-empty">Пока пусто — добавьте чаты в Telegram</p>';
    return `
      <div class="folder-box">
        <div class="folder-head">
          <span>Папка «${AccountsView._escape(title)}»</span>
          <b>${countLabel}</b>
        </div>
        <p class="folder-hint">${AccountsView._escape(hint)}</p>
        ${list}
      </div>`;
  }

  static _statusMeta(status) {
    if (status === 'active') return { label: 'Активен', cls: 'active' };
    if (status === 'checking') return { label: 'Проверка…', cls: 'checking' };
    return { label: 'Неактивен', cls: 'inactive' };
  }

  static _timeAgo(iso) {
    const diffMs = Date.now() - new Date(iso).getTime();
    const min = Math.floor(diffMs / 60000);
    if (min < 1) return 'только что';
    if (min < 60) return `${min} мин назад`;
    const h = Math.floor(min / 60);
    if (h < 24) return `${h} ч назад`;
    return `${Math.floor(h / 24)} дн назад`;
  }

  _render() {
    const q = this.searchEl.value.trim().toLowerCase();
    const list = this.accounts.filter(
      (a) => !q || a.phone.toLowerCase().includes(q) || a.name.toLowerCase().includes(q)
    );

    this.statTotalEl.textContent = this.accounts.length;
    this.statActiveEl.textContent = this.accounts.filter((a) => a.status === 'active').length;
    this.statInactiveEl.textContent = this.accounts.filter((a) => a.status === 'inactive').length;
    if (this.statAgentsEl) {
      this.statAgentsEl.textContent = this.accounts.filter((a) => a.agent && a.agent.running).length;
    }

    if (list.length === 0) {
      this.gridEl.innerHTML = `
        <div class="empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
          <h3>${this.accounts.length === 0 ? 'Пока нет аккаунтов' : 'Ничего не найдено'}</h3>
          <p>${
            this.accounts.length === 0
              ? 'Добавьте первый Telegram-аккаунт, авторизовавшись по номеру телефона.'
              : 'Попробуйте изменить поисковый запрос.'
          }</p>
          ${this.accounts.length === 0 ? '<button class="btn btn-primary" id="emptyAddBtn">Добавить аккаунт</button>' : ''}
        </div>`;
      const emptyBtn = this.gridEl.querySelector('#emptyAddBtn');
      if (emptyBtn) emptyBtn.addEventListener('click', () => document.dispatchEvent(new CustomEvent('open-add-modal')));
      return;
    }

    this.gridEl.innerHTML = list
      .map((a) => {
        const s = AccountsView._statusMeta(a.status);
        const c = AccountsView._colorFor(a.phone);
        const agent = a.agent || {};
        const agentOn = !!agent.running;
        const lastCheck = a.last_check;
        const agentBtn = agentOn
          ? `<button class="btn btn-danger-ghost btn-sm" data-action="agent-stop">Стоп ИИ</button>`
          : `<button class="btn btn-ai btn-sm" data-action="agent-start">ИИ-агент</button>`;
        const agentLine = AccountsView._agentLine(agent);
        const stats = AccountsView._statsBlock(agent);
        const folder = AccountsView._folderBlock(a);
        const typing = (agent.typing_text || '').trim();
        const live = (agentOn && agent.status === 'generating')
          ? `<div class="card-meta admin-only"><span class="agent-log">Набирает: ${AccountsView._escape(typing || '…')}</span></div>`
          : '';
        return `
        <div class="card" data-id="${a.id}">
          <div class="card-top">
            <div class="avatar" style="background:${c}">${AccountsView._escape(AccountsView._initials(a.name))}</div>
            <div class="card-id">
              <div class="card-name">${AccountsView._escape(a.name)}</div>
              <div class="card-phone">${AccountsView._escape(a.phone)}</div>
            </div>
            <span class="badge ${s.cls}"><span class="dot"></span>${s.label}</span>
          </div>
          <div class="card-meta">
            <span>Проверка: <b>${AccountsView._timeAgo(lastCheck)}</b></span>
            <span style="font-family:var(--font-mono)">${AccountsView._escape(String(a.id).slice(0, 8))}</span>
          </div>
          ${agentLine ? `<div class="card-meta">${agentLine}</div>` : ''}
          ${live}
          ${folder}
          ${stats}
          <div class="card-actions">
            ${agentBtn}
            <button class="btn btn-ghost btn-sm admin-only" data-action="history">История диалога</button>
            <button class="btn btn-ghost btn-sm" data-action="check">Проверить</button>
            <button class="btn btn-danger-ghost btn-sm" data-action="delete">Удалить</button>
          </div>
        </div>`;
      })
      .join('');
  }
}
