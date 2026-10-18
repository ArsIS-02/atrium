"""MyHomeLaboratory: панель и редактор каталога сервисов домашней сети."""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

import click
import requests
import yaml
from cryptography import x509
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

HOMELAB_DIR = Path(os.environ.get('HOMELAB_DIR', '/data'))
SERVICES_YAML = Path(os.environ.get('SERVICES_YAML', HOMELAB_DIR / 'services.yaml'))
GENERATE = Path(os.environ.get('GENERATE_SCRIPT', HOMELAB_DIR / 'generate.py'))
CADDY_IMPORT = Path(os.environ.get('CADDY_IMPORT', '/caddy/Caddyfile.apps'))
ZONE = os.environ.get('ZONE', 'lab.example')
STATUS_INTERVAL = int(os.environ.get('STATUS_INTERVAL', '30'))

# Где Caddy слушает HTTPS с сервера (для проверки сертификата по SNI)
CADDY_HOST = os.environ.get('CADDY_HOST', 'host.containers.internal')
CADDY_PORT = int(os.environ.get('CADDY_PORT', '443'))

# Вход администратора по PIN (9 цифр). Хранится только хеш.
ADMIN_FILE = Path(os.environ.get('ADMIN_FILE', HOMELAB_DIR / 'admin.json'))
SECRET_FILE = HOMELAB_DIR / 'secret.key'
PIN_RE = re.compile(r'^\d{9}$')
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60

ICON_BASE = 'https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/'
ICONS_URL = 'https://raw.githubusercontent.com/walkxcode/dashboard-icons/main/tree.json'
ICONS_CACHE = Path(os.environ.get('ICONS_CACHE', '/tmp/icons-cache.json'))
DEFAULT_ICON = 'application-default-icon'

# Своя коллекция иконок: файлы лежат в ICONS_DIR и отдаются по /icons/<имя>
ICONS_DIR = Path(os.environ.get('ICONS_DIR', HOMELAB_DIR / 'icons'))
LOCAL_PREFIX = 'local:'
ICON_MAX_BYTES = 512 * 1024
ICON_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}\.(png|svg|webp)$')

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
SCHEMES = ('http', 'https')
BACKEND_RE = re.compile(r'^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9._-]+):(\d{1,5})$')
SCHEME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*://')

requests.packages.urllib3.disable_warnings()

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
)

_store_lock = threading.Lock()


def _load_secret() -> str:
    """Ключ подписи сессий хранится в файле, чтобы вход не слетал при перезапуске."""
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    try:
        if SECRET_FILE.exists():
            key = SECRET_FILE.read_text(encoding='utf-8').strip()
            if key:
                return key
        key = secrets.token_urlsafe(48)
        SECRET_FILE.write_text(key, encoding='utf-8')
        os.chmod(SECRET_FILE, 0o600)
        return key
    except OSError:
        return secrets.token_urlsafe(48)


app.secret_key = _load_secret()


# --- вход администратора -----------------------------------------------------

def _pin_hash() -> str | None:
    try:
        return json.loads(ADMIN_FILE.read_text(encoding='utf-8')).get('pin_hash')
    except (OSError, ValueError, AttributeError):
        return None


def pin_is_set() -> bool:
    return bool(_pin_hash())


def check_pin(pin: str) -> bool:
    h = _pin_hash()
    return bool(h) and bool(PIN_RE.match(pin)) and check_password_hash(h, pin)


def is_admin() -> bool:
    return session.get('admin') is True and pin_is_set()


