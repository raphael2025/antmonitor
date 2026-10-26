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

**Ubuntu بسطر واحد** (لا حاجة لتثبيت git أو Python مسبقًا):

```bash
curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
```

يثبّت الحزم، ويجلب الشيفرة (git أو أرشيف المصدر)، وينشئ venv والإعدادات، ويطلب كلمة مرور admin
(Enter = توليد كلمة قوية)، ويشغّل خدمة `antmonitor`. اللوحة: `http://<IP>:8800`.
للربط بالسحابة أثناء التثبيت: `… | sudo bash -s -- --connect-code 'AMC1-…' --site-name Site-1`
(الرمز من لوحة السحابة «🔌 ربط المواقع»). تشغيل الأمر مرة أخرى يعرض القائمة (تحديث/إصلاح/إزالة/إعادة تعيين الحساب).
GitHub بطيء: `GH_PROXY=<بادئة المرآة>/` (مرايا موثوقة فقط).

**Windows بسطر واحد** (PowerShell دون صلاحيات مسؤول):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.ps1 | iex"
```

تُنزَّل الشيفرة إلى `%LOCALAPPDATA%\AntMonitor\antmonitor` مع اختصار على سطح المكتب ويُشغَّل `run.bat`.

**بعد التثبيت:** ⚙ الشبكات ← أضف الشبكات الفرعية ← احفظ وافحص. السحابة: ☁ الإرسال للسحابة ← الصق **رمز الربط** ←
تطبيق ← اسم الموقع ← تفعيل ← حفظ. زر «🔌 اختبار الاتصال» وسطر الحالة يوضحان نجاح الإرسال.
السحابة دون نطاق (`https://IP:port`) تحتاج **بصمة الشهادة**، وهي موجودة في رمز الربط.

**إنذارات كاذبة أقل في المواقع الكبيرة:** الانقطاع وانقطاع الشبكة الفرعية والموقع كاملًا والهاش صفر تُؤكَّد في الدورة التالية
(`alerts.offline_confirm_sec` و`zero_confirm_sec` = 240 ث)، والحرارة الزائدة بعد دورتين، والانقطاع الشامل يعطي إنذارًا واحدًا.
إذا تعلّقت واجهة الويب للمُعدِّن يُقرأ الهاش من المنفذ 4028.

راجع [SECURITY](../SECURITY.md).

---

## الاستخدام

viewer / ops / admin. الملخص والتنبيهات → اختيار أجهزة لـ LED/إعادة التشغيل → الصيانة تكتم انقطاع الشبكة. حدود إعادة التشغيل لكل IP.

---

## التخصيص

WeChat **`raphael-2024`** · Telegram https://t.me/+W3J9yAypNgpjNTk9

</div>
