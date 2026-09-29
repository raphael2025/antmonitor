# AntMonitor · دليل عربي

<div dir="rtl">

**اللغة / Language:** [中文](zh.md) · [English](en.md) · [Русский](ru.md) · [Español](es.md) · [Deutsch](de.md) · [العربية](ar.md)

المستودع: https://github.com/raphael2025/antmonitor  
نظرة السحابة: https://github.com/raphael2025/cloud-overview

---

## 1. مقدمة

**AntMonitor** نظام **مراقبة وإدارة جماعية على الشبكة المحلية** لأجهزة تعدين **BITMAIN ANTMINER** وحاويات التبريد المائي **ANTBOX**.

القدرات:

- مسح المعدّنات والحاويات: الهاشريت، الحرارة، الطاقة، اسم العامل
- لوحة ويب: ملخص، قائمة، رفوف، تنبيهات، تقارير العملاء
- فحوصات مجدولة + تنبيهات (انقطاع / سخونة / انخفاض هاشريت / أعطال تبريد)؛ صوت، Telegram
- عن بُعد: ضوء تحديد الموقع وإعادة التشغيل (حدود 24 ساعة/فاصل؛ إعادة تلقائية اختيارية، **مغلقة افتراضيًا**)
- إرسال متعدد المواقع إلى السحابة؛ الخطة: **Agent Skills + MCP**

مناسب للاستضافة / الكولوكيشن — جهاز Windows أو Ubuntu في الموقع.

---

## 2. النشر

### 2.1 المتطلبات

- Python 3.8+
- وصول شبكي لشبكات المعدّنات
- يُفضّل جهاز مخصص أو Ubuntu مع systemd

### 2.2 بداية سريعة

```bash
git clone https://github.com/raphael2025/antmonitor.git
cd antmonitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python server.py    # http://0.0.0.0:8800
```

على Windows استخدم **`run.bat`**.

### 2.3 إعداد إلزامي (`config.yaml`)

1. الشبكات الفرعية — `scan.segments` أو لوحة الأدمن
2. المستخدمون — `auth.users` (`python auth.py <كلمة-المرور>`)
3. كلمات مرور المعدّنات — `scan.passwords`
4. اختياري: Telegram، `cloud`، قائمة تجمعات مسموحة

انظر [SECURITY.md](../SECURITY.md).

### 2.4 مثال systemd

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

اللوحة: `http://127.0.0.1:8800` أو IP الشبكة من السجل.

---

## 3. طريقة الاستخدام

### 3.1 الأدوار

| الدور | الصلاحية |
|---|---|
| viewer | قراءة فقط |
| ops | قراءة + مسح + أوامر |
| admin | الكل |

كلمة المرور الضعيفة **تنبيه فقط** — لا تقفل العمليات.

### 3.2 العمل اليومي

1. افتح اللوحة → متصل / هاشريت / تنبيهات
2. أدمن: عيّن الشبكات → مسح كامل
3. ~5 دقائق مسح سريع؛ ~ساعة اكتشاف كامل
4. تنبيهات صوتية / Telegram
5. اختر معدّنات → LED / إعادة تشغيل (مع تأكيد)
6. الشريط العلوي: إعادة تلقائية، الفاصل والتوازي (أدمن؛ افتراضيًا مغلق)
7. صيانة / إزالة من شريط الأوامر

### 3.3 القيود

- ≤4 إعادة تشغيل ناجحة / IP / 24 ساعة، فاصل ≥15 دقيقة
- واجهة تغيير المجمع أُزيلت؛ الـ API يبقى (يحتاج allowlist)
- 1000 TH = 1 PH

### 3.4 التخصيص والدعم

WeChat: **`raphael-2024`** · Telegram: https://t.me/+W3J9yAypNgpjNTk9

</div>