_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def _login_blocked(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < LOGIN_WINDOW]
        _fails[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILS


def _login_failed(ip: str) -> None:
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


def _login_reset(ip: str) -> None:
    with _fails_lock:
        _fails.pop(ip, None)


def csrf_token() -> str:
    tok = session.get('csrf')
    if not tok:
        tok = secrets.token_urlsafe(24)
        session['csrf'] = tok
    return tok


@app.before_request
def _check_csrf():
    if request.method != 'POST':
        return None
    sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token', '')
    if not sent or not secrets.compare_digest(sent, session.get('csrf', '')):
        if request.path.startswith('/api/'):
            return jsonify(ok=False, msg='Сессия устарела: обновите страницу'), 400
        abort(400)
    return None


@app.context_processor
def _template_globals():
    return {'csrf_token': csrf_token, 'is_admin': is_admin(), 'pin_set': pin_is_set(),
            'bg': background_info()}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_admin():
            return view(*args, **kwargs)
        if request.path.startswith('/api/'):
            return jsonify(ok=False, msg='Нужен вход администратора (PIN)'), 401
        flash('Чтобы менять сервисы, войдите по PIN', 'error')
        return redirect(url_for('login', next=request.full_path.rstrip('?')))
    return wrapped


@app.cli.command('set-pin')
def set_pin_cmd():
    """Задаёт PIN администратора (ровно 9 цифр)."""
    while True:
        pin = click.prompt('Новый PIN (9 цифр)', hide_input=True)
        if not PIN_RE.match(pin):
            click.echo('Нужно ровно 9 цифр, попробуйте ещё раз.')
            continue
        if click.prompt('Повторите PIN', hide_input=True) != pin:
            click.echo('PIN не совпадает, попробуйте ещё раз.')
            continue
        break
    ADMIN_FILE.write_text(json.dumps({'pin_hash': generate_password_hash(pin),
                                      'updated': int(time.time())}), encoding='utf-8')
    os.chmod(ADMIN_FILE, 0o600)
    click.echo('PIN сохранён. Старые сессии останутся действительны до конца их срока (8 ч).')


# --- хранилище ---------------------------------------------------------------

def load() -> dict:
    if not SERVICES_YAML.exists():
        return {'services': []}
    data = yaml.safe_load(SERVICES_YAML.read_text(encoding='utf-8')) or {}
    data.setdefault('services', [])
    return data


def save(data: dict) -> None:
    header = ('# Единый источник истины: сервисы домашней лаборатории.\n'
              '# Правится через MyHomeLaboratory (можно и вручную).\n'
              '# Поля: name, title, group, backend, scheme (http|https), path, insecure, auth, icon, description, href\n\n')
    tmp = SERVICES_YAML.with_name(SERVICES_YAML.name + '.tmp')
    tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                                           default_flow_style=False), encoding='utf-8')
    tmp.replace(SERVICES_YAML)


