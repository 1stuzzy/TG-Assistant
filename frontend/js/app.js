/**
 * App — корень фронтенда: аккаунты, мастер входа и запуск ИИ-агента.
 */
class AgentDialog {
  constructor({ api, toast, onStarted }) {
    this.api = api;
    this.toast = toast;
    this.onStarted = onStarted;
    this.accountId = null;
    this.overlay = document.getElementById('agentOverlay');
    this.select = document.getElementById('agentModelSelect');
    this.persona = document.getElementById('agentPersona');
    this.engineLocal = document.getElementById('engineLocal');
    this.engineRemote = document.getElementById('engineRemote');
    this.workerSelect = document.getElementById('agentWorkerSelect');
    this.characterSelect = document.getElementById('agentCharacterSelect');
    this.localFields = document.getElementById('agentLocalFields');
    this.remoteFields = document.getElementById('agentRemoteFields');
    this.err = document.getElementById('errAgent');
    document.getElementById('agentCloseBtn').addEventListener('click', () => this.close());
    document.getElementById('agentCancelBtn').addEventListener('click', () => this.close());
    document.getElementById('agentStartBtn').addEventListener('click', () => this.submit());
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });
    const sync = () => this._syncEngine();
    if (this.engineLocal) this.engineLocal.addEventListener('change', sync);
    if (this.engineRemote) this.engineRemote.addEventListener('change', sync);
    this._syncEngine();
    this.quota = null;
  }

  setQuota(quota) {
    this.quota = quota || null;
    const note = document.getElementById('agentAssignedNote');
    const engineRow = this.engineLocal && this.engineLocal.closest('.field');
    const isTenant = Boolean(this.quota);
    if (note) {
      note.hidden = true;
    }
    if (engineRow) engineRow.style.display = isTenant ? 'none' : '';
    if (this.localFields) this.localFields.style.display = isTenant ? 'none' : (this.engineRemote && this.engineRemote.checked ? 'none' : '');
    if (this.remoteFields) this.remoteFields.style.display = isTenant ? 'none' : (this.engineRemote && this.engineRemote.checked ? '' : 'none');
  }

  _syncEngine() {
    if (this.quota) {
      if (this.localFields) this.localFields.style.display = 'none';
      if (this.remoteFields) this.remoteFields.style.display = 'none';
      return;
    }
    const remote = this.engineRemote && this.engineRemote.checked;
    if (this.localFields) this.localFields.style.display = remote ? 'none' : '';
    if (this.remoteFields) this.remoteFields.style.display = remote ? '' : 'none';
  }

  async open(accountId) {
    this.accountId = accountId;
    this.err.classList.remove('show');
    this.err.textContent = '';
    if (this.persona) this.persona.value = '';
    this.overlay.classList.add('open');
    this._syncEngine();
    try {
      const [models, characters, workers] = await Promise.all([
        this.quota ? Promise.resolve([]) : this.api.listModels(),
        this.api.listCharacters().catch(() => []),
        this.quota ? Promise.resolve([]) : this.api.listWorkers().catch(() => []),
      ]);
      this.select.innerHTML = models.length
        ? models.map((m) => `<option value="${m.name}" ${m.is_default ? 'selected' : ''}>${m.name} (${m.size_label})</option>`).join('')
        : '<option value="">Нет моделей — загрузите во вкладке Настройки</option>';
      if (this.characterSelect) {
        this.characterSelect.innerHTML = '<option value="">Без персонажа (общий стиль)</option>' +
          characters.map((c) => `<option value="${c.id}">${c.name}${c.age ? ', ' + c.age : ''}${c.city ? ' · ' + c.city : ''}</option>`).join('');
      }
      if (this.workerSelect) {
        this.workerSelect.innerHTML = workers.length
          ? workers.map((w) => `<option value="${w.id}">${w.name} — ${w.url}</option>`).join('')
          : '<option value="">Сначала добавьте удалённый сервер в Настройках</option>';
      }
    } catch (e) {
      this.select.innerHTML = `<option value="">${e.message}</option>`;
    }
  }

  close() {
    this.overlay.classList.remove('open');
  }

  async submit() {
    const remote = this.engineRemote && this.engineRemote.checked;
    const model = this.select ? this.select.value : '';
    if (!this.quota && !remote && !model) {
      this.err.textContent = 'Сначала загрузите GGUF-модель во вкладке «Настройки»';
      this.err.classList.add('show');
      return;
    }
    if (!this.quota && remote && (!this.workerSelect || !this.workerSelect.value)) {
      this.err.textContent = 'Выберите удалённый сервер или добавьте его в Настройках';
      this.err.classList.add('show');
      return;
    }
    const btn = document.getElementById('agentStartBtn');
    btn.disabled = true;
    btn.textContent = this.quota || remote ? 'Запуск…' : 'Загрузка модели…';
    try {
      await this.api.startAgent(this.accountId, {
        model: this.quota ? null : (remote ? null : model),
        persona: this.persona ? this.persona.value.trim() : '',
        character_id: this.characterSelect && this.characterSelect.value ? this.characterSelect.value : null,
        engine: this.quota ? 'local' : (remote ? 'remote' : 'local'),
        worker_id: this.quota ? null : (remote ? this.workerSelect.value : null),
      });
      this.toast('ok', remote ? 'Агент запущен на удалённом сервере' : 'ИИ-агент запущен на этом сервере');
      this.close();
      await this.onStarted();
    } catch (e) {
      this.err.textContent = e.message;
      this.err.classList.add('show');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Запустить';
    }
  }
}

