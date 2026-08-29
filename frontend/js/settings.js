/**
 * SettingsManager — вкладка «Настройки»: локальные GGUF, персонажи, удалённый сервер.
 */
class SettingsManager {
  constructor({ container, onToast, onLog, onQuota }) {
    this.container = container;
    this.onToast = onToast || (() => {});
    this.onLog = onLog || (() => {});
    this.onQuota = onQuota || (() => {});
    this.api = new ApiClient(
      (location.port === '5500' || location.port === '5501') ? 'http://localhost:8000' : location.origin
    );
    this._bindTabs();
    this._bindUpload();
    this._bindDefault();
    this._bindCharacters();
    this._bindWorkers();
    this._bindTelegram();
    const dl = document.getElementById('downloadWorkerBtn');
    if (dl) dl.addEventListener('click', () => { window.location.href = this.api.workerBundleUrl(); });
    this.role = 'admin';
    this.quota = null;
    this._loadTimer = null;
    this._bindMaint();
    this._loadSystem();
  }

  setQuota(quota) {
    this.quota = quota || null;
    if (this.quota) this._fillTelegram(this.quota);
    else this._loadTelegram();
  }

  _fillTelegram(src) {
    if (!src) return;
    const folder = document.getElementById('folderTitleInput');
    const read = document.getElementById('readDelayMs');
    const reply = document.getElementById('replyDelayMs');
    if (folder) {
      folder.value = src.folder_title || '';
      folder.placeholder = src.folder_title_default || 'TG-Assistant';
    }
    if (read) read.value = src.read_delay_ms ?? 800;
    if (reply) reply.value = src.reply_delay_ms ?? 1500;
  }

  async _loadTelegram() {
    try {
      const prefs = await this.api.myTelegram();
      this._fillTelegram(prefs);
    } catch (_) {}
  }

