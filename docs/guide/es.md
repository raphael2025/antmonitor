# AntMonitor · Español

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**Sitio** https://github.com/raphael2025/antmonitor · **Cloud** https://github.com/raphael2025/antmonitor-cloud

---

## Introducción

AntMonitor es monitoreo y operación **en LAN**: visibilidad del parque, alertas fiables, acciones remotas con confirmación y límites.

- Panel / lista / racks / alertas / contenedores / informes
- Escaneos programados; offline, bajo hashrate, calor, cooling (voz + Telegram)
- LED de localización y reboot por lotes (con límites); auto-reboot opcional (**off**)
- Multi-sitio → AntMonitor Cloud; roadmap Agent Skills + MCP

---

## Despliegue

**Ubuntu:** `sudo bash install.sh`（安装/更新/修复/卸载/重置账号）

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py
```

Windows: `run.bat`. Ver [SECURITY](../SECURITY.md).

---

## Uso

viewer / ops / admin. Resumen y alertas → seleccionar equipos para LED/reboot → reparación silencia offline. Reboots limitados por IP.

---

## Personalización

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9