def regenerate() -> tuple[bool, str]:
    """Запускает generate.py и кладёт Caddyfile туда, где его читает Caddy."""
    try:
        r = subprocess.run(['python3', str(GENERATE)], cwd=GENERATE.parent,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f'generate.py не запустился: {e}'
    if r.returncode != 0:
        return False, 'generate.py завершился с ошибкой:\n' + (r.stderr or r.stdout).strip()
    src = GENERATE.parent / 'generated' / 'Caddyfile.apps'
    if not CADDY_IMPORT.parent.is_dir():
        return False, f'нет каталога {CADDY_IMPORT.parent}'
    CADDY_IMPORT.write_bytes(src.read_bytes())
    return True, 'Конфиг Caddy обновлён. Чтобы адрес заработал, перезагрузите Caddy (см. Справку)'


def apply_change(mutate, success: str) -> tuple[bool, str]:
    """Меняет services.yaml, генерирует конфиги и откатывает всё при ошибке."""
    with _store_lock:
        old_text = SERVICES_YAML.read_text(encoding='utf-8') if SERVICES_YAML.exists() else None
        data = load()
        err = mutate(data)
        if err:
            return False, err
        save(data)
        ok, msg = regenerate()
        if not ok:
            if old_text is not None:
                SERVICES_YAML.write_text(old_text, encoding='utf-8')
            return False, msg + '\nИзменения отменены.'
        return True, f'{success}. {msg}'


def normalize_backend(raw: str) -> tuple[str | None, str | None]:
    """Backend — только host:port. Схема и путь задаются отдельными полями."""
    b = (raw or '').strip()
    if not b:
        return None, 'Нужен backend host:port'
    if SCHEME_RE.match(b):
        return None, 'В поле Backend не пишите http:// или https:// — схему выберите полем «Схема»'
    b = b.rstrip('/').strip()
    if '/' in b:
        return None, 'В поле Backend только host:port. Путь вроде /nagios пишется в поле «Префикс пути»'
    m = BACKEND_RE.match(b)
    if not m:
        return None, 'Backend — это host:port, например 192.168.0.10:8080'
    if not 1 <= int(m.group(2)) <= 65535:
        return None, 'Порт должен быть от 1 до 65535'
    return b, None


def svc_from_form(form) -> tuple[dict | None, str | None]:
    name = form.get('name', '').strip().lower()
    backend, err = normalize_backend(form.get('backend', ''))
    if err:
        return None, err
    svc = {
        'name': name,
        'title': form.get('title', '').strip(),
        'group': form.get('group', '').strip() or 'Other',
        'backend': backend,
        'scheme': form.get('scheme', 'http'),
    }
    if not NAME_RE.match(name):
        return None, 'Адрес: только a–z, 0–9 и дефис, без дефиса в начале'
    if not svc['title']:
        return None, 'Нужно название'
    if svc['scheme'] not in SCHEMES:
        return None, 'Неверная схема'
    path = form.get('path', '').strip()
    if path:
        if not path.startswith('/'):
            return None, 'Префикс пути должен начинаться с /'
        svc['path'] = path
    if form.get('insecure') == 'on':
        svc['insecure'] = True
    if form.get('auth') == 'on':
        svc['auth'] = True
    icon = form.get('icon', '').strip()
    if icon.startswith(LOCAL_PREFIX):
        if not ICON_NAME_RE.match(icon[len(LOCAL_PREFIX):]) or \
                not (ICONS_DIR / icon[len(LOCAL_PREFIX):]).is_file():
            return None, 'Своя иконка не найдена, загрузите её заново'
        svc['icon'] = icon
    elif icon:
        svc['icon'] = icon.removesuffix('.png')
    description = form.get('description', '').strip()
    if description:
        svc['description'] = description
    href = form.get('href', '').strip()
    if href:
        if not href.startswith(('http://', 'https://')):
            return None, 'Ссылка должна начинаться с http:// или https://'
        svc['href'] = href
    return svc, None


def _known_groups(services: list[dict]) -> list[str]:
    return sorted({s.get('group', 'Other') for s in services})


# --- статусы ------------------------------------------------------------------

_status: dict[str, dict] = {}
_status_lock = threading.Lock()


def classify(code: int) -> str:
    if 200 <= code < 400:
        return 'ok'
    if code in (401, 403):
        return 'warn'
    return 'bad'


def probe(svc: dict, timeout: float = 3.0) -> dict:
    """Проверяет бэкенд напрямую, без DNS-имени поддомена."""
    url = f"{svc.get('scheme', 'http')}://{svc['backend']}{svc.get('path') or '/'}"
    try:
        r = requests.get(url, verify=not svc.get('insecure', False), timeout=timeout,
                         allow_redirects=False, stream=True)
        code = r.status_code
        r.close()
    except requests.RequestException:
        code = 0
    return {'code': code, 'class': classify(code), 'checked': int(time.time())}


def poll_once() -> None:
    services = load()['services']
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: (s['name'], probe(s)), services))
    with _status_lock:
        _status.clear()
        _status.update(results)


def _poll_forever() -> None:
    while True:
        try:
            poll_once()
        except Exception:  # noqa: BLE001 - фоновый цикл не должен падать
            app.logger.exception('status poll failed')
        time.sleep(STATUS_INTERVAL)


def start_background_polling() -> None:
    threading.Thread(target=_poll_forever, daemon=True, name='status-poller').start()


if os.environ.get('STATUS_POLL', '1') == '1':
    start_background_polling()


# --- сертификат ---------------------------------------------------------------

def check_cert(name: str) -> dict:
    """Подключается к Caddy с нужным SNI и смотрит, какой сертификат он отдаёт."""
    server_name = f'{name}.{ZONE}'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((CADDY_HOST, CADDY_PORT), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_name) as tls:
                der = tls.getpeercert(binary_form=True)
    except ssl.SSLError:
        return {'ok': False, 'state': 'missing',
                'msg': 'Caddy ещё не знает этот адрес, сертификата нет. Сохраните карточку '
                       'и перезагрузите Caddy (команда в Справке), затем проверьте снова.'}
    except OSError:
        return {'ok': False, 'state': 'error',
                'msg': f'Caddy не отвечает на {CADDY_HOST}:{CADDY_PORT}. Проверьте, что контейнер caddy запущен.'}
    if not der:
        return {'ok': False, 'state': 'missing', 'msg': 'Caddy не отдал сертификат'}
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    if server_name not in san:
        names = ', '.join(san) or 'без имени'
        return {'ok': False, 'state': 'mismatch',
                'msg': f'Caddy отдаёт сертификат для другого имени: {names}'}
    expires = cert.not_valid_after_utc
    days = (expires - datetime.datetime.now(datetime.timezone.utc)).days
    msg = f'Сертификат есть, действует до {expires:%d.%m.%Y} ({days} дн.)'
    if 'caddy' in cert.issuer.rfc4514_string().lower():
        msg += '. Выдан локальным центром Caddy: где установлен его корень, предупреждения не будет'
    return {'ok': True, 'state': 'ok', 'msg': msg, 'days': days}


