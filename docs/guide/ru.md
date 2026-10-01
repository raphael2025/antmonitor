# AntMonitor · Русский

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**Площадка** https://github.com/raphael2025/antmonitor · **Облако** https://github.com/raphael2025/antmonitor-cloud

---

## Введение

AntMonitor — **локальный** мониторинг и управление фермой: обзор хешрейта, надёжные аварии, удалённые действия с подтверждением и лимитами.

- Панель / список / стойки / аварии / контейнеры / отчёты
- Плановые опросы; офлайн, падение хешрейта, перегрев, охлаждение (голос + Telegram)
- LED поиска и пакетный reboot (с лимитами); авто-reboot опционально (**выкл.**)
- Мультисайт → AntMonitor Cloud; в планах Agent Skills + MCP

---

## Развёртывание

**Ubuntu:** `sudo bash install.sh`（安装/更新/修复/卸载/重置账号）

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py
```

Windows: `run.bat`. См. [SECURITY](../SECURITY.md).

---

## Использование

viewer / ops / admin. Смотрите сводку и аварии, выбирайте майнеры для LED/reboot, «ремонт» глушит офлайн-аварии. Лимиты reboot на IP.

---

## Кастом

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9
