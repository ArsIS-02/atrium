# Развёртывание (пример)

Все пути и имена ниже — плейсхолдеры. Подставьте свои.

- `<HOME>` — домашний каталог пользователя, от которого работает Podman
- `<DATA>` — каталог с данными панели (`services.yaml`, `admin.json`, `secret.key`, `backgrounds/`, `settings.json`). Вне репозитория
- `<CADDY_CONFIG>` — каталог, который смонтирован в контейнер Caddy и откуда он читает `Caddyfile.apps`
- `<ZONE>` — домен сервисов, например `apps.example.lan`

## 1. Сборка образа

```bash
podman build -t localhost/myhomelab:latest <HOME>/MyHomeLaboratory
```

## 2. Данные

В `<DATA>` кладём `services.yaml` (за основу берём `services.example.yaml`) и `generate.py` из репозитория.

## 3. PIN администратора

```bash
podman run --rm -it -v <DATA>:/data:z localhost/myhomelab:latest flask set-pin
```

Хранится только хеш, в `<DATA>/admin.json`.

## 4. Запуск

```bash
podman run -d --name myhomelab \
  -e ZONE=<ZONE> \
  -p 5000:5000 \
  -v <DATA>:/data:z \
  -v <CADDY_CONFIG>:/caddy:z \
  localhost/myhomelab:latest
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
