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

**Needs:** Ubuntu 20.04+ or Windows 10/11 that can reach the miner subnets. No git/Python needed beforehand.

**Ubuntu, one line (recommended):**

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

Installs packages, fetches the code (git if available, otherwise a source tarball), creates the venv and config,
asks for the admin password (Enter = generate a strong one, shown at the end) and starts the `antmonitor`
systemd service. Panel: `http://<host>:8800`. Connect to the cloud during install with the code from the cloud
panel (🔌 Site onboarding): `… | sudo bash -s -- --connect-code - --site-name Site-1`.
Run the same command again for the menu (update / repair / uninstall / reset account), or pass `update` etc.
Slow GitHub: set `GH_PROXY=<mirror prefix>/` (trusted mirrors only). No GitHub access: unzip the source and run `sudo bash install.sh` inside it (Python packages still need PyPI or a mirror).

**Windows, one line** (PowerShell, no admin needed):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

Downloads to `%LOCALAPPDATA%\AntMonitor\antmonitor`, adds a desktop shortcut and starts `run.bat`
(installs Python/venv/deps on first run and enables web updates). The old way — unzip and double-click `run.bat` — still works.

**After install:** log in as admin → ⚙ Segments → add subnets → save & full scan. Cloud: ☁ Cloud reporting →
paste the **connect code** → Apply → site name → enable → save. "🔌 Test connection" checks URL/certificate/token,
and the last report status (time of last success or the reason it failed) is shown right there.
A cloud without a domain (`https://IP:port`) needs the **certificate fingerprint**; the connect code includes it.
Saved tokens are never shown again in full (last 4 chars only); leave the field empty to keep the token.

<details><summary>Manual install</summary>

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python auth.py passwd admin
python server.py
```
</details>

Security: [SECURITY](../SECURITY.md).

**Fewer false alarms on large sites (on by default).** Switch reboots, STP/ARP churn, a hung miner web UI or a 0 reading
while switching pools usually pass within a minute or two, so alerts now need confirmation across rounds
(`config.yaml > alerts`): `offline_confirm_sec: 240` (offline / segment / site-wide), `zero_confirm_sec: 240`,
`overheat_rounds: 2` (readings ≤0 °C or >150 °C are dropped as broken sensors), `unresponsive_rounds: 3`
(port open but API silent), `site_down_ratio: 0.5` + `site_down_min: 50` (one "site-wide outage" alert instead of
dozens), `container_offline_rounds: 3`, `scan.reconfirm_delay_sec: 20`; each probe waits up to 3 s (`scan.online_timeout`/`gate_timeout`, values below 3 are raised to 3). If the miner web UI hangs while cgminer keeps
mining, hashrate is read from port 4028 instead of reporting it offline.

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
