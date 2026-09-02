/**
 * Выгрузка живого Telegram-чата в примеры стиля для модели.
 * Только администратор: Настройки → Telegram → Выгрузить чат.
 */
class HarvestDialog {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.overlay = document.getElementById('harvestOverlay');
    this.titleEl = document.getElementById('harvestTitle');
    this.subEl = document.getElementById('harvestSub');
    this.errEl = document.getElementById('errHarvest');
    this.bodyEl = document.getElementById('harvestBody');
    this.searchWrap = document.getElementById('harvestSearchWrap');
    this.searchEl = document.getElementById('harvestSearch');
    this.footerEl = document.getElementById('harvestFooter');
    this.backBtn = document.getElementById('harvestBackBtn');
    this.closeBtn = document.getElementById('harvestCloseBtn');

    this.accounts = [];
    this.folders = [];
    this.chats = [];
    this.account = null;
    this.folder = null;
    this.step = 'accounts';

    if (!this.overlay || !this.bodyEl) return;
    if (this.closeBtn) this.closeBtn.addEventListener('click', () => this.close());
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });
    if (this.backBtn) this.backBtn.addEventListener('click', () => this._back());
    if (this.searchEl) {
      this.searchEl.addEventListener('input', () => {
        if (this.step === 'folders') this._renderFolders();
        else if (this.step === 'chats') this._renderChats();
      });
    }
    this.bodyEl.addEventListener('click', (e) => this._onBodyClick(e));
  }

  async open() {
    if (!this.overlay || !this.bodyEl) return;
    this.account = null;
    this.folder = null;
    this.folders = [];
    this.chats = [];
    if (this.searchEl) this.searchEl.value = '';
    this._clearErr();
    this.overlay.classList.add('open');
    this._busy('Загружаю аккаунты…');
    try {
      this.accounts = await this.api.listAccounts();
    } catch (e) {
      this._error(e.message);
      this._setStep('accounts');
      this.bodyEl.innerHTML = '';
      return;
    }
    if (!this.accounts.length) {
      this._setStep('accounts');
      this.bodyEl.innerHTML = '<p class="harvest-empty">Сначала подключите Telegram-аккаунт на вкладке «Аккаунты».</p>';
      return;
    }
    if (this.accounts.length === 1) {
      await this._openAccount(this.accounts[0]);
      return;
    }
    this._renderAccounts();
  }

  close() {
    this.overlay.classList.remove('open');
  }

  _setStep(step) {
    this.step = step;
    const searchable = step === 'folders' || step === 'chats';
    if (this.searchWrap) this.searchWrap.hidden = !searchable;
    if (this.footerEl) this.footerEl.hidden = step === 'accounts' || step === 'done';
    this.titleEl.textContent = 'Выгрузить чат';
    if (step === 'accounts') {
      this.subEl.textContent = 'С какого аккаунта взять диалоги.';
    } else if (step === 'folders') {
      const who = this.account ? this.account.name || this.account.phone : '';
      this.subEl.textContent = who
        ? `Аккаунт ${who}. Выберите папку — например «Парсинг».`
        : 'Выберите папку на аккаунте.';
    } else if (step === 'chats') {
      const folder = this.folder ? this.folder.title : 'папка';
      this.subEl.textContent = `Папка «${folder}». Нажмите чат — выгрузится весь текст, без фото и аудио.`;
    }
  }

  _busy(text) {
    if (this.searchWrap) this.searchWrap.hidden = true;
    if (this.footerEl) this.footerEl.hidden = true;
    this.bodyEl.innerHTML = `<p class="harvest-empty">${HarvestDialog._escape(text)}</p>`;
  }

  _clearErr() {
    this.errEl.classList.remove('show');
    this.errEl.textContent = '';
  }

  _error(msg) {
    this.errEl.textContent = msg;
    this.errEl.classList.add('show');
  }

  _back() {
    this._clearErr();
    if (this.searchEl) this.searchEl.value = '';
    if (this.step === 'chats' || this.step === 'done') {
      this.folder = null;
      this.chats = [];
      this._renderFolders();
      return;
    }
    if (this.step === 'folders') {
      this.account = null;
      this.folders = [];
      if (this.accounts.length <= 1) {
        this.close();
        return;
      }
      this._renderAccounts();
    }
  }

  _renderAccounts() {
    this._setStep('accounts');
    this.bodyEl.innerHTML = `<div class="harvest-list">${this.accounts.map((a) => {
      const status = a.status === 'active' ? 'активен' : 'неактивен';
      return `<button type="button" class="harvest-row" data-account="${HarvestDialog._escape(a.id)}">
        <span class="harvest-row-main">
          <b>${HarvestDialog._escape(a.name || a.phone)}</b>
          <span class="meta">${HarvestDialog._escape(a.phone)} · ${status}</span>
        </span>
      </button>`;
    }).join('')}</div>`;
  }

  async _openAccount(account) {
    this.account = account;
    this.folder = null;
    this._clearErr();
    this._busy('Загружаю папки…');
    try {
      const data = await this.api.listAccountFolders(account.id);
      this.folders = data.folders || [];
    } catch (e) {
      this._error(e.message);
      if (this.accounts.length > 1) this._renderAccounts();
      else this.bodyEl.innerHTML = '';
      return;
    }
    const preferred = this.folders.filter((f) => f.preferred);
    if (preferred.length === 1) {
      await this._openFolder(preferred[0]);
      return;
    }
    this._renderFolders();
  }

  _renderFolders() {
    this._setStep('folders');
    const q = (this.searchEl && this.searchEl.value || '').trim().toLowerCase();
    const list = this.folders.filter((f) => !q || String(f.title || '').toLowerCase().includes(q));
    if (!this.folders.length) {
      this.bodyEl.innerHTML = '<p class="harvest-empty">Папок не видно. Создайте в Telegram папку «Парсинг» и перетащите туда лички.</p>';
      return;
    }
    if (!list.length) {
      this.bodyEl.innerHTML = '<p class="harvest-empty">Ничего не найдено.</p>';
      return;
    }
    this.bodyEl.innerHTML = `<div class="harvest-list">${list.map((f) => {
      const count = f.chats == null ? 'все лички' : `${f.chats} чат.`;
      const mark = f.preferred ? ' · для парсинга' : '';
      return `<button type="button" class="harvest-row" data-folder="${HarvestDialog._escape(f.id)}">
        <span class="harvest-row-main">
          <b>${HarvestDialog._escape(f.title)}</b>
          <span class="meta">${HarvestDialog._escape(count + mark)}</span>
        </span>
      </button>`;
    }).join('')}</div>`;
  }

  async _openFolder(folder) {
    this.folder = folder;
    this._clearErr();
    this._busy(`Смотрю чаты в «${folder.title}»…`);
    try {
      const data = await this.api.listAccountChats(this.account.id, folder.id);
      this.chats = data.chats || [];
      if (data.folder_title) this.folder = { ...folder, title: data.folder_title };
    } catch (e) {
      this._error(e.message);
      this._renderFolders();
      return;
    }
    this._renderChats();
  }

  _renderChats() {
    this._setStep('chats');
    const q = (this.searchEl && this.searchEl.value || '').trim().toLowerCase();
    const list = this.chats.filter((c) => {
      if (!q) return true;
      return String(c.name || '').toLowerCase().includes(q) || String(c.last || '').toLowerCase().includes(q);
    });
    if (!this.chats.length) {
      this.bodyEl.innerHTML = '<p class="harvest-empty">В этой папке нет личных чатов. Перетащите диалоги в папку в Telegram и откройте её снова.</p>';
      return;
    }
    if (!list.length) {
      this.bodyEl.innerHTML = '<p class="harvest-empty">Ничего не найдено.</p>';
      return;
    }
    this.bodyEl.innerHTML = `<div class="harvest-list">${list.map((c) => `
      <button type="button" class="harvest-row" data-chat="${HarvestDialog._escape(c.id)}">
        <span class="harvest-row-main">
          <b>${HarvestDialog._escape(c.name)}</b>
          <span class="meta">${HarvestDialog._escape(c.last || 'нет текста')}</span>
        </span>
      </button>
    `).join('')}</div>`;
  }

  async _importChat(chatId, chatName) {
    this._clearErr();
    if (this.searchWrap) this.searchWrap.hidden = true;
    if (this.footerEl) this.footerEl.hidden = true;
    this._busy(`Читаю историю «${chatName}» с первого сообщения…`);
    try {
      const result = await this.api.importAccountChat(this.account.id, chatId);
      this.step = 'done';
      this._renderDone(result);
      this.toast(`Добавлено ${result.added} пар в примеры модели`, 'success');
    } catch (e) {
      this._error(e.message);
      this._renderChats();
    }
  }

  _renderDone(result) {
    this.titleEl.textContent = 'Чат выгружен';
    const scanned = result.scanned != null ? ` Просмотрено сообщений: ${result.scanned}.` : '';
    this.subEl.textContent = `«${result.name || 'чат'}»: ${result.added} текстовых пар.${scanned} Фото и аудио пропущены. Перезапустите backend, если агент уже работает.`;
    const pairs = result.pairs || [];
    const sample = pairs.length
      ? `<div class="harvest-pairs">${pairs.map((p) => `
          <div class="harvest-pair">
            <div class="u">${HarvestDialog._escape(p.user)}</div>
            <div class="a">${HarvestDialog._escape(p.assistant)}</div>
          </div>`).join('')}</div>`
      : '';
    this.bodyEl.innerHTML = `${sample}
      <div class="modal-footer" style="margin-top:14px">
        <button class="btn btn-ghost" type="button" data-harvest="again">Другой чат</button>
        <button class="btn btn-primary" type="button" data-harvest="close">Готово</button>
      </div>`;
  }

  _onBodyClick(e) {
    const again = e.target.closest('[data-harvest="again"]');
    if (again) {
      this._clearErr();
      if (this.searchEl) this.searchEl.value = '';
      this._renderChats();
      return;
    }
    const closeBtn = e.target.closest('[data-harvest="close"]');
    if (closeBtn) {
      this.close();
      return;
    }
    const accBtn = e.target.closest('[data-account]');
    if (accBtn) {
      const id = accBtn.dataset.account;
      const acc = this.accounts.find((a) => a.id === id);
      if (acc) this._openAccount(acc);
      return;
    }
    const folderBtn = e.target.closest('[data-folder]');
    if (folderBtn) {
      const id = folderBtn.dataset.folder;
      const folder = this.folders.find((f) => String(f.id) === String(id));
      if (folder) this._openFolder(folder);
      return;
    }
    const chatBtn = e.target.closest('[data-chat]');
    if (chatBtn && this.account) {
      const id = chatBtn.dataset.chat;
      const chat = this.chats.find((c) => String(c.id) === String(id));
      this._importChat(id, (chat && chat.name) || 'чат');
    }
  }

  static _escape(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}
