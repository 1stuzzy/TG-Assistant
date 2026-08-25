/**
 * SettingsManager — вкладка «Настройки»: каталог GGUF, загрузка и скачивание CPU-моделей.
 */
class SettingsManager {
  constructor({ container, onToast, onLog }) {
    this.container = container;
    this.onToast = onToast || (() => {});
    this.onLog = onLog || (() => {});
    this.api = new ApiClient(
      (location.port === '5500' || location.port === '5501') ? 'http://localhost:8000' : location.origin
    );
    this._bindTabs();
    this._bindUpload();
    this._bindDefault();
    this._bindCharacters();
    this._bindWorkers();
    this._bindDelays();
    const dl = document.getElementById('downloadWorkerBtn');
    if (dl) dl.addEventListener('click', () => { window.location.href = this.api.workerBundleUrl(); });
    this.role = 'admin';
    this.quota = null;
    this._loadSystem();
  }

  setQuota(quota) {
    this.quota = quota || null;
    const card = document.getElementById('delayCard');
    if (card) card.hidden = !this.quota;
    if (!this.quota) return;
    const read = document.getElementById('readDelayMs');
    const reply = document.getElementById('replyDelayMs');
    if (read) read.value = this.quota.read_delay_ms ?? 800;
    if (reply) reply.value = this.quota.reply_delay_ms ?? 1500;
  }