# --- иконки -------------------------------------------------------------------

_icons: list[str] | None = None


def icons_catalog() -> list[str]:
    global _icons
    if _icons is not None:
        return _icons
    try:
        if ICONS_CACHE.exists() and time.time() - ICONS_CACHE.stat().st_mtime < 86400:
            names = json.loads(ICONS_CACHE.read_text(encoding='utf-8'))
        else:
            tree = requests.get(ICONS_URL, timeout=10).json()
            files = tree.get('png', []) if isinstance(tree, dict) else tree
            names = sorted({Path(f).name for f in files
                            if isinstance(f, str) and f.endswith('.png')})
            ICONS_CACHE.write_text(json.dumps(names), encoding='utf-8')
    except (requests.RequestException, ValueError, OSError):
        return []
    _icons = names
    return _icons


def icon_url(icon: str | None) -> str:
    icon = (icon or DEFAULT_ICON).strip()
    if icon.startswith(LOCAL_PREFIX):
        return '/icons/' + icon[len(LOCAL_PREFIX):]
    if not icon.endswith(('.png', '.svg')):
        icon += '.png'
    return ICON_BASE + icon


def _sniff(data: bytes, ext: str) -> bool:
    """Проверяем содержимое, а не только расширение."""
    if ext == 'jpg':
        return data[:3] == b'\xff\xd8\xff'
    if ext == 'png':
        return data[:8] == b'\x89PNG\r\n\x1a\n'
    if ext == 'webp':
        return data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    if ext == 'svg':
        return b'<svg' in data[:4096].lower()
    return False


def _ensure_icons_dir() -> None:
    try:
        ICONS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        app.logger.warning('не удалось создать %s', ICONS_DIR)


_ensure_icons_dir()


@app.template_filter('icon')
def _icon_filter(name):
    return icon_url(name)


# --- фон панели ---------------------------------------------------------------
# Один фон для всех: файл и настройки лежат на сервере, в HOMELAB_DIR.

BG_DIR = HOMELAB_DIR / 'backgrounds'
SETTINGS_FILE = HOMELAB_DIR / 'settings.json'
BG_MAX_BYTES = 3 * 1024 * 1024
BG_NAME_RE = re.compile(r'^bg-\d{10}\.(jpg|png|webp)$')
DIM_DEFAULT = 40


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(SETTINGS_FILE)


def _dim_value(raw) -> int:
    try:
        return max(0, min(80, int(raw)))
    except (TypeError, ValueError):
        return DIM_DEFAULT


def _remove_bg_file(name: str | None) -> None:
    if name and BG_NAME_RE.match(name) and (BG_DIR / name).is_file():
        (BG_DIR / name).unlink()


def background_info() -> dict | None:
    s = load_settings()
    name = s.get('file')
    if not name or not BG_NAME_RE.match(name) or not (BG_DIR / name).is_file():
        return None
    mtime = int((BG_DIR / name).stat().st_mtime)
    return {'url': f'/background/{name}?v={mtime}', 'dim': _dim_value(s.get('dim', DIM_DEFAULT)) / 100}


# --- страницы -----------------------------------------------------------------

@app.get('/')
def index():
    services = load()['services']
    group = request.args.get('group', '')
    visible = [s for s in services if not group or s.get('group') == group]
    return render_template('index.html', services=visible, all_count=len(services),
                           groups=_known_groups(services), current_group=group, zone=ZONE)


@app.route('/add', methods=['GET', 'POST'])
@admin_required
def add():
    services = load()['services']
    if request.method == 'POST':
        svc, err = svc_from_form(request.form)
        if err is None:
            def mutate(data):
                if any(s['name'] == svc['name'] for s in data['services']):
                    return f'Адрес {svc["name"]} уже занят'
                data['services'].append(svc)
                return None
            ok, msg = apply_change(mutate, f'«{svc["title"]}» добавлен')
            if ok:
                flash(msg, 'ok')
                return redirect(url_for('index'))
            err = msg
        flash(err, 'error')
        return render_template('form.html', service=dict(request.form), is_edit=False,
                               original='', zone=ZONE, known_groups=_known_groups(services)), 400
    return render_template('form.html', service={}, is_edit=False, original='', zone=ZONE,
                           known_groups=_known_groups(services))


