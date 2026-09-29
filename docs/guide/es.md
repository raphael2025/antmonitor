# AntMonitor · Guía (ES)

**Idioma:** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

Repositorio: https://github.com/raphael2025/antmonitor  
Vista cloud: https://github.com/raphael2025/cloud-overview

---

## 1. Introducción

**AntMonitor** es un sistema de **monitoreo y operación masiva en LAN** para mineros **BITMAIN ANTMINER** y contenedores hidro **ANTBOX**.

Qué incluye:

- Escaneo de mineros y coolers: hashrate, temp, potencia, worker
- Panel web: resumen, lista, racks, alertas, informes de clientes
- Escaneos programados + alertas (offline / sobrecalentamiento / bajo hashrate / fallos de cooling); voz, Telegram
- Remoto: LED de localización y reinicio (límites 24h/intervalo; auto-reboot opcional, **off por defecto**)
- Envío multi-sitio a cloud; roadmap: **Agent Skills + MCP**

Ideal para hosting / colocación — un PC Windows o Ubuntu en el sitio.

---

## 2. Despliegue

### 2.1 Requisitos

- Python 3.8+
- Acceso LAN (o enrutado) a las subredes de mineros
- Preferible host dedicado o Ubuntu + systemd

### 2.2 Inicio rápido

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py    # http://0.0.0.0:8800
```

Windows: **`run.bat`**.

### 2.3 Configuración obligatoria (`config.yaml`)

1. Subredes — `scan.segments` o panel admin
2. Usuarios — `auth.users` (`python auth.py <clave>`)
3. Contraseñas de mineros — `scan.passwords`
4. Opcional: Telegram, `cloud`, allowlist de pools

Ver [SECURITY.md](../SECURITY.md).

### 2.4 Ejemplo systemd

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

Panel: `http://127.0.0.1:8800` o la IP LAN del log.

---

## 3. Cómo usar

### 3.1 Roles

| Rol | Acceso |
|---|---|
| viewer | solo lectura |
| ops | lectura + escaneos + comandos |
| admin | todo |

Contraseña débil solo **avisa**; no bloquea operaciones.

### 3.2 Uso diario

1. Abrir panel → online / hashrate / alertas
2. Admin: segmentos → escaneo completo
3. ~5 min escaneo rápido; ~1 h descubrimiento full
4. Alertas de voz / Telegram
5. Seleccionar mineros → LED / reboot (confirmar)
6. Barra superior: auto-reboot, intervalo y concurrencia (admin; off por defecto)
7. Reparación / baja en la barra de comandos

### 3.3 Límites

- ≤4 reinicios OK / IP / 24h, ≥15 min entre ellos
- UI de cambio de pool eliminada; API sigue (requiere allowlist)
- 1000 TH = 1 PH

### 3.4 Personalización

WeChat: **`raphael-2024`** · Telegram: https://t.me/+W3J9yAypNgpjNTk9
