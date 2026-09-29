# AntMonitor · Руководство (RU)

**Язык:** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

Репозиторий: https://github.com/raphael2025/antmonitor  
Облачный обзор: https://github.com/raphael2025/cloud-overview

---

## 1. Введение

**AntMonitor** — система **локального массового мониторинга и управления** для майнеров **BITMAIN ANTMINER** и гидроконтейнеров **ANTBOX** в локальной сети фермы.

Возможности:

- Сканирование майнеров и контейнеров: хешрейт, температура, мощность, worker
- Веб-панель: сводка, список, стойки, аварии, отчёты по клиентам
- Плановые опросы + аварии (офлайн / перегрев / падение хешрейта / охлаждение); голос, Telegram
- Удалённо: индикатор поиска, перезагрузка (лимиты 24ч/интервал; авто-reboot при офлайне **выкл. по умолчанию**)
- Мультисайтовая отправка в облако; в планах **Agent Skills + MCP**

Подходит для хостинга / колокации — один ПК Windows или Ubuntu на площадке.

---

## 2. Развёртывание

### 2.1 Требования

- Python 3.8+
- Доступ к подсетям майнеров в LAN
- Желательно отдельный хост или Ubuntu + systemd

### 2.2 Быстрый старт

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py    # http://0.0.0.0:8800
```

Windows: **`run.bat`** (автоперезапуск при сбое).

### 2.3 Обязательная настройка (`config.yaml`)

1. Подсети — `scan.segments` или веб «Сегменты» (admin)
2. Пользователи — `auth.users` (`python auth.py <пароль>`)
3. Пароли майнеров — `scan.passwords`
4. Опционально: Telegram, `cloud`, whitelist пулов

См. [SECURITY.md](../SECURITY.md).

### 2.4 Пример systemd

```ini
[Unit]
Description=AntMonitor
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/antmonitor
ExecStart=/opt/antmonitor/.venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Панель: `http://127.0.0.1:8800` или LAN IP из логов.

---

## 3. Как пользоваться

### 3.1 Роли

| Роль | Доступ |
|---|---|
| viewer | только просмотр |
| ops | просмотр + сканы + команды |
| admin | всё |

Слабый пароль только **предупреждает**, права не блокирует.

### 3.2 Ежедневная работа

1. Открыть панель — онлайн / хешрейт / аварии
2. Admin: задать сегменты → полный скан
3. ~5 мин быстрый скан реестра; ~1 ч полное обнаружение
4. Голосовые аварии / Telegram
5. Выбрать майнеры → LED / reboot (с подтверждением)
6. Верхняя панель: авто-reboot, интервал и параллелизм (admin; по умолчанию выкл.)
7. Ремонт / снятие с учёта в панели команд

### 3.3 Лимиты

- ≤4 успешных reboot / IP / 24ч, интервал ≥15 мин
- Смена пула в UI убрана; API остаётся (нужен allowlist)
- 1000 TH = 1 PH

### 3.4 Кастомизация

WeChat: **`raphael-2024`** · Telegram: https://t.me/+W3J9yAypNgpjNTk9
