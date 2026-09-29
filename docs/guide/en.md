# AntMonitor · English

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**Site** https://github.com/raphael2025/antmonitor · **Cloud** https://github.com/raphael2025/antmonitor-cloud

---

## Intro

AntMonitor is **on-prem LAN** monitoring & ops for mining farms: clear fleet visibility, reliable alerts, gated remote actions.

- Dashboard / list / rack map / alerts / containers / customer reports
- Scheduled scans; offline, low hashrate, overheat, cooler faults (voice + Telegram)
- Locate LED & batch reboot (rate-limited); optional auto-reboot (**off** by default)
- Multi-site push to AntMonitor Cloud; roadmap: Agent Skills + MCP

For hosting / colocation — one Windows or Ubuntu box on site.

---

## Deploy

**Needs:** Python 3.8+, reachability to miner subnets.

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml    # set segments & users
python server.py                      # http://<IP>:8800
```

Windows: `run.bat`. Production: systemd. Security: [SECURITY](../SECURITY.md).

As admin, configure subnets and run a full discovery once.

---

## Use

| Role | Access |
|---|---|
| viewer | read-only |
| ops | scans + remote commands |
| admin | segments / settings / audit / updates |

Daily: watch summary & alerts → select machines for locate/reboot → mark repair to silence alerts.  
Reboots are rate-limited per IP. Weak passwords only prompt a change.

---

## Custom

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9

[DEVELOPMENT](../DEVELOPMENT.md) · Agent API [EXTERNAL_API](../EXTERNAL_API.md)
