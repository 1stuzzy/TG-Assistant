/**
 * История диалогов ИИ по аккаунту (админ).
 */
class DialogHistory {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.accountId = null;
    this.accountLabel = '';
    this.timer = null;
    this.overlay = document.getElementById('historyOverlay');
    this.title = document.getElementById('historyTitle');
    this.chatsEl = document.getElementById('historyChats');
    this.threadEl = document.getElementById('historyThread');
    this.typingEl = document.getElementById('historyTyping');
    this.chatId = null;
    document.getElementById('historyCloseBtn')?.addEventListener('click', () => this.close());
    this.overlay?.addEventListener('click', (e) => { if (e.target === this.overlay) this.close(); });
  }

  open(accountId, label) {
    this.accountId = accountId;
    this.accountLabel = label || accountId;
    this.chatId = null;
    if (this.title) this.title.textContent = 'История диалога · ' + this.accountLabel;
    this.overlay?.classList.add('open');
    this.refresh();
    this._arm();
  }

  close() {
    this.overlay?.classList.remove('open');
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  _arm() {
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => {
      if (this.overlay?.classList.contains('open')) this.refresh().catch(() => {});
    }, 2000);
  }

  async refresh() {
    if (!this.accountId) return;
    const data = await this.api.dialogHistory(this.accountId);
    const chats = data.chats || [];
    if (!this.chatId && chats[0]) this.chatId = chats[0].chat_id;
    if (this.chatsEl) {
      this.chatsEl.innerHTML = chats.length
        ? chats.map((c) => {
            const active = String(c.chat_id) === String(this.chatId) ? ' active' : '';
            const n = (c.messages || []).length;
            return `<button type="button" class="history-chat${active}" data-chat="${DialogHistory._escAttr(c.chat_id)}">
              <b>${DialogHistory._esc(c.peer || c.chat_id)}</b>
              <span>${n} сообщ.</span>
            </button>`;
          }).join('')
        : '<p class="history-empty">Пока нет сохранённых диалогов. Они появятся, когда агент начнёт отвечать.</p>';
      this.chatsEl.querySelectorAll('[data-chat]').forEach((btn) => {
        btn.addEventListener('click', () => {
          this.chatId = btn.dataset.chat;
          this.refresh().catch(() => {});
        });
      });
    }
    const chat = chats.find((c) => String(c.chat_id) === String(this.chatId));
    const msgs = (chat && chat.messages) || [];
    if (this.threadEl) {
      this.threadEl.innerHTML = msgs.length
        ? msgs.map((m) => {
            const mine = m.role === 'assistant';
            return `<div class="bubble ${mine ? 'out' : 'in'}">
              <div class="bubble-meta">${mine ? 'ИИ' : DialogHistory._esc(chat.peer || 'Собеседник')} · ${DialogHistory._clock(m.ts)}</div>
              <div class="bubble-text">${DialogHistory._esc(m.content || '')}</div>
            </div>`;
          }).join('')
        : '<p class="history-empty">Выберите чат слева</p>';
      this.threadEl.scrollTop = this.threadEl.scrollHeight;
    }
    if (this.typingEl) {
      const typing = (data.typing || '').trim();
      const generating = data.status === 'generating';
      this.typingEl.hidden = !(typing || generating);
      this.typingEl.textContent = typing
        ? 'Сейчас набирает: ' + typing
        : (generating ? 'Модель считает ответ…' : '');
    }
  }

  static _clock(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleString('ru-RU', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' });
  }

  static _esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  static _escAttr(value) {
    return DialogHistory._esc(value).replace(/"/g, '&quot;');
  }
}