  _bindTelegram() {
    const form = document.getElementById('telegramForm');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const read = Number(document.getElementById('readDelayMs').value);
      const reply = Number(document.getElementById('replyDelayMs').value);
      const folder = (document.getElementById('folderTitleInput').value || '').trim();
      try {
        const prefs = await this.api.updateMyTelegram({
          folder_title: folder,
          read_delay_ms: Number.isFinite(read) ? read : 800,
          reply_delay_ms: Number.isFinite(reply) ? reply : 1500,
        });
        this._fillTelegram(prefs);
        if (this.role === 'tenant') {
          this.quota = { ...(this.quota || {}), ...prefs };
          this.onQuota(this.quota);
        }
        this.onToast('Настройки Telegram сохранены', 'success');
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
        if (tab.dataset.panel === 'system') {
          this._loadSystem();
          this._startLoadPoll();
        } else {
          this._stopLoadPoll();
        }
        if (tab.dataset.panel === 'characters') this._loadCharacters();
        if (tab.dataset.panel === 'workers') this._loadWorkers();
        if (tab.dataset.panel === 'telegram') this._loadTelegram();
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
    const clearBtn = document.getElementById('clearDefaultBtn');
    if (clearBtn) {
      clearBtn.addEventListener('click', async () => {
        try {
          await this.api.clearDefaultModel();
          this.onToast('Активная модель сброшена', 'success');
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
    } catch (e) {
      this.onToast(e.message, 'error');
    }
  }

  _renderLocal(models) {
    const list = document.getElementById('modelList');
    const empty = document.getElementById('emptyModels');
    const current = document.getElementById('currentDefaultDisplay');
    const chip = document.getElementById('modelCountChip');
    if (!list) return;
    const def = models.find((m) => m.is_default);
    if (current) current.textContent = def ? def.name : 'не задана';
    if (chip) chip.textContent = String(models.length);
    if (!models.length) {
      if (empty) empty.hidden = false;
      list.innerHTML = '';
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = models.map((m) => `
      <div class="model-item${m.is_default ? ' is-active' : ''}">
        <div class="model-info">
          <span class="model-name">${this._esc(m.name)}</span>
          <span class="model-size">${this._esc(m.size_label)}</span>
        </div>
        <div class="model-actions">
          ${m.is_default
            ? '<span class="status-pill">активна</span>'
            : `<button class="btn-ghost-sm" data-use="${this._esc(m.name)}">Сделать активной</button>`}
          <button class="btn-danger-sm" data-del="${this._esc(m.name)}">Удалить</button>
        </div>
      </div>`).join('');
    list.querySelectorAll('[data-use]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        try {
          await this.api.setDefaultModel(btn.dataset.use);
          this.onToast('Активная модель сохранена', 'success');
          this.refresh();
        } catch (e) {
          this.onToast(e.message, 'error');
        }
      });
    });
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

  _bindMaint() {
    const slots = document.getElementById('memorySlots');
    if (!slots) return;
    slots.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-maint]');
      if (!btn || btn.disabled) return;
      this._toggleMaint(btn.dataset.maint, btn.dataset.on === '1');
    });
  }

  async _toggleMaint(target, currentlyOn) {
    try {
      await this.api.setMaintenance({ target, enabled: !currentlyOn });
      this.onToast(currentlyOn ? 'Техработы сняты' : 'Техработы включены', 'success');
      await this._loadSystem();
    } catch (e) {
      this.onToast(e.message, 'error');
    }
  }

  _startLoadPoll() {
    this._stopLoadPoll();
    this._loadTimer = setInterval(() => this._loadSystem(), 4000);
  }

  _stopLoadPoll() {
    if (this._loadTimer) {
      clearInterval(this._loadTimer);
      this._loadTimer = null;
    }
  }

  _meter(label, percent, detail) {
    const pct = Math.max(0, Math.min(100, Number(percent) || 0));
    let lvl = '';
    if (pct >= 90) lvl = 'is-hot';
    else if (pct >= 75) lvl = 'is-warn';
    const extra = detail ? ` · ${this._esc(detail)}` : '';
    return `<div class="load-meter ${lvl}">
      <span class="load-label">${this._esc(label)}</span>
      <span class="load-bar"><i style="width:${pct}%"></i></span>
      <span class="load-val">${pct.toFixed(0)}%${extra}</span>
    </div>`;
  }

  _loadMeters(load) {
    if (!load) {
      return '<div class="load-na">Нет данных по нагрузке</div>';
    }
    const ram = (load.ram_used_gb != null && load.ram_total_gb != null)
      ? `${load.ram_used_gb} / ${load.ram_total_gb} ГБ` : '';
    const disk = (load.disk_used_gb != null && load.disk_total_gb != null)
      ? `${load.disk_used_gb} / ${load.disk_total_gb} ГБ` : '';
    const cpu = load.cpu_count ? `${load.cpu_count} ядр.` : '';
    return `<div class="load-meters">
      ${this._meter('CPU', load.cpu_percent, cpu)}
      ${this._meter('RAM', load.ram_percent, ram)}
      ${this._meter('Диск', load.disk_percent, disk)}
    </div>`;
  }

  async _loadSystem() {
    try {
      const info = await this.api.systemInfo();
      const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
      set('appVersion', 'v' + info.app_version);
      set('apiVersion', info.api_version);
      if (this.role === 'admin') set('systemModelCount', String(info.models ?? 0));
      set('serverStatus', '● Online');
      const slots = document.getElementById('memorySlots');
      if (slots && this.role === 'admin') {
        const mem = info.memory || {};
        const local = mem.local || {};
        const remote = mem.remote || [];
        const rows = [{
          id: 'local',
          name: local.name || 'Этот сервер',
          ok: local.ok !== false,
          kind: 'local',
          load: local.load || info.load,
          maintenance: !!(local.maintenance || info.maintenance),
        }];
        remote.forEach((w) => {
          rows.push({
            id: w.id || '',
            name: w.name || 'Удалённый сервер',
            ok: !!w.ok,
            kind: 'remote',
            loading: !!w.loading,
            load: w.load || null,
            error: w.error || '',
            maintenance: !!w.maintenance,
          });
        });
        slots.innerHTML = rows.map((r) => {
          let status = 'нет связи';
          if (r.maintenance) status = 'техработы';
          else if (r.kind === 'local') status = 'этот сервер';
          else if (r.loading) status = 'модель грузится';
          else if (r.ok) status = 'онлайн';
          const on = r.kind === 'local' ? !r.maintenance : (!!r.ok && !r.loading && !r.maintenance);
          const meters = (r.kind === 'local' || r.ok)
            ? this._loadMeters(r.load)
            : `<div class="load-na">${this._esc(r.error || 'Нет ответа')}</div>`;
          const target = r.kind === 'local' ? 'local' : r.id;
          const label = r.maintenance ? 'Завершить техработы' : 'Техработы';
          const btnClass = r.maintenance ? 'btn btn-danger-ghost btn-sm' : 'btn btn-ghost btn-sm';
          return `<div class="mem-row ${on ? 'is-on' : 'is-off'}${r.maintenance ? ' is-maint' : ''}">
            <div class="mem-row-head">
              <span class="mem-name">${this._esc(r.name)}</span>
              <span class="mem-status">${status}</span>
            </div>
            ${meters}
            <button type="button" class="${btnClass} mem-maint-btn" data-maint="${this._esc(target)}" data-on="${r.maintenance ? '1' : '0'}">${label}</button>
          </div>`;
        }).join('');
      }
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
        list.innerHTML = '<div class="empty-hint">Пока нет персонажей — добавьте справа</div>';
        return;
      }
      list.innerHTML = items.map((c) => `
        <div class="model-item">
          <div class="model-info">
            <span class="model-name">${this._esc(c.name)}${c.age ? ', ' + this._esc(c.age) : ''}${c.city ? ' · ' + this._esc(c.city) : ''}</span>
            <span class="model-size">${this._esc(c.occupation || '')} ${this._esc(c.hobbies || '')}</span>
          </div>
          <div class="model-actions">
            <button class="btn-danger-sm" data-char-del="${this._esc(c.id)}">Удалить</button>
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
        this.onToast('Удалённый сервер добавлен', 'success');
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
        list.innerHTML = '<div class="empty-hint">Пока нет удалённых серверов — добавьте справа</div>';
        return;
      }
      list.innerHTML = items.map((w) => `
        <div class="model-item">
          <div class="model-info">
            <span class="model-name">${this._esc(w.name)}</span>
            <span class="model-size">${this._esc(w.url)}</span>
          </div>
          <div class="model-actions">
            <button class="btn-ghost-sm" data-ping="${this._esc(w.id)}">Проверить</button>
            <button class="btn-danger-sm" data-worker-del="${this._esc(w.id)}">Удалить</button>
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