  _bindDelays() {
    const form = document.getElementById('delayForm');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const read = Number(document.getElementById('readDelayMs').value);
      const reply = Number(document.getElementById('replyDelayMs').value);
      try {
        const quota = await this.api.updateMyDelays({
          read_delay_ms: Number.isFinite(read) ? read : 800,
          reply_delay_ms: Number.isFinite(reply) ? reply : 1500,
        });
        this.quota = quota;
        this.onToast('Задержки сохранены', 'success');
      } catch (err) {
        this.onToast(err.message, 'error');
      }
    });
  }

  _bindTabs() {
    this.container.querySelectorAll('.settings-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        this.container.querySelectorAll('.settings-tab').forEach((t) => t.classList.remove('active'));
        this.container.querySelectorAll('.settings-panel').forEach((p) => p.classList.remove('active'));
        tab.classList.add('active');
        const panel = this.container.querySelector(`#panel${tab.dataset.panel[0].toUpperCase()}${tab.dataset.panel.slice(1)}`);
        if (panel) panel.classList.add('active');
        if (tab.dataset.panel === 'models') this.refresh();
        if (tab.dataset.panel === 'system') this._loadSystem();
        if (tab.dataset.panel === 'characters') this._loadCharacters();
        if (tab.dataset.panel === 'workers') this._loadWorkers();
      });
    });
  }

  _bindUpload() {
    const area = document.getElementById('uploadArea');
    const input = document.getElementById('fileInput');
    if (!area || !input) return;
    area.addEventListener('click', () => input.click());
    area.addEventListener('dragover', (e) => { e.preventDefault(); area.classList.add('dragover'); });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
      e.preventDefault();
      area.classList.remove('dragover');
      const file = e.dataTransfer.files[0];
      if (file) this._upload(file);
    });
    input.addEventListener('change', () => {
      if (input.files[0]) this._upload(input.files[0]);
      input.value = '';
    });
  }

  _bindDefault() {
    const setBtn = document.getElementById('setDefaultBtn');
    const clearBtn = document.getElementById('clearDefaultBtn');
    if (setBtn) {
      setBtn.addEventListener('click', async () => {
        const name = document.getElementById('defaultModelSelect').value;
        if (!name) return this.onToast('Выберите модель', 'error');
        try {
          await this.api.setDefaultModel(name);
          this.onToast('Модель по умолчанию сохранена', 'success');
          this.refresh();
        } catch (e) {
          this.onToast(e.message, 'error');
        }
      });
    }
    if (clearBtn) {
      clearBtn.addEventListener('click', async () => {
        try {
          await this.api.clearDefaultModel();
          this.onToast('Модель по умолчанию сброшена', 'success');
          this.refresh();
        } catch (e) {
          this.onToast(e.message, 'error');
        }
      });
    }
  }

  async refresh() {
    try {
      const models = await this.api.listModels();
      this._renderLocal(models);
      if (this.role === 'admin') {
        const recommended = await this.api.listRecommendedModels().catch(() => []);
        this._renderRecommended(recommended);
      }
    } catch (e) {
      this.onToast(e.message, 'error');
    }
  }

  _renderLocal(models) {
    const list = document.getElementById('modelList');
    const empty = document.getElementById('emptyModels');
    const select = document.getElementById('defaultModelSelect');
    const current = document.getElementById('currentDefaultDisplay');
    if (!list) return;
    const def = models.find((m) => m.is_default);
    if (current) current.textContent = def ? def.name : 'не задана';
    if (select) {
      select.innerHTML = '<option value="">— Не выбрано —</option>' +
        models.map((m) => `<option value="${m.name}" ${m.is_default ? 'selected' : ''}>${m.name}</option>`).join('');
    }
    if (!models.length) {
      if (empty) empty.style.display = 'block';
      list.innerHTML = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    list.innerHTML = models.map((m) => `
      <div class="model-item">
        <div class="model-info">
          <span class="model-name">${m.name}</span>
          <span class="model-size">${m.size_label}</span>
          ${m.is_default ? '<span class="status-pill">по умолчанию</span>' : ''}
        </div>
        <div class="model-actions">
          <button class="btn-danger-sm" data-del="${m.name}">Удалить</button>
        </div>
      </div>`).join('');
    list.querySelectorAll('[data-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!confirm(`Удалить ${btn.dataset.del}?`)) return;
        try {
          await this.api.deleteModel(btn.dataset.del);
          this.onToast('Модель удалена', 'success');
          this.refresh();
        } catch (e) {
          this.onToast(e.message, 'error');
        }
      });
    });
  }

  _esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  _renderRecommended(items) {
    const box = document.getElementById('recommendedList');
    if (!box) return;
    const groups = [
      { id: 'cpu', title: 'CPU', hint: 'Этот компьютер, без видеокарты' },
      { id: 'gpu', title: 'GPU', hint: 'Игровой ПК, модель целиком в VRAM' },
      { id: 'hybrid', title: 'CPU+GPU', hint: 'Часть слоёв на карте, остальное в RAM' },
    ];
    box.innerHTML = groups.map((group) => {
      const rows = items.filter((m) => m.kind === group.id);
      if (!rows.length) return '';
      return `
        <div class="bench-group">
          <div class="bench-head">
            <span class="bench-tag bench-tag-${group.id}">${group.title}</span>
            <span class="bench-hint">${this._esc(group.hint)}</span>
          </div>
          ${rows.map((m) => {
            const mem = m.kind === 'cpu' ? m.ram_label : m.vram_label;
            return `
            <div class="bench-row">
              <div class="bench-main">
                <div class="bench-title">
                  <span class="model-name">${this._esc(m.name)}</span>
                  ${m.downloaded ? '<span class="status-pill">скачана</span>' : `<span class="status-pill">${this._esc(m.quality)}</span>`}
                </div>
                <div class="bench-meta">${this._esc(m.size_label)} · ${this._esc(mem)} · ${this._esc(m.speed)}</div>
                <div class="bench-desc">${this._esc(m.description)}</div>
              </div>
              <div class="model-actions">
                <a class="hf-link" href="${this._esc(m.hf_url)}" target="_blank" rel="noopener noreferrer">HF</a>
                ${m.downloaded
                  ? ''
                  : `<button class="btn-ghost-sm" data-dl="${this._esc(m.id)}">Скачать</button>`}
              </div>
            </div>`;
          }).join('')}
        </div>`;
    }).join('');
    box.querySelectorAll('[data-dl]').forEach((btn) => {
      btn.addEventListener('click', () => this._download(btn.dataset.dl, btn));
    });
  }

  async _upload(file) {
    if (!file.name.toLowerCase().endsWith('.gguf')) {
      this.onToast('Нужен файл .gguf', 'error');
      return;
    }
    const status = document.getElementById('uploadStatus');
    const bar = document.getElementById('uploadProgress');
    const fill = document.getElementById('progressFill');
    if (status) { status.className = 'upload-status loading'; status.textContent = 'Загрузка…'; }
    if (bar) bar.style.display = 'block';
    try {
      await this.api.uploadModel(file, (pct) => { if (fill) fill.style.width = pct + '%'; });
      if (status) { status.className = 'upload-status success'; status.textContent = 'Файл сохранён в data/models'; }
      this.onToast('Модель загружена', 'success');
      this.refresh();
    } catch (e) {
      if (status) { status.className = 'upload-status error'; status.textContent = e.message; }
      this.onToast(e.message, 'error');
    }
  }

  async _download(modelId, btn) {
    btn.disabled = true;
    btn.textContent = 'Скачивание…';
    try {
      await this.api.downloadModel(modelId);
      this.onToast('Скачивание началось, подождите 1–5 минут', 'info');
      const timer = setInterval(async () => {
        try {
          const st = await this.api.downloadStatus();
          btn.textContent = st.message || 'Скачивание…';
          if (st.status === 'done') {
            clearInterval(timer);
            this.onToast('Модель скачана', 'success');
            this.refresh();
          }
          if (st.status === 'error') {
            clearInterval(timer);
            this.onToast(st.message || 'Ошибка скачивания', 'error');
            this.refresh();
          }
        } catch (_) {}
      }, 2000);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Скачать';
      this.onToast(e.message, 'error');
    }
  }

  async _loadSystem() {
    try {
      const info = await this.api.systemInfo();
      const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
      set('appVersion', 'v' + info.app_version);
      set('apiVersion', info.api_version);
      set('systemModelCount', String(info.models));
      set('serverStatus', '● Online');
      const mem = document.getElementById('memoryUsage');
      if (mem) mem.textContent = info.loaded_model ? `в RAM: ${info.loaded_model}` : 'модель не загружена';
    } catch (_) {}
  }

  _bindCharacters() {
    const form = document.getElementById('characterForm');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = {
        name: document.getElementById('charName').value.trim(),
        age: Number(document.getElementById('charAge').value) || null,
        city: document.getElementById('charCity').value.trim(),
        gender: document.getElementById('charGender').value,
        occupation: document.getElementById('charOccupation').value.trim(),
        hobbies: document.getElementById('charHobbies').value.trim(),
        bio: document.getElementById('charBio').value.trim(),
        extra: document.getElementById('charExtra').value.trim(),
      };
      try {
        await this.api.createCharacter(data);
        this.onToast('Персонаж сохранён', 'success');
        form.reset();
        this._loadCharacters();
      } catch (err) {
        this.onToast(err.message, 'error');
      }
    });
  }

  async _loadCharacters() {
    const list = document.getElementById('characterList');
    if (!list) return;
    try {
      const items = await this.api.listCharacters();
      if (!items.length) {
        list.innerHTML = '<div class="empty-hint">Пока нет персонажей</div>';
        return;
      }
      list.innerHTML = items.map((c) => `
        <div class="model-item">
          <div class="model-info">
            <span class="model-name">${c.name}${c.age ? ', ' + c.age : ''}${c.city ? ' · ' + c.city : ''}</span>
            <span class="model-size">${c.occupation || ''} ${c.hobbies || ''}</span>
          </div>
          <div class="model-actions">
            <button class="btn-danger-sm" data-char-del="${c.id}">Удалить</button>
          </div>
        </div>`).join('');
      list.querySelectorAll('[data-char-del]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          if (!confirm('Удалить персонажа?')) return;
          try {
            await this.api.deleteCharacter(btn.dataset.charDel);
            this._loadCharacters();
          } catch (err) {
            this.onToast(err.message, 'error');
          }
        });
      });
    } catch (err) {
      this.onToast(err.message, 'error');
    }
  }

  _bindWorkers() {
    const form = document.getElementById('workerForm');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await this.api.createWorker({
          name: document.getElementById('workerName').value.trim(),
          url: document.getElementById('workerUrl').value.trim(),
          api_key: document.getElementById('workerKey').value.trim(),
        });
        this.onToast('Удалённый ПК добавлен', 'success');
        form.reset();
        this._loadWorkers();
      } catch (err) {
        this.onToast(err.message, 'error');
      }
    });
  }

  async _loadWorkers() {
    const list = document.getElementById('workerList');
    if (!list) return;
    try {
      const items = await this.api.listWorkers();
      if (!items.length) {
        list.innerHTML = '<div class="empty-hint">Пока нет удалённых ПК</div>';
        return;
      }
      list.innerHTML = items.map((w) => `
        <div class="model-item">
          <div class="model-info">
            <span class="model-name">${w.name}</span>
            <span class="model-size">${w.url}</span>
          </div>
          <div class="model-actions">
            <button class="btn-ghost-sm" data-ping="${w.id}">Проверить</button>
            <button class="btn-danger-sm" data-worker-del="${w.id}">Удалить</button>
          </div>
        </div>`).join('');
      list.querySelectorAll('[data-ping]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            const st = await this.api.pingWorker(btn.dataset.ping);
            if (st.loading || !st.ok) {
              this.onToast('Сервер онлайн, модель ещё загружается. Дождитесь «Готово» в start.bat', 'error');
            } else {
              this.onToast(`Онлайн: ${st.model || 'ok'} (${st.device || ''})`, 'success');
            }
          } catch (err) {
            this.onToast(err.message, 'error');
          } finally {
            btn.disabled = false;
          }
        });
      });
      list.querySelectorAll('[data-worker-del]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          try {
            await this.api.deleteWorker(btn.dataset.workerDel);
            this._loadWorkers();
          } catch (err) {
            this.onToast(err.message, 'error');
          }
        });
      });
    } catch (err) {
      this.onToast(err.message, 'error');
    }
  }
}
