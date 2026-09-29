# AntMonitor · Deutsch

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**Standort** https://github.com/raphael2025/antmonitor · **Cloud** https://github.com/raphael2025/antmonitor-cloud

---

## Einführung

AntMonitor ist **lokales** Farm-Monitoring & Ops: Überblick, zuverlässige Alarme, Remote-Aktionen mit Bestätigung und Limits.

- Dashboard / Liste / Racks / Alarme / Container / Reports
- Geplante Scans; Offline, niedrige Hashrate, Überhitzung, Kühlung (Sprache + Telegram)
- Locate-LED & Batch-Reboot (limitiert); Auto-Reboot optional (**aus**)
- Multi-Site → AntMonitor Cloud; Roadmap Agent Skills + MCP

---

## Deployment

**Ubuntu:** `sudo bash install.sh`（安装/更新/修复/卸载/重置账号）

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py
```

Windows: `run.bat`. Siehe [SECURITY](../SECURITY.md).

---

## Bedienung

viewer / ops / admin. Übersicht & Alarme → Miner wählen für LED/Reboot → Reparatur stummschaltet Offline. Reboot-Limits pro IP.

---

## Customizing

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9
