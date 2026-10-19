# Развёртывание (пример)

Все пути и имена ниже — плейсхолдеры. Подставьте свои.

- `<HOME>` — домашний каталог пользователя, от которого работает Podman
- `<DATA>` — каталог с данными панели (`services.yaml`, `admin.json`, `secret.key`, `backgrounds/`, `settings.json`). Вне репозитория
- `<CADDY_CONFIG>` — каталог, который смонтирован в контейнер Caddy и откуда он читает `Caddyfile.apps`
- `<ZONE>` — домен сервисов, например `apps.example.lan`

## 1. Сборка образа

```bash
podman build -t localhost/atrium:latest <HOME>/atrium
```

## 2. Данные

В `<DATA>` кладём `services.yaml` (за основу берём `services.example.yaml`) и `generate.py` из репозитория.

Важно при обновлении: `generate.py` в `<DATA>` должен быть из той же версии, что и образ. Иначе опция «Передавать адрес как есть» не попадёт в конфиг Caddy. Заменить можно без остановки панели; конфиг появится после следующего сохранения сервиса.

## 3. Пароль администратора

```bash
podman run --rm -it -v <DATA>:/data:z localhost/atrium:latest flask set-password
```

Не меньше 9 знаков, до 128, латинские буквы, цифры и знаки. Хранится только хеш, в `<DATA>/admin.json`. Пароль, заданный в предыдущей версии PIN-кодом, продолжит работать без изменений.

Если используете вход через Caddy («Закрыть входом через Caddy»), задайте учётные данные в контейнере Caddy:

```bash
podman exec caddy caddy hash-password        # ввести пароль, получить хеш
# добавить в окружение контейнера Caddy:
#   PANEL_USER=<логин>
#   PANEL_PASS_HASH=<хеш>
```

Без этих переменных Caddy отказывается загрузить конфиг, и перезагрузка не проходит. Сервисы без входа через Caddy на это не влияют.

## 4. Запуск

```bash
podman run -d --name atrium \
  -e ZONE=<ZONE> \
  -p 5000:5000 \
  -v <DATA>:/data:z \
  -v <CADDY_CONFIG>:/caddy:z \
  localhost/atrium:latest
```

## 5. Перезагрузка Caddy после изменений

После сохранения в панели Caddy нужно перезагрузить вручную:

```bash
podman exec caddy caddy validate --config /etc/caddy/Caddyfile
podman exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Автоматическая перезагрузка через `caddy-reload.path` и `caddy-reload.service` — необязательный пример, его нужно проверить на своей системе.

## 6. Git и личные данные

В `<DATA>` тоже можно вести git, но с таким `.gitignore`:

```
secret.key
admin.json
settings.json
backgrounds/
```

`services.yaml` с реальными адресами в публичный репозиторий не отправляем.
