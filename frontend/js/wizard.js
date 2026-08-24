/**
 * LoginWizard управляет модальным окном добавления аккаунта:
 * телефон -> код -> (опционально) 2FA -> сохранение сессии.
 * Все реальные вызовы идут через ApiClient в backend (Telethon).
 */
class LoginWizard {
  constructor({ api, onFinished, toast }) {
    this.api = api;
    this.onFinished = onFinished;
    this.toast = toast;
    this.loginId = null;

    this.overlay = document.getElementById('overlay');
    this.steps = {
      phone: document.getElementById('stepPhone'),
      code: document.getElementById('stepCode'),
      twofa: document.getElementById('step2FA'),
      loading: document.getElementById('stepLoading'),
      success: document.getElementById('stepSuccess'),
    };

    this._bindEvents();
  }

  open() {
    this.loginId = null;
    document.getElementById('phoneInput').value = '';
    document.getElementById('ccInput').value = '+7';
    document.querySelectorAll('.code-box').forEach((b) => (b.value = ''));
    document.getElementById('twoFaInput').value = '';
    this._clearErrors();
    this._goTo('phone');
    this.overlay.classList.add('open');
    setTimeout(() => document.getElementById('phoneInput').focus(), 50);
  }

  close() {
    this.overlay.classList.remove('open');
  }

  // ---------- внутреннее ----------

  _bindEvents() {
    document.getElementById('btnSendCode').addEventListener('click', () => this._submitPhone());
    document.getElementById('btnVerifyCode').addEventListener('click', () => this._submitCode());
    document.getElementById('btnVerify2fa').addEventListener('click', () => this._submitPassword());
    document.querySelectorAll('.close-x').forEach((b) => b.addEventListener('click', () => this.close()));
    document.querySelectorAll('[data-back]').forEach((b) =>
      b.addEventListener('click', () => this._goTo(b.dataset.back))
    );
    document.getElementById('resendCodeLink').addEventListener('click', () => {
      this.toast('ok', 'Код отправлен повторно');
    });
    this.overlay.addEventListener('click', (e) => {
      if (e.target === this.overlay) this.close();
    });

    const boxes = Array.from(document.querySelectorAll('.code-box'));
    boxes.forEach((box, i) => {
      box.addEventListener('input', () => {
        box.value = box.value.replace(/\D/g, '').slice(0, 1);
        if (box.value && boxes[i + 1]) boxes[i + 1].focus();
      });
      box.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !box.value && boxes[i - 1]) boxes[i - 1].focus();
      });
    });
  }

  _goTo(step) {
    Object.values(this.steps).forEach((el) => (el.style.display = 'none'));
    this.steps[step].style.display = 'block';
  }

  _clearErrors() {
    ['errPhone', 'errCode', 'err2fa'].forEach((id) => {
      const el = document.getElementById(id);
      el.classList.remove('show');
      el.textContent = '';
    });
  }

  _showError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.classList.add('show');
  }

  _setBtnLoading(id, loading) {
    const btn = document.getElementById(id);
    if (loading) {
      btn.dataset.label = btn.innerHTML;
      btn.innerHTML = '<div class="spinner"></div>';
      btn.disabled = true;
    } else {
      btn.innerHTML = btn.dataset.label;
      btn.disabled = false;
    }
  }

  async _submitPhone() {
    this._clearErrors();
    const cc = document.getElementById('ccInput').value.trim();
    const raw = document.getElementById('phoneInput').value.trim();
    const digits = raw.replace(/\D/g, '');

    if (!cc.startsWith('+') || cc.length < 2) {
      this._showError('errPhone', 'Укажите код страны, например +7');
      return;
    }
    if (digits.length < 6) {
      this._showError('errPhone', 'Введите корректный номер телефона');
      return;
    }

    const phone = `${cc}${digits}`;
    this._setBtnLoading('btnSendCode', true);
    try {
      const { login_id } = await this.api.startLogin(phone);
      this.loginId = login_id;
      this.phone = phone;
      document.getElementById('codePhoneLabel').textContent = phone;
      this._goTo('code');
      document.querySelector('.code-box').focus();
    } catch (e) {
      this._showError('errPhone', e.message);
    } finally {
      this._setBtnLoading('btnSendCode', false);
    }
  }

  async _submitCode() {
    this._clearErrors();
    const code = Array.from(document.querySelectorAll('.code-box')).map((b) => b.value).join('');
    if (code.length < 5) {
      this._showError('errCode', 'Введите все 5 цифр кода');
      return;
    }

    this._setBtnLoading('btnVerifyCode', true);
    try {
      const { status } = await this.api.confirmCode(this.loginId, code);
      if (status === 'need_2fa') {
        this._goTo('twofa');
        document.getElementById('twoFaInput').focus();
      } else {
        await this._finish();
      }
    } catch (e) {
      this._showError('errCode', e.message);
    } finally {
      this._setBtnLoading('btnVerifyCode', false);
    }
  }

  async _submitPassword() {
    this._clearErrors();
    const password = document.getElementById('twoFaInput').value;
    if (!password) {
      this._showError('err2fa', 'Введите облачный пароль');
      return;
    }

    this._setBtnLoading('btnVerify2fa', true);
    try {
      await this.api.confirmPassword(this.loginId, password);
      await this._finish();
    } catch (e) {
      this._showError('err2fa', e.message);
    } finally {
      this._setBtnLoading('btnVerify2fa', false);
    }
  }

  async _finish() {
    this._goTo('loading');
    await this.onFinished();
    document.getElementById('sessionChip').textContent = this.phone;
    this._goTo('success');
  }
}
