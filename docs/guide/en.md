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

**Ubuntu one-click (recommended):** from the repo root run `sudo bash install.sh`  
Pick a language, then: Install · Update · Repair · Uninstall · Reset username & password. Default path `/opt/antmonitor`, unit `antmonitor`.

```bash
# manual
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py
```

Windows: `run.bat`. Security: [SECURITY](../SECURITY.md).

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

We keep shipping updates — feedback welcome. Roadmap includes **multi-customer management**, Agent Skills + MCP, and more.

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9

[DEVELOPMENT](../DEVELOPMENT.md) · Agent API [EXTERNAL_API](../EXTERNAL_API.md)

## Panel language

Use the language selector on the login page and top bar. Preference is saved in the browser.
