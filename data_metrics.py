"""Shared measurement rules for live APIs; unknown is distinct from a measured zero."""
import math


def measurement(value, signed=False):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and (signed or value >= 0) else None


def live_record(record, fresh=True):
    """Project raw evidence into a current view without modifying stored samples."""
    out = dict(record)
    status = out.get("status")
    if status not in ("online", "offline", "unknown"):
        status = "unknown"
    if not fresh:
        out["last_status"] = status
        status = "unknown"
        out["data_stale"] = True
    out["status"] = status
    for key in ("hr_rt", "hr_avg", "power", "eff", "uptime", "accepted", "rejected", "stale"):
        out[key] = measurement(out.get(key)) if status == "online" else None
    out["temp"] = measurement(out.get("temp"), signed=True) if status == "online" else None
    if status == "offline":
        # Monitoring convention: confirmed offline contributes zero to estimated work.
        # This is not a physical power-meter measurement.
        out["hr_rt"] = out["power"] = 0.0
    return out


def aggregate(records):
    """Full totals require every known machine; expose measured subtotals separately."""
    records = [live_record(r) for r in records]
    online = [r for r in records if r["status"] == "online"]
    offline = sum(r["status"] == "offline" for r in records)
    rates = [r["hr_rt"] for r in records if r["hr_rt"] is not None]
    watts = [r["power"] for r in records if r["power"] is not None]
    hr_complete = len(rates) == len(records)
    power_complete = len(watts) == len(records)
    known_hr = round(sum(rates), 2) if rates or not records else None
    known_kw = round(sum(watts) / 1000, 3) if watts or not records else None
    total_hr = known_hr if hr_complete else None
    total_kw = known_kw if power_complete else None
    fw = {}
    for r in online:
        name = r.get("firmware") or ""
        fw[name] = fw.get(name, 0) + 1
    return {
        "total": len(records), "online": len(online), "offline": offline,
        "unknown": len(records) - len(online) - offline,
        "with_hashrate": sum(r["hr_rt"] is not None for r in online),
        "no_hashrate": sum(r["hr_rt"] is None for r in online),
        "zero_hashrate": sum(r["hr_rt"] == 0 for r in online),
        "total_hashrate_th": total_hr, "known_hashrate_ths": known_hr,
        "total_power_kw": total_kw, "known_power_kw": known_kw,
        "hashrate_complete": hr_complete, "power_complete": power_complete,
        "hashrate_known_machines": len(rates), "power_known_machines": len(watts),
        "avg_hashrate_th": round(total_hr / len(online), 2)
        if hr_complete and online else None,
        # Fleet energy intensity: total watts / total TH/s, including idle online power.
        "avg_efficiency": round(sum(watts) / sum(rates), 2)
        if hr_complete and power_complete and sum(rates) > 0 else None,
        "by_firmware": fw,
    }
