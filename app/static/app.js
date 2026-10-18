// MyHomeLaboratory: живые статусы, поиск, редактор
(() => {
  const STATUS_MS = 10000;
  const LABEL = { ok: 'Работает', warn: 'Нужен вход', bad: 'Недоступен', pending: 'Проверяется…' };

  // Токен CSRF для всех POST-запросов из скриптов
  const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const withCsrf = (opts = {}) => ({ ...opts, headers: { 'X-CSRF-Token': CSRF, ...(opts.headers || {}) } });

  // Поиск по карточкам без перезагрузки страницы
  const search = document.getElementById('search');
  const cards = [...document.querySelectorAll('.card')];
  const noMatch = document.getElementById('no-match');
  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      let shown = 0;
      for (const c of cards) {
        const hit = !q || c.dataset.search.includes(q);
        c.classList.toggle('is-hidden', !hit);
        if (hit) shown++;
      }
      if (noMatch) noMatch.hidden = shown > 0 || cards.length === 0;
    });
  }

  // Живые статусы: опрашиваем сервер и плавно меняем точки
  const dots = cards.map(c => [c.dataset.name, c.querySelector('[data-status]')]);
  function applyStatus(data) {
    for (const [name, dot] of dots) {
      if (!dot) continue;
      const s = data[name];
      const cls = s ? s.class : 'pending';
      dot.classList.remove('ok', 'warn', 'bad', 'pending');
      dot.classList.add(cls);
      dot.title = s ? `${LABEL[cls]} · HTTP ${s.code || 'нет ответа'}` : LABEL.pending;
    }
  }
  async function pollStatus() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (r.ok) applyStatus(await r.json());
    } catch (_) { /* сеть недоступна: оставляем прошлые статусы */ }
  }
  if (dots.length) {
    pollStatus();
    setInterval(pollStatus, STATUS_MS);
  }

  // Подтверждение удаления
  document.querySelectorAll('form[data-confirm]').forEach(f => {
    f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // Уведомления пропадают сами
  document.querySelectorAll('.toast').forEach((t, i) => {
    setTimeout(() => t.classList.add('leave'), 4000 + i * 300);
    setTimeout(() => t.remove(), 4600 + i * 300);
  });

  // Редактор
  const form = document.getElementById('svc-form');
  if (!form) return;

  const zone = form.dataset.zone;
  const original = form.dataset.original;
  const nameInput = form.elements.name;
  const preview = document.getElementById('addr-preview');
  const renameWarn = document.getElementById('rename-warn');
  nameInput.addEventListener('input', () => {
    const v = nameInput.value.trim().toLowerCase();
    if (preview) preview.textContent = (v || 'имя') + '.' + zone;
    if (renameWarn) renameWarn.hidden = !(original && v && v !== original);
  });

  // Иконки из dashboard-icons
  const iconInput = document.getElementById('icon-input');
  const iconPreview = document.getElementById('icon-preview');
  const iconResults = document.getElementById('icon-results');
  const DEFAULT_ICON = 'application-default-icon';
  const LOCAL = 'local:';
  const iconUrl = (n) => {
    if (n.startsWith(LOCAL)) return '/icons/' + n.slice(LOCAL.length);
    const file = /\.(png|svg)$/.test(n) ? n : n + '.png';
    return 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/' + file;
  };

  const iconUploadMsg = document.getElementById('icon-upload-msg');

  const iconButton = (i, local) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.title = i.name.replace(LOCAL, '');
    const img = new Image();
    img.src = i.url; img.alt = b.title; img.loading = 'lazy';
    b.append(img);
    b.addEventListener('click', () => {
      iconInput.value = i.name;
      iconPreview.src = i.url;
      iconResults.replaceChildren();
    });
    if (local) {
      const del = document.createElement('span');
      del.className = 'icon-del';
      del.textContent = '×';
      del.title = 'Удалить файл';
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        const file = i.name.slice(LOCAL.length);
        if (!confirm(`Удалить иконку ${file}?`)) return;
        const r = await fetch('/api/icons/delete/' + encodeURIComponent(file), withCsrf({ method: 'POST' }));
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { iconUploadMsg.textContent = d.msg || 'не удалось удалить'; return; }
        iconUploadMsg.textContent = 'удалено';
        searchIcons(iconInput.value.trim());
      });
      b.append(del);
    }
    return b;
  };

  const section = (title, items, local) => {
    if (!items.length) return null;
    const wrap = document.createElement('div');
    const h = document.createElement('h4');
    h.textContent = title;
    const grid = document.createElement('div');
    grid.className = 'icon-grid';
    grid.append(...items.map(i => iconButton(i, local)));
    wrap.append(h, grid);
    return wrap;
  };

  const fillIcons = (items) => {
    const local = items.filter(i => i.source === 'local');
    const remote = items.filter(i => i.source !== 'local');
    iconResults.replaceChildren(
      ...[section('Свои', local, true), section('dashboard-icons', remote, false)].filter(Boolean)
    );
  };
  const searchIcons = (q) => fetch('/api/icons?q=' + encodeURIComponent(q))
    .then(r => r.json()).then(fillIcons).catch(() => fillIcons([]));

  let iconTimer;
  iconInput.addEventListener('input', () => {
    clearTimeout(iconTimer);
    iconTimer = setTimeout(() => {
      const v = iconInput.value.trim();
      iconPreview.src = iconUrl(v || DEFAULT_ICON);
      searchIcons(v);
    }, 200);
  });
  iconInput.addEventListener('focus', () => searchIcons(iconInput.value.trim()));

  // Загрузка своей иконки
  const iconFile = document.getElementById('icon-file');
  const iconUploadBtn = document.getElementById('icon-upload-btn');
  iconUploadBtn.addEventListener('click', () => iconFile.click());
  iconFile.addEventListener('change', async () => {
    const f = iconFile.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    iconUploadMsg.textContent = 'загружаю…';
    try {
      const r = await fetch('/api/icons/upload', withCsrf({ method: 'POST', body: fd }));
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.msg || 'ошибка загрузки');
      iconInput.value = d.name;
      iconPreview.src = d.url;
      iconUploadMsg.textContent = 'иконка загружена';
      iconResults.replaceChildren();
    } catch (e) {
      iconUploadMsg.textContent = e.message;
    }
    iconFile.value = '';
  });

  // Проверка бэкенда до сохранения
  const checkBtn = document.getElementById('check-backend');
  const checkOut = document.getElementById('check-result');
  checkBtn.addEventListener('click', async () => {
    checkOut.textContent = 'проверяю…';
    checkOut.className = 'check-result';
    try {
      const r = await fetch('/api/check-backend', withCsrf({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          backend: form.elements.backend.value,
          scheme: form.elements.scheme.value,
          insecure: form.elements.insecure.checked,
        }),
      }));
      const d = await r.json();
      if (!d.ok && d.msg && d.msg !== 'нет ответа') {
        checkOut.textContent = d.msg;
      } else {
        checkOut.textContent = d.ok ? `HTTP ${d.code}, отвечает` : 'нет ответа' + (d.msg ? ': ' + d.msg : '');
      }
      checkOut.className = 'check-result ' + (d.ok ? 'ok' : 'bad');
    } catch (_) {
      checkOut.textContent = 'ошибка запроса';
      checkOut.className = 'check-result bad';
    }
  });

  // Проверка сертификата Caddy для адреса сервиса
  const certBtn = document.getElementById('check-cert');
  const certOut = document.getElementById('cert-result');
  certBtn.addEventListener('click', async () => {
    certOut.textContent = 'проверяю…';
    certOut.className = 'check-result';
    try {
      const r = await fetch('/api/check-cert', withCsrf({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nameInput.value.trim().toLowerCase() }),
      }));
      const d = await r.json().catch(() => ({}));
      certOut.textContent = d.msg || 'нет ответа';
      certOut.className = 'check-result ' + (d.ok ? 'ok' : 'bad');
    } catch (_) {
      certOut.textContent = 'ошибка запроса';
      certOut.className = 'check-result bad';
    }
  });
})();