@app.route('/edit/<name>', methods=['GET', 'POST'])
@admin_required
def edit(name):
    services = load()['services']
    current = next((s for s in services if s['name'] == name), None)
    if current is None:
        flash(f'Сервис {name} не найден', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        svc, err = svc_from_form(request.form)
        if err is None:
            def mutate(data):
                if svc['name'] != name and any(s['name'] == svc['name'] for s in data['services']):
                    return f'Адрес {svc["name"]} уже занят'
                for i, s in enumerate(data['services']):
                    if s['name'] == name:
                        data['services'][i] = svc
                        return None
                return f'Сервис {name} пропал из конфига'
            ok, msg = apply_change(mutate, f'«{svc["title"]}» сохранён')
            if ok:
                flash(msg, 'ok')
                return redirect(url_for('index'))
            err = msg
        flash(err, 'error')
        return render_template('form.html', service=dict(request.form), is_edit=True,
                               original=name, zone=ZONE,
                               known_groups=_known_groups(services)), 400
    return render_template('form.html', service=current, is_edit=True, original=name,
                           zone=ZONE, known_groups=_known_groups(services))


@app.post('/delete/<name>')
@admin_required
def delete(name):
    def mutate(data):
        before = len(data['services'])
        data['services'] = [s for s in data['services'] if s['name'] != name]
        return None if len(data['services']) < before else f'Сервис {name} не найден'
    ok, msg = apply_change(mutate, f'Сервис {name} удалён')
    flash(msg, 'ok' if ok else 'error')
    return redirect(url_for('index'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    nxt = request.values.get('next', '')
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = url_for('index')
    ip = request.remote_addr or '?'
    if request.method == 'POST':
        if _login_blocked(ip):
            flash('Слишком много неверных попыток. Подождите 15 минут.', 'error')
            return render_template('login.html', next=nxt), 429
        pin = re.sub(r'\s', '', request.form.get('pin', ''))
        if check_pin(pin):
            _login_reset(ip)
            session.permanent = True
            session['admin'] = True
            return redirect(nxt)
        _login_failed(ip)
        flash('Неверный PIN', 'error')
        return render_template('login.html', next=nxt), 401
    return render_template('login.html', next=nxt)


@app.post('/logout')
def logout():
    session.pop('admin', None)
    flash('Вы вышли из режима редактирования', 'ok')
    return redirect(url_for('index'))


@app.get('/help')
def help_page():
    return render_template('help.html', zone=ZONE)


@app.get('/background/<fname>')
def serve_background(fname):
    if not BG_NAME_RE.match(fname):
        abort(404)
    return send_from_directory(BG_DIR, fname, max_age=86400)


@app.get('/appearance')
@admin_required
def appearance():
    return render_template('appearance.html', bg=background_info(),
                           dim=_dim_value(load_settings().get('dim', DIM_DEFAULT)))


@app.post('/appearance/upload')
@admin_required
def appearance_upload():
    f = request.files.get('file')
    if f is None or not f.filename:
        flash('Выберите картинку', 'error')
        return redirect(url_for('appearance'))
    ext = Path(f.filename).suffix.lower().lstrip('.')
    ext = 'jpg' if ext == 'jpeg' else ext
    data = f.stream.read(BG_MAX_BYTES + 1)
    if len(data) > BG_MAX_BYTES:
        flash('Картинка больше 3 МБ', 'error')
        return redirect(url_for('appearance'))
    if ext not in ('jpg', 'png', 'webp') or not _sniff(data, ext):
        flash('Нужна картинка JPG, PNG или WebP', 'error')
        return redirect(url_for('appearance'))
    BG_DIR.mkdir(parents=True, exist_ok=True)
    name = f'bg-{int(time.time())}.{ext}'
    (BG_DIR / name).write_bytes(data)
    s = load_settings()
    old = s.get('file')
    s['file'] = name
    save_settings(s)
    if old != name:
        _remove_bg_file(old)
    flash('Фон обновлён', 'ok')
    return redirect(url_for('appearance'))


@app.post('/appearance/settings')
@admin_required
def appearance_settings():
    s = load_settings()
    s['dim'] = _dim_value(request.form.get('dim', DIM_DEFAULT))
    save_settings(s)
    flash('Затемнение сохранено', 'ok')
    return redirect(url_for('appearance'))


@app.post('/appearance/reset')
@admin_required
def appearance_reset():
    _remove_bg_file(load_settings().get('file'))
    save_settings({})
    flash('Фон убран, вернули стандартное оформление', 'ok')
    return redirect(url_for('appearance'))


# --- API ----------------------------------------------------------------------

@app.get('/api/status')
def api_status():
    with _status_lock:
        return jsonify(dict(_status))


@app.get('/api/icons')
def api_icons():
    q = request.args.get('q', '').strip().lower()
    local = []
    if ICONS_DIR.is_dir():
        for p in sorted(ICONS_DIR.iterdir()):
            if ICON_NAME_RE.match(p.name) and (not q or q in p.name.lower()):
                local.append({'name': LOCAL_PREFIX + p.name, 'url': '/icons/' + p.name, 'source': 'local'})
    names = icons_catalog()
    if q:
        names = [n for n in names if q in n.lower()]
    remote = [{'name': n.removesuffix('.png'), 'url': ICON_BASE + n, 'source': 'remote'} for n in names[:40]]
    return jsonify(local + remote)


@app.get('/icons/<fname>')
def serve_icon(fname):
    if not ICON_NAME_RE.match(fname):
        abort(404)
    return send_from_directory(ICONS_DIR, fname, max_age=86400)


@app.post('/api/icons/upload')
@admin_required
def api_icon_upload():
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify(ok=False, msg='Файл не выбран'), 400
    path = Path(f.filename)
    ext = path.suffix.lower().lstrip('.')
    stem = re.sub(r'[^a-z0-9-]+', '-', path.stem.lower()).strip('-')[:60]
    if not stem or ext not in ('png', 'svg', 'webp'):
        return jsonify(ok=False, msg='Нужен файл PNG, SVG или WebP'), 400
    data = f.stream.read(ICON_MAX_BYTES + 1)
    if len(data) > ICON_MAX_BYTES:
        return jsonify(ok=False, msg='Файл больше 512 КБ'), 400
    if not _sniff(data, ext):
        return jsonify(ok=False, msg='Файл не похож на PNG, SVG или WebP'), 400
    _ensure_icons_dir()
    target = ICONS_DIR / f'{stem}.{ext}'
    tmp = ICONS_DIR / f'.{stem}.{ext}.tmp'
    tmp.write_bytes(data)
    tmp.replace(target)
    name = LOCAL_PREFIX + target.name
    return jsonify(ok=True, name=name, url=icon_url(name))


@app.post('/api/icons/delete/<fname>')
@admin_required
def api_icon_delete(fname):
    if not ICON_NAME_RE.match(fname):
        return jsonify(ok=False, msg='Некорректное имя'), 400
    target = ICONS_DIR / fname
    if not target.is_file():
        return jsonify(ok=False, msg='Файл не найден'), 404
    used = [s['name'] for s in load()['services'] if s.get('icon') == LOCAL_PREFIX + fname]
    if used:
        return jsonify(ok=False, msg='Иконка используется: ' + ', '.join(used)), 409
    target.unlink()
    return jsonify(ok=True)


@app.post('/api/check-backend')
@admin_required
def api_check_backend():
    body = request.get_json(silent=True) or {}
    backend, err = normalize_backend(body.get('backend') or '')
    if err:
        return jsonify(ok=False, msg=err)
    scheme = body.get('scheme', 'http')
    if scheme not in SCHEMES:
        return jsonify(ok=False, msg='неверная схема')
    result = probe({'backend': backend, 'scheme': scheme,
                    'insecure': bool(body.get('insecure'))}, timeout=4)
    if result['code'] == 0:
        return jsonify(ok=False, msg='нет ответа')
    return jsonify(ok=True, code=result['code'])


@app.post('/api/check-cert')
@admin_required
def api_check_cert():
    body = request.get_json(silent=True) or {}
    name = str(body.get('name') or '').strip().lower()
    if not NAME_RE.match(name):
        return jsonify(ok=False, state='error', msg='Сначала укажите адрес: латиница, цифры и дефис')
    return jsonify(check_cert(name))


@app.get('/healthz')
def healthz():
    return 'ok'
