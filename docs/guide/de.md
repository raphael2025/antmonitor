# AntMonitor · Anleitung (DE)

**Sprache:** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

Repo: https://github.com/raphael2025/antmonitor  
Cloud-Übersicht: https://github.com/raphael2025/cloud-overview

---

## 1. Einführung

**AntMonitor** ist ein **lokales Massen-Monitoring- und Ops-System** für **BITMAIN ANTMINER**-Miner und **ANTBOX**-Hydrocontainer im Farm-LAN.

Funktionen:

- Scan von Minern & Coolern: Hashrate, Temp, Leistung, Worker
- Web-UI: Übersicht, Liste, Rack-Ansicht, Alarme, Kundenreports
- Geplante Scans + Alarme (Offline / Überhitzung / niedrige Hashrate / Kühlfehler); Sprache, Telegram
- Remote: Locate-LED & Reboot (24h-/Intervall-Limits; Auto-Reboot optional, **standardmäßig aus**)
- Multi-Site-Push in die Cloud; Roadmap: **Agent Skills + MCP**

Ideal für Hosting / Colocation — ein Windows- oder Ubuntu-Rechner vor Ort.

---

## 2. Deployment

### 2.1 Voraussetzungen

- Python 3.8+
- LAN-/Routing-Zugang zu den Miner-Subnetzen
- Bevorzugt dedizierter Host oder Ubuntu + systemd

### 2.2 Schnellstart

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py    # http://0.0.0.0:8800
```

Windows: **`run.bat`**.

### 2.3 Pflichtkonfiguration (`config.yaml`)

1. Subnetze — `scan.segments` oder Web-Admin
2. Benutzer — `auth.users` (`python auth.py <passwort>`)
3. Miner-Passwörter — `scan.passwords`
4. Optional: Telegram, `cloud`, Pool-Allowlist

Siehe [SECURITY.md](../SECURITY.md).

### 2.4 systemd-Beispiel

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

Panel: `http://127.0.0.1:8800` oder LAN-IP aus dem Log.

---

## 3. Bedienung

### 3.1 Rollen

| Rolle | Rechte |
|---|---|
| viewer | nur lesen |
| ops | lesen + Scans + Befehle |
| admin | alles |

Schwaches Passwort nur **Hinweis** — sperrt nicht.

### 3.2 Alltag

1. Panel öffnen → Online / Hashrate / Alarme
2. Admin: Segmente setzen → Full-Scan
3. ~5 Min Quick-Scan; ~1 Std Full-Discovery
4. Sprachalarme / Telegram
5. Miner wählen → LED / Reboot (Bestätigung)
6. Kopfzeile: Auto-Reboot, Intervall & Parallelität (Admin; default aus)
7. Reparatur / Ausmustern in der Befehlsleiste

### 3.3 Limits

- ≤4 erfolgreiche Reboots / IP / 24h, ≥15 Min Abstand
- Pool-Wechsel-UI entfernt; API bleibt (Allowlist nötig)
- 1000 TH = 1 PH

### 3.4 Customizing

WeChat: **`raphael-2024`** · Telegram: https://t.me/+W3J9yAypNgpjNTk9
