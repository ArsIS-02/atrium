<p align="center">
  <img src="logo.svg" width="96" alt="Atrium">
</p>

# Atrium

Панель и редактор каталога сервисов домашней лаборатории. Flask-приложение, которое:

- показывает плитки сервисов с живыми статусами (точка: работает, нужен вход, недоступен);
- даёт добавлять, менять и удалять сервисы через веб-интерфейс;
- генерирует конфиг Caddy и поддерживает загрузку своих иконок;
- проверяет бэкенд и сертификат прямо из формы;
- позволяет поставить фон панели: один для всех.

Редактирование защищено PIN-кодом из 9 цифр. Просмотр открыт всем, кто имеет доступ к панели.

## Быстрый старт

```bash
podman build -t atrium .
mkdir -p data && cp services.example.yaml data/services.yaml && cp generate.py data/
podman run --rm -it -v "$PWD/data:/data:z" atrium flask set-pin
podman run -d --name atrium -p 5000:5000 -v "$PWD/data:/data:z" \
  -e CADDY_IMPORT=/data/Caddyfile.apps atrium
```

Откройте `http://localhost:5000`. Для редактирования войдите по PIN.

Подробности развёртывания: [deploy/README.md](deploy/README.md).

## Структура

- `app/` — приложение Flask, шаблоны и статика
- `generate.py` — генератор конфига Caddy из `services.yaml`
- `services.example.yaml` — пример каталога сервисов
- `deploy/` — примеры для systemd и инструкция по развёртыванию

## Личные данные

В репозитории нет адресов, паролей и ключей. Файлы `services.yaml`, `admin.json`, `secret.key`, `settings.json`, `backgrounds/`, `icons/` и каталог Caddy с сертификатами создаются на сервере и в `.gitignore`.

## Требования

Python 3.11, Flask, PyYAML, requests, cryptography (см. `requirements.txt`). Для работы с Caddy нужен Caddy 2 с локальным центром сертификатов (`tls internal`).
