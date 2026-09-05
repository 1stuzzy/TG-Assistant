/**
 * ApiClient — единственная точка общения фронтенда с backend.
 * Все методы возвращают Promise и бросают Error с человекочитаемым
 * сообщением (взятым из поля detail ответа FastAPI).
 */
class ApiClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async _request(path, options = {}) {
    const { skipAuthRedirect, headers, ...rest } = options;
    let res;
    try {
      res = await fetch(this.baseUrl + path, {
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', ...(headers || {}) },
        ...rest,
      });
    } catch (e) {
      throw new Error('Не удалось подключиться к серверу. Backend запущен?');
    }

    if (res.status === 401 && !skipAuthRedirect && !/login\.html$/.test(location.pathname)) {
      location.href = (location.port === '5500' || location.port === '5501') ? '/login.html' : '/login.html';
      throw new Error('Войдите в панель');
    }

    if (!res.ok) {
      let detail = 'Ошибка запроса';
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }

    if (res.status === 204) return null;
    return res.json();
  }

  health() {
    return this._request('/health', { skipAuthRedirect: true });
  }

  me() {
    return this._request('/api/auth/me', { skipAuthRedirect: true });
  }

  login(login, password) {
    return this._request('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ login, password }),
      skipAuthRedirect: true,
    });
  }

  logout() {
    return this._request('/api/auth/logout', { method: 'POST', skipAuthRedirect: true });
  }

  updateMyDelays(data) {
    return this._request('/api/me/delays', { method: 'PATCH', body: JSON.stringify(data) });
  }

  myTelegram() {
    return this._request('/api/me/telegram');
  }

  updateMyTelegram(data) {
    return this._request('/api/me/telegram', { method: 'PATCH', body: JSON.stringify(data) });
  }

  listTenants() {
    return this._request('/api/admin/tenants');
  }

  createTenant(data) {
    return this._request('/api/admin/tenants', { method: 'POST', body: JSON.stringify(data) });
  }

  updateTenant(id, data) {
    return this._request(`/api/admin/tenants/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
  }

  setTenantAccess(id, granted) {
    return this._request(`/api/admin/tenants/${id}/access`, {
      method: 'POST',
      body: JSON.stringify({ granted }),
    });
  }

  deleteTenant(id) {
    return this._request(`/api/admin/tenants/${id}`, { method: 'DELETE' });
  }

  aiOptions() {
    return this._request('/api/admin/ai-options');
  }

  listAccounts() {
    return this._request('/api/accounts');
  }

  startLogin(phone) {
    return this._request('/api/accounts/login/start', {
      method: 'POST',
      body: JSON.stringify({ phone }),
    });
  }

  confirmCode(loginId, code) {
    return this._request('/api/accounts/login/code', {
      method: 'POST',
      body: JSON.stringify({ login_id: loginId, code }),
    });
  }

  confirmPassword(loginId, password) {
    return this._request('/api/accounts/login/password', {
      method: 'POST',
      body: JSON.stringify({ login_id: loginId, password }),
    });
  }

  checkAccount(id) {
    return this._request(`/api/accounts/${id}/check`, { method: 'POST' });
  }

  deleteAccount(id) {
    return this._request(`/api/accounts/${id}`, { method: 'DELETE' });
  }

  listModels() {
    return this._request('/api/models');
  }

  setDefaultModel(name) {
    return this._request('/api/models/default', {
      method: 'POST',
      body: JSON.stringify({ name }),
    });
  }

  clearDefaultModel() {
    return this._request('/api/models/default', { method: 'DELETE' });
  }

  deleteModel(name) {
    return this._request(`/api/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
  }

  systemInfo() {
    return this._request('/api/system');
  }

  setMaintenance(data) {
    return this._request('/api/admin/maintenance', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  startAgent(accountId, payload) {
    return this._request(`/api/accounts/${accountId}/agent/start`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  listCharacters() {
    return this._request('/api/characters');
  }

  createCharacter(data) {
    return this._request('/api/characters', { method: 'POST', body: JSON.stringify(data) });
  }

  updateCharacter(id, data) {
    return this._request(`/api/characters/${id}`, { method: 'PUT', body: JSON.stringify(data) });
  }

  deleteCharacter(id) {
    return this._request(`/api/characters/${id}`, { method: 'DELETE' });
  }

  listWorkers() {
    return this._request('/api/workers');
  }

  createWorker(data) {
    return this._request('/api/workers', { method: 'POST', body: JSON.stringify(data) });
  }

  deleteWorker(id) {
    return this._request(`/api/workers/${id}`, { method: 'DELETE' });
  }

  pingWorker(id) {
    return this._request(`/api/workers/${id}/ping`, { method: 'POST' });
  }

  workerBundleUrl() {
    return `${this.baseUrl}/api/workers/bundle`;
  }

  listLogs() {
    return this._request('/api/logs');
  }

  listAccountFolders(accountId) {
    return this._request(`/api/accounts/${accountId}/folders`);
  }

  listAccountChats(accountId, folderId) {
    const q = folderId ? `?folder_id=${encodeURIComponent(folderId)}` : '';
    return this._request(`/api/accounts/${accountId}/chats${q}`);
  }

  importAccountChat(accountId, chatId) {
    return this._request(`/api/accounts/${accountId}/chats/${encodeURIComponent(chatId)}/import`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
  }

  stopAgent(accountId) {
    return this._request(`/api/accounts/${accountId}/agent/stop`, { method: 'POST' });
  }

  dialogHistory(accountId) {
    return this._request(`/api/accounts/${accountId}/dialog-history`);
  }

  modelLogs(accountId) {
    const q = accountId ? `?account_id=${encodeURIComponent(accountId)}` : '';
    return this._request(`/api/model-logs${q}`);
  }

  testLlm(payload) {
    return this._request('/api/llm/test', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  uploadModel(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${this.baseUrl}/api/models/upload`);
      xhr.withCredentials = true;
      xhr.upload.onprogress = (e) => {
        if (onProgress && e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText || '{}');
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else reject(new Error(body.detail || 'Ошибка загрузки'));
        } catch (e) {
          reject(new Error('Ошибка загрузки'));
        }
      };
      xhr.onerror = () => reject(new Error('Не удалось подключиться к серверу. Backend запущен?'));
      const form = new FormData();
      form.append('file', file);
      xhr.send(form);
    });
  }
}
