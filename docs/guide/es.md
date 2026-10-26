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

**Ubuntu, en una línea** (no hace falta git ni Python previos):

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

Instala dependencias, descarga el código (git o tarball), crea venv y configuración, pide la contraseña de admin
(Enter = generarla) y arranca el servicio `antmonitor`. Panel: `http://<IP>:8800`.
Conectar a la nube en la instalación: `… | sudo bash -s -- --connect-code - --site-name Sitio-1`
(código en el panel de la nube «🔌 Alta de sitios»). Volver a ejecutar = menú (actualizar/reparar/desinstalar/restablecer).
GitHub lento: `GH_PROXY=<prefijo de espejo>/` (solo espejos de confianza).

**Windows, en una línea** (PowerShell, sin administrador):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

Código en `%LOCALAPPDATA%\AntMonitor\antmonitor`, acceso directo en el escritorio y arranque de `run.bat`.

**Después:** ⚙ Segmentos → subredes → guardar y escanear. Nube: ☁ Envío a la nube → pegar el **código de conexión** →
Aplicar → nombre del sitio → activar → guardar. «🔌 Probar conexión» y la línea de estado muestran si el envío funciona.
Una nube sin dominio (`https://IP:puerto`) requiere la **huella del certificado**; ya va en el código.

**Menos falsas alarmas en sitios grandes:** desconexión/segmento/sitio completo y hashrate cero se confirman en la
ronda siguiente (`alerts.offline_confirm_sec`, `zero_confirm_sec` = 240 s), sobretemperatura 2 rondas seguidas y una
caída masiva da una sola alarma de sitio. Si la web del minero se cuelga, el hashrate se lee por el puerto 4028.

Ver [SECURITY](../SECURITY.md).

---

## Uso

viewer / ops / admin. Resumen y alertas → seleccionar equipos para LED/reboot → reparación silencia offline. Reboots limitados por IP.

---

## Personalización

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9
