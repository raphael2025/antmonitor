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

**Ubuntu in einer Zeile** (git/Python vorher nicht nötig):

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

Installiert Pakete, lädt den Code (git oder Quell-Tarball), legt venv und Konfiguration an, fragt das admin-Passwort ab
(Enter = generieren) und startet den Dienst `antmonitor`. Panel: `http://<IP>:8800`.
Direkt an die Cloud anbinden: `… | sudo bash -s -- --connect-code - --site-name Standort-1`
(Code im Cloud-Panel „🔌 Standorte anbinden“). Erneut ausführen = Menü (Update/Reparatur/Deinstallation/Konto).
Langsames GitHub: `GH_PROXY=<Mirror-Präfix>/` (nur vertrauenswürdige Mirrors).

**Windows in einer Zeile** (PowerShell, ohne Admin):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

Code nach `%LOCALAPPDATA%\AntMonitor\antmonitor`, Desktop-Verknüpfung, Start von `run.bat`.

**Danach:** ⚙ Segmente → Subnetze → speichern & scannen. Cloud: ☁ Cloud-Meldung → **Verbindungscode** einfügen →
Übernehmen → Standortname → aktivieren → speichern. „🔌 Verbindung testen“ und die Statuszeile zeigen, ob gemeldet wird.
Eine Cloud ohne Domain (`https://IP:Port`) braucht den **Zertifikat-Fingerabdruck**; er steckt im Code.

**Weniger Fehlalarme auf großen Standorten:** ein Miner offline oder mit Null-Hashrate wird nach 20 s mit längerem Timeout
erneut abgefragt und erst dann (in derselben Runde) gemeldet; ein komplett ausgefallenes Segment wird nicht erneut abgefragt und sofort
als ein Alarm gemeldet (`alerts.offline_confirm_sec` / `zero_confirm_sec` = 240 wartet zusätzlich die nächste Runde ab), Übertemperatur nach 2 Runden, ein Massenausfall
ergibt einen einzigen Standort-Alarm. Hängt die Miner-Weboberfläche, wird die Hashrate über Port 4028 gelesen.

Siehe [SECURITY](../SECURITY.md).

---

## Bedienung

viewer / ops / admin. Übersicht & Alarme → Miner wählen für LED/Reboot → Reparatur stummschaltet Offline. Reboot-Limits pro IP.

---

## Customizing

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9