class App {
  constructor(apiBaseUrl) {
    this.api = new ApiClient(apiBaseUrl);
    this.maintenance = false;

    this.accountsView = new AccountsView({
      api: this.api,
      gridEl: document.getElementById('grid'),
      searchEl: document.getElementById('searchInput'),
      statTotalEl: document.getElementById('statTotal'),
      statActiveEl: document.getElementById('statActive'),
      statInactiveEl: document.getElementById('statInactive'),
      statAgentsEl: document.getElementById('statAgents'),
      toast: (type, msg) => this.toast(type, msg),
      onStartAgent: (id) => {
        if (this.maintenance) {
          this.toast('err', 'Технические работы. Запуск агентов временно недоступен.');
          return;
        }
        this.agentDialog.open(id);
      },
    });

    this.wizard = new LoginWizard({
      api: this.api,
      toast: (type, msg) => this.toast(type, msg),
      onFinished: () => this.accountsView.refresh(),
    });

    this.agentDialog = new AgentDialog({
      api: this.api,
      toast: (type, msg) => this.toast(type, msg),
      onStarted: () => this.accountsView.refresh(),
    });
    this.devConsole = new DevConsole(apiBaseUrl);

    this.settings = new SettingsManager({
      container: document.getElementById('tabSettings'),
      onToast: (msg, kind) => {
        const map = { error: 'err', success: 'ok', info: 'ok' };
        this.toast(map[kind] || 'ok', msg);
      },
      onQuota: (quota) => {
        this.quota = quota;
        this._renderCabinet();
      },
    });
    this._bindNav();

    document.getElementById('addAccountBtn').addEventListener('click', () => {
      if (this.maintenance) {
        this.toast('err', 'Технические работы. Добавление аккаунтов временно недоступно.');
        return;
      }
      this.wizard.open();
    });
    document.getElementById('finishBtn').addEventListener('click', () => this.wizard.close());
    document.addEventListener('open-add-modal', () => {
      if (this.maintenance) {
        this.toast('err', 'Технические работы. Добавление аккаунтов временно недоступно.');
        return;
      }
      this.wizard.open();
    });
  }

  _bindNav() {
    document.querySelectorAll('.nav-item[data-tab]').forEach((item) => {
      item.addEventListener('click', () => {
        const tab = item.dataset.tab;
        if (this.maintenance && this.user && this.user.role === 'tenant' && tab !== 'cabinet') {
          return;
        }
        this._openTab(tab);
      });
    });
  }

  _openTab(tab) {
    document.querySelectorAll('.nav-item[data-tab]').forEach((n) => {
      n.classList.toggle('active', n.dataset.tab === tab);
    });
    document.querySelectorAll('.settings-content').forEach((el) => el.classList.remove('active'));
    if (tab === 'accounts') document.getElementById('tabAccounts').classList.add('active');
    if (tab === 'cabinet') {
      const el = document.getElementById('tabCabinet');
      if (el) el.classList.add('active');
      this._renderCabinet();
    }
    if (tab === 'settings') {
      document.getElementById('tabSettings').classList.add('active');
      this.settings.refresh();
    }
    if (tab === 'tenants') {
      const el = document.getElementById('tabTenants');
      if (el) el.classList.add('active');
      if (this.tenants) this.tenants.refresh();
    }
  }

  applyRole(me) {
    const isAdmin = me.user && me.user.role === 'admin';
    document.body.classList.toggle('role-admin', isAdmin);
    document.body.classList.toggle('role-tenant', !isAdmin);
    const sub = document.querySelector('.brand-sub');
    if (sub) sub.textContent = isAdmin ? 'administration console' : 'tenant panel';
    const loginEl = document.getElementById('userLogin');
    if (loginEl) loginEl.textContent = (me.user && me.user.login) || '';
    this.settings.role = isAdmin ? 'admin' : 'tenant';
    this.settings.setQuota(isAdmin ? null : me.quota);
    this.agentDialog.setQuota(isAdmin ? null : me.quota);
    this.applyMaintenance(!!me.maintenance);
    this._renderCabinet();
    if (!isAdmin) {
      if (me.maintenance) this._openTab('cabinet');
      else {
        const charTab = document.querySelector('.settings-tab[data-panel="characters"]');
        if (charTab) charTab.click();
      }
    }
  }

  _powerLabel(power) {
    return { low: 'Базовый', medium: 'Стандарт', high: 'Расширенный', custom: 'Персональный' }[power]
      || 'Персональный';
  }

