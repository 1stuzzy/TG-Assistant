/**
 * Админка арендаторов: панели, лимиты, модель и доступ.
 */
class TenantsManager {
  constructor({ api, toast }) {
    this.api = api;
    this.toast = toast;
    this.items = [];
    this.options = { models: [], workers: [], power_presets: {} };
    this.form = document.getElementById('tenantForm');
    this.list = document.getElementById('tenantList');
    if (!this.form) return;
    this.form.addEventListener('submit', (e) => { e.preventDefault(); this._save(); });
    document.getElementById('tenantResetBtn')?.addEventListener('click', () => this._reset());
    document.getElementById('tenantPower')?.addEventListener('change', () => this._applyPower());
    this.refresh();
  }

  async refresh() {
    try {
      const [items, options] = await Promise.all([
        this.api.listTenants(),
        this.api.aiOptions().catch(() => this.options),
      ]);
      this.items = items;
      this.options = options;
      this._fillSelects();
      this._render();
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  _fillSelects() {
    const model = document.getElementById('tenantModel');
    const worker = document.getElementById('tenantWorker');
    if (model) {
      const cur = model.value;
      model.innerHTML = '<option value="">— не назначена —</option>' +
        (this.options.models || []).map((m) => `<option value="${m.name}">${m.name}</option>`).join('');
      model.value = cur;
    }
    if (worker) {
      const cur = worker.value;
      worker.innerHTML = '<option value="">— не назначен —</option>' +
        (this.options.workers || []).map((w) => `<option value="${w.id}">${w.name}</option>`).join('');
      worker.value = cur;
    }
  }

  _applyPower() {
    const power = document.getElementById('tenantPower').value;
    const preset = (this.options.power_presets || {})[power];
    if (!preset) return;
    document.getElementById('tenantMaxAccounts').value = preset.max_accounts;
    document.getElementById('tenantMaxChats').value = preset.max_chats;
    document.getElementById('tenantMaxAgents').value = preset.max_agents;
  }

  _payload() {
    return {
      name: document.getElementById('tenantName').value.trim(),
      login: document.getElementById('tenantLogin').value.trim(),
      password: document.getElementById('tenantPassword').value,
      power: document.getElementById('tenantPower').value,
      max_accounts: Number(document.getElementById('tenantMaxAccounts').value) || null,
      max_chats: Number(document.getElementById('tenantMaxChats').value) || null,
      max_agents: Number(document.getElementById('tenantMaxAgents').value) || null,
      model_name: document.getElementById('tenantModel').value,
      engine: document.getElementById('tenantEngine').value,
      worker_id: document.getElementById('tenantWorker').value,
      note: document.getElementById('tenantNote').value.trim(),
      read_delay_ms: Number(document.getElementById('tenantReadDelay').value) || 0,
      reply_delay_ms: Number(document.getElementById('tenantReplyDelay').value) || 0,
    };
  }

  _reset() {
    this.form.dataset.id = '';
    this.form.reset();
    document.getElementById('tenantFormTitle').textContent = 'Новая панель';
    document.getElementById('tenantPassword').required = true;
  }

  _edit(t) {
    this.form.dataset.id = t.id;
    document.getElementById('tenantFormTitle').textContent = 'Панель: ' + t.name;
    document.getElementById('tenantName').value = t.name || '';
    document.getElementById('tenantLogin').value = t.login || '';
    document.getElementById('tenantPassword').value = '';
    document.getElementById('tenantPassword').required = false;
    document.getElementById('tenantPower').value = t.power || 'medium';
    document.getElementById('tenantMaxAccounts').value = t.max_accounts;
    document.getElementById('tenantMaxChats').value = t.max_chats;
    document.getElementById('tenantMaxAgents').value = t.max_agents;
    document.getElementById('tenantModel').value = t.model_name || '';
    document.getElementById('tenantEngine').value = t.engine || 'local';
    document.getElementById('tenantWorker').value = t.worker_id || '';
    document.getElementById('tenantNote').value = t.note || '';
    document.getElementById('tenantReadDelay').value = t.read_delay_ms ?? 800;
    document.getElementById('tenantReplyDelay').value = t.reply_delay_ms ?? 1500;
  }

  async _save() {
    const data = this._payload();
    const id = this.form.dataset.id;
    if (id && !data.password) delete data.password;
    try {
      if (id) await this.api.updateTenant(id, data);
      else await this.api.createTenant(data);
      this.toast('ok', id ? 'Панель обновлена' : 'Панель создана');
      this._reset();
      await this.refresh();
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  async _access(id, granted) {
    try {
      await this.api.setTenantAccess(id, granted);
      this.toast('ok', granted ? 'Доступ выдан' : 'Доступ отозван, агенты остановлены');
      await this.refresh();
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  async _status(id, status) {
    try {
      await this.api.updateTenant(id, { status });
      this.toast('ok', status === 'active' ? 'Панель включена' : 'Панель приостановлена');
      await this.refresh();
    } catch (e) {
      this.toast('err', e.message);
    }
  }

  _render() {
    if (!this.list) return;
    if (!this.items.length) {
      this.list.innerHTML = '<div class="empty-hint">Пока нет арендаторов — создайте панель слева</div>';
      return;
    }
    const label = { active: 'активна', suspended: 'пауза', revoked: 'отозвана' };
    this.list.innerHTML = this.items.map((t) => `
      <div class="tenant-card">
        <div class="tenant-head">
          <div>
            <div class="model-name">${t.name}</div>
            <div class="model-size">${t.login} · ${t.accounts_used}/${t.max_accounts} акк. · ${t.agents_running}/${t.max_agents} агентов</div>
          </div>
          <span class="badge ${t.status}">${label[t.status] || t.status}</span>
        </div>
        <div class="tenant-meta">
          мощность ${t.power || 'medium'} · чатов на аккаунт ${t.max_chats}
          ${t.model_name ? ' · ' + t.model_name : ''}
          ${t.engine === 'remote' ? ' · удалённый ПК' : ''}
        </div>
        <div class="card-actions">
          <button class="btn btn-ghost btn-sm" data-edit="${t.id}">Изменить</button>
          ${t.status === 'active'
            ? `<button class="btn btn-ghost btn-sm" data-pause="${t.id}">Пауза</button>`
            : `<button class="btn btn-ghost btn-sm" data-resume="${t.id}">Включить</button>`}
          ${t.status === 'revoked'
            ? `<button class="btn btn-primary btn-sm" data-grant="${t.id}">Выдать доступ</button>`
            : `<button class="btn btn-danger-ghost btn-sm" data-revoke="${t.id}">Отозвать</button>`}
        </div>
      </div>`).join('');
    this.list.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => {
      const t = this.items.find((x) => x.id === b.dataset.edit);
      if (t) this._edit(t);
    }));
    this.list.querySelectorAll('[data-grant]').forEach((b) => b.addEventListener('click', () => this._access(b.dataset.grant, true)));
    this.list.querySelectorAll('[data-revoke]').forEach((b) => b.addEventListener('click', () => {
      if (confirm('Отозвать доступ? Арендатор выйдет, агенты остановятся.')) this._access(b.dataset.revoke, false);
    }));
    this.list.querySelectorAll('[data-pause]').forEach((b) => b.addEventListener('click', () => this._status(b.dataset.pause, 'suspended')));
    this.list.querySelectorAll('[data-resume]').forEach((b) => b.addEventListener('click', () => this._status(b.dataset.resume, 'active')));
  }
}
