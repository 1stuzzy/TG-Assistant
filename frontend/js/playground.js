/**
 * Тест ответов модели без Telegram.
 */
class ModelPlayground {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.history = [];
    this.busy = false;
    this.sourceSel = document.getElementById('playSource');
    this.modelSel = document.getElementById('playModel');
    this.charSel = document.getElementById('playCharacter');
    this.modelField = document.getElementById('playModelField');
    this.charField = document.getElementById('playCharField');
    this.thread = document.getElementById('playThread');
    this.input = document.getElementById('playInput');
    this.sendBtn = document.getElementById('playSend');
    this.clearBtn = document.getElementById('playClear');
    this.status = document.getElementById('playStatus');
    this.sendBtn?.addEventListener('click', () => this.send());
    this.clearBtn?.addEventListener('click', () => this.clear());
    this.sourceSel?.addEventListener('change', () => this._syncFields());
    this.input?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.send();
      }
    });
  }

  _remoteId() {
    const v = this.sourceSel?.value || 'local';
    return v.startsWith('w:') ? v.slice(2) : '';
  }

  _syncFields() {
    const remote = !!this._remoteId();
    if (this.modelField) this.modelField.hidden = remote;
    if (this.charField) this.charField.hidden = remote;
    if (this.status && remote) {
      this.status.textContent = 'Удалённый сервер, без системного промпта';
    }
  }

  async refresh() {
    try {
      const [models, characters, workers] = await Promise.all([
        this.api.listModels().catch(() => []),
        this.api.listCharacters().catch(() => []),
        this.api.listWorkers().catch(() => []),
      ]);
      if (this.sourceSel) {
        const cur = this.sourceSel.value;
        const localOpts = '<option value="local">Этот ПК (локальная GGUF)</option>';
        const remoteOpts = (workers || []).map(
          (w) => `<option value="w:${w.id}">${w.name} — удалённый, без промпта</option>`
        ).join('');
        this.sourceSel.innerHTML = localOpts + remoteOpts;
        if (cur && [...this.sourceSel.options].some((o) => o.value === cur)) {
          this.sourceSel.value = cur;
        } else if (workers.length) {
          this.sourceSel.value = `w:${workers[0].id}`;
        }
      }
      if (this.modelSel) {
        const cur = this.modelSel.value;
        this.modelSel.innerHTML = (models.length
          ? models.map((m) => `<option value="${m.name}">${m.name} (${m.size_label || ''})</option>`).join('')
          : '<option value="">Нет моделей</option>');
        if (cur) this.modelSel.value = cur;
      }
      if (this.charSel) {
        const cur = this.charSel.value;
        this.charSel.innerHTML = '<option value="">Без персонажа</option>' +
          characters.map((c) => `<option value="${c.id}">${c.name}</option>`).join('');
        if (cur) this.charSel.value = cur;
      }
      this._syncFields();
    } catch (e) {
      if (this.status) this.status.textContent = e.message;
    }
  }

  clear() {
    this.history = [];
    this._render();
    if (this.status) this.status.textContent = this._remoteId() ? 'Удалённый сервер, без системного промпта' : '';
  }

  _render(typing) {
    if (!this.thread) return;
    const rows = this.history.map((m) => {
      const mine = m.role === 'user';
      return `<div class="bubble ${mine ? 'out' : 'in'}">
        <div class="bubble-meta">${mine ? 'Вы' : 'Модель'}</div>
        <div class="bubble-text">${DialogHistory._esc(m.content || '')}</div>
      </div>`;
    });
    if (typing) {
      rows.push(`<div class="bubble in typing-live"><div class="bubble-meta">Модель печатает</div><div class="bubble-text">${DialogHistory._esc(typing)}</div></div>`);
    }
    const empty = this._remoteId()
      ? 'Напишите реплику — ответ с RTX, без карточки персонажа и без промпта Telegram.'
      : 'Напишите реплику — модель ответит так же, как в Telegram, без отправки в чат.';
    this.thread.innerHTML = rows.join('') || `<p class="history-empty">${empty}</p>`;
    this.thread.scrollTop = this.thread.scrollHeight;
  }

  async send() {
    const text = (this.input?.value || '').trim();
    if (!text || this.busy) return;
    this.busy = true;
    this.history.push({ role: 'user', content: text });
    if (this.input) this.input.value = '';
    this._render('…');
    if (this.sendBtn) this.sendBtn.disabled = true;
    if (this.status) this.status.textContent = 'Считает ответ…';
    const poll = setInterval(async () => {
      try {
        const logs = await this.api.modelLogs();
        const last = [...(logs || [])].reverse().find((r) => r.kind === 'typing' && r.account_id === 'playground');
        if (last && last.detail) this._render(last.detail);
      } catch (_) {}
    }, 800);
    try {
      const workerId = this._remoteId();
      const data = await this.api.testLlm({
        message: text,
        worker_id: workerId || null,
        model: workerId ? null : (this.modelSel?.value || null),
        character_id: workerId ? null : (this.charSel?.value || null),
        history: this.history.slice(0, -1),
      });
      this.history.push({ role: 'assistant', content: data.reply || '' });
      this._render();
      if (this.status) {
        this.status.textContent = `${data.model || 'модель'} · ${data.elapsed ?? '—'} с`;
      }
    } catch (e) {
      this._render();
      if (this.status) this.status.textContent = e.message;
      this.toast('err', e.message);
    } finally {
      clearInterval(poll);
      this.busy = false;
      if (this.sendBtn) this.sendBtn.disabled = false;
    }
  }
}
