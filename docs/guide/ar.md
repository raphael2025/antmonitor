# AntMonitor · العربية

<div dir="rtl">

[中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

**الموقع** https://github.com/raphael2025/antmonitor · **السحابة** https://github.com/raphael2025/antmonitor-cloud

---

## مقدمة

AntMonitor مراقبة وتشغيل **على الشبكة المحلية**: رؤية الأسطول، تنبيهات موثوقة، إجراءات عن بُعد مع تأكيد وحدود.

- لوحة / قائمة / رفوف / تنبيهات / حاويات / تقارير
- فحوصات مجدولة؛ انقطاع، انخفاض هاشريت، سخونة، تبريد (صوت + Telegram)
- ضوء تحديد وإعادة تشغيل جماعية (محدودة)؛ إعادة تلقائية اختيارية (**مغلقة**)
- مواقع متعددة → AntMonitor Cloud؛ الخطة Agent Skills + MCP

---

## النشر

```bash
git clone https://github.com/raphael2025/antmonitor.git && cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py
```

Windows: `run.bat`. انظر [SECURITY](../SECURITY.md).

---

## الاستخدام

viewer / ops / admin. الملخص والتنبيهات → اختيار أجهزة لـ LED/إعادة التشغيل → الصيانة تكتم انقطاع الشبكة. حدود إعادة التشغيل لكل IP.

---

## التخصيص

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9

</div>
