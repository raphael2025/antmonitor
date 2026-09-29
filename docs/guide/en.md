# AntMonitor · English guide

**Language:** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

Repo: https://github.com/raphael2025/antmonitor  
Cloud overview: https://github.com/raphael2025/cloud-overview

---

## 1. Intro

**AntMonitor** is a **LAN bulk monitoring & ops** system for **BITMAIN ANTMINER** miners and **ANTBOX** hydro containers.

What you get:

- Scan miners & coolers on the LAN; read hashrate, temp, power, worker names
- Web UI: summary, list, rack map, alerts, customer reports
- Scheduled scans + offline / overheat / low-hashrate / cooler-fault alerts (voice, Telegram)
- Remote locate LED & reboot (24h / interval rate limits; optional auto-reboot on offline, **off by default**)
- Multi-site push to cloud overview; roadmap: **Agent Skills + MCP** for smarter ops

Best for hosting / colocation farms — one Windows or Ubuntu box on site.

---

## 2. Deploy

### 2.1 Requirements

- Python 3.8+
- Same LAN (or routed) access to miner subnets
- Prefer a dedicated PC or Ubuntu + systemd

### 2.2 Quick start

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit segments, users, passwords
python server.py                     # http://0.0.0.0:8800
```

On Windows you can use **`run.bat`** (auto-restart on crash).

### 2.3 Must-configure (`config.yaml`)

1. **Subnets** — `scan.segments` or Web **Segments** (admin) → `segments.json`
2. **Users** — `auth.users` (hash via `python auth.py <new-password>`)
3. **Miner passwords** — `scan.passwords` (stock Digest; often root/root)
4. Optional: Telegram, `cloud` ingest, `control.pool_allowlist`

See [docs/SECURITY.md](../SECURITY.md).

### 2.4 Ubuntu systemd (example)

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

Panel: `http://127.0.0.1:8800` locally; use the LAN IP printed in startup logs.

---

## 3. How to use

### 3.1 Roles

| Role | Access |
|---|---|
| viewer | read-only |
| ops | read + scan + remote commands |
| admin | everything (segments, settings, audit, updates) |

Weak passwords only **prompt** a change — they do **not** lock ops. Change them ASAP.

### 3.2 Daily ops

1. Open the panel → check online count / hashrate / alerts
2. Admin: Segments → save miner ranges → full discovery scan
3. Default: ~5 min **quick** roster scan; ~1 h **full** discovery
4. Enable voice alerts (top-right); optional Telegram
5. Select miners → locate LED / reboot (destructive actions need confirm)
6. Top bar: auto-reboot switch, batch delay & concurrency (admin; default off)
7. Repair / remove from command bar (repair silences offline alerts)

### 3.3 Limits

- Reboot rate limit: ≤4 successful reboots / IP / rolling 24h, ≥15 min apart
- Pool-change UI removed; API remains (needs pool allowlist)
- Hashrate units: 1000 TH = 1 PH

### 3.4 Custom & support

Custom reports, integrations, RBAC, multi-site policy, Agent/MCP, etc.:

- WeChat: search **`raphael-2024`**
- Telegram: https://t.me/+W3J9yAypNgpjNTk9

Deep docs: [DEVELOPMENT](../DEVELOPMENT.md) · [INTERNAL_API](../INTERNAL_API.md) · [EXTERNAL_API](../EXTERNAL_API.md)