  _renderCabinet() {
    const tab = document.getElementById('tabCabinet');
    if (!tab) return;
    const q = this.quota;
    if (!q) return;
    const accounts = (this.accountsView && this.accountsView.accounts) || [];
    const used = this._accountsReady ? accounts.length : (Number(q.accounts_used) || 0);
    const agents = this._accountsReady
      ? accounts.filter((a) => a.agent && a.agent.running).length
      : (Number(q.agents_running) || 0);
    const maxAccounts = Number(q.max_accounts) || 0;
    const maxAgents = Number(q.max_agents) || 0;
    const maxChats = Number(q.max_chats) || 0;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('cabAccounts', `${used}/${maxAccounts}`);
    set('cabAgents', `${agents}/${maxAgents}`);
    set('cabChats', String(maxChats));
    set('cabPower', this._powerLabel(q.power));
    set('cabName', q.name || 'Панель');
    set('cabFolder', (q.folder_title_effective || q.folder_title || 'TG-Assistant'));
    set('cabLogin', (this.user && this.user.login) || '—');
    const suspended = q.status && q.status !== 'active';
    set('cabStatus', suspended ? 'приостановлена' : 'активна');
    const statusEl = document.getElementById('cabStatus');
    if (statusEl) statusEl.className = suspended ? 'cab-bad' : 'cab-ok';
    set('cabMaxAccounts', String(maxAccounts));
    set('cabMaxAgents', String(maxAgents));
    set('cabMaxChats', String(maxChats));
    set('cabReadDelay', `${q.read_delay_ms ?? 800} мс`);
    set('cabReplyDelay', `${q.reply_delay_ms ?? 1500} мс`);
    const warn = document.getElementById('cabinetSuspended');
    if (warn) {
      warn.hidden = !suspended;
      warn.innerHTML = suspended
        ? '<b>Панель приостановлена.</b> Новые аккаунты и агенты недоступны.'
        : '';
    }
    const cabMaint = document.getElementById('cabinetMaintBanner');
    if (cabMaint) cabMaint.hidden = !this.maintenance;
  }

  applyMaintenance(on) {
    const was = this.maintenance;
    const next = !!on;
    const isTenant = this.user && this.user.role === 'tenant';
    this.maintenance = next;
    document.body.classList.toggle('is-maintenance', next);
    const cab = document.getElementById('cabinetMaintBanner');
    if (cab) cab.hidden = !next;
    if (isTenant && next && !was) {
      this._openTab('cabinet');
      if (this.wizard) this.wizard.close();
      if (this.agentDialog) this.agentDialog.close();
    }
    if (isTenant && was && !next) {
      this.toast('ok', 'Технические работы завершены. Можно снова добавлять аккаунты и запускать агентов.', 6000);
    }
  }

  async start() {
    await this._checkApiHealth();
    let me;
    try {
      me = await this.api.me();
    } catch (e) {
      location.href = '/login.html';
      return;
    }
    this.user = me.user;
    this.quota = me.quota || null;
    this._accountsReady = false;
    this.applyRole(me);
    if (me.user && me.user.role === 'admin') {
      this.tenants = new TenantsManager({
        api: this.api,
        toast: (type, msg) => this.toast(type, msg),
      });
    }
    const logout = document.getElementById('logoutBtn');
    if (logout) {
      logout.addEventListener('click', async () => {
        try { await this.api.logout(); } catch (_) {}
        location.href = '/login.html';
      });
    }
    try {
      await this.accountsView.load();
      this._accountsReady = true;
      this._renderCabinet();
    } catch (e) {
      this.toast('err', e.message);
    }
    this.settings.refresh();
    const poll = () => {
      const accounts = this.accountsView.accounts || [];
      const generating = accounts.some((a) => a.agent && a.agent.status === 'generating');
      const booting = accounts.some((a) => a.agent && a.agent.running && ['starting', 'loading_model', 'connecting'].includes(a.agent.status));
      const running = accounts.some((a) => a.agent && a.agent.running);
      if (generating || running) {
        this.accountsView.load().then(() => this._renderCabinet()).catch(() => {});
      } else {
        this._renderCabinet();
      }
      if (this.user && this.user.role === 'tenant') {
        this.api.me().then((m) => this.applyMaintenance(!!m.maintenance)).catch(() => {});
      }
      const wait = this.maintenance ? 2500 : (generating ? 1500 : booting ? 1000 : running ? 4000 : 8000);
      setTimeout(poll, wait);
    };
    setTimeout(poll, 4000);
  }

  async _checkApiHealth() {
    const el = document.getElementById('apiStatus');
    if (!el) return;
    try {
      await this.api.health();
      el.classList.add('ok');
      el.querySelector('span:last-child').textContent = 'Backend подключён';
    } catch (e) {
      el.classList.add('down');
      el.querySelector('span:last-child').textContent = 'Backend недоступен — запустите run.py';
    }
  }

  toast(type, text, ms) {
    const wrap = document.getElementById('toastWrap');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span class="tdot"></span>${text}`;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transition = 'opacity .25s';
      setTimeout(() => el.remove(), 250);
    }, ms || 3200);
  }
}

const API_BASE = (location.port === '5500' || location.port === '5501')
  ? 'http://localhost:8000'
  : location.origin;
const app = new App(API_BASE);
app.start();
