# تقرير تقوية LedgerCore

**التاريخ:** 21 نوفمبر 2026  
**النطاق:** التعديلات الأمنية والمحاسبية، تقوية الواجهة، الاختبارات، وتهيئة CI.

## ملخص التنفيذ

تم تطبيق الإصلاحات المطلوبة في نسخة المشروع، مع إضافة Migration جديدة (`0002_integrity_and_idempotency.py`). الإصلاحات البرمجية موجودة داخل الحزمة المرفقة. بعض اختبارات PostgreSQL وBrowser لا يمكن اعتبارها ناجحة محليًا في بيئة التنفيذ الحالية؛ التفاصيل موضحة في قسم التحقق.

## 1. الإصلاحات الحرجة

### Refresh Token Rotation
- استبدال القراءة ثم الإلغاء المنفصل بتحديث SQL ذري مشروط على `token_hash` و`revoked_at IS NULL` ووقت الانتهاء.
- التوكن الجديد لا يصدر إلا للطلب الذي استهلك السجل فعلًا.
- إعادة استخدام التوكن الملغى تؤدي إلى إلغاء عائلة التوكنات بالكامل.
- إضافة اختبار PostgreSQL متزامن لطلبين يستخدمان التوكن القديم نفسه.

### CSRF في Logout
- إضافة `verify_csrf` إلى `POST /auth/logout`.
- الاختبار يتحقق أن الطلب الذي لا يحمل CSRF صالحًا يرجع `403` ولا يلغي Refresh Token.

### سلامة الـLedger على مستوى قاعدة البيانات
- إضافة جدول `journal_entry_seals` لختم القيد بعد إدخال كل سطوره.
- في PostgreSQL، Deferred Constraint Triggers تتحقق عند `COMMIT` أن كل قيد جديد مختوم، يحتوي على سطرين على الأقل، وأن إجمالي Debit يساوي Credit.
- Trigger يمنع إدخال سطور جديدة لقيد مختوم.
- تمت إضافة قفل على صف القيد نفسه في مساري إدخال السطر وإنشاء الختم؛ هذا يعالج سباقًا محتملًا بين إدخال متزامن وختم القيد.
- حماية الختم من التعديل والحذف.
- SQLite يطبق منع الإضافة بعد الختم والتحقق عند إنشاء الختم، لكنه لا يوفر Deferred Constraint Triggers مماثلة لـPostgreSQL؛ ضمان التوازن النهائي في الإنتاج يعتمد على PostgreSQL.
- Migration تفحص القيود التاريخية أولًا، وترفض الترقية إذا وجدت قيدًا غير متوازن أو لا يحتوي على سطرين على الأقل بدل ختم بيانات قديمة غير سليمة.
- أضيفت اختبارات Raw SQL لحالات عدم التوازن، وغياب الختم، ومحاولة إضافة سطر بعد الختم. اختبارات الـcommit الفعلية مخصصة لـPostgreSQL.

### Idempotency للعمليات المالية
- إضافة جدول `idempotency_records` مع قيد فريد على `(actor, operation, key)` وبصمة SHA-256 للطلب.
- يتم حفظ نتيجة العملية المالية ومفتاحها في المعاملة نفسها؛ إعادة نفس المفتاح ونفس الطلب ترجع النتيجة السابقة، وإعادة المفتاح مع بيانات مختلفة ترجع `409`.
- أصبح Header باسم `Idempotency-Key` مطلوبًا في:
  - `POST /api/v1/journal-entries`
  - `POST /api/v1/journal-entries/{id}/reverse`
  - `POST /api/v1/invoices`
  - `POST /api/v1/invoices/{id}/payments`
  - `POST /api/v1/invoices/{id}/void`
- الواجهة تحفظ المفتاح المؤقت في `sessionStorage` وتعيد استخدامه بعد أخطاء الشبكة أو فشل الخادم، وتمسحه بعد اكتمال ناجح أو خطأ عميل نهائي.

### إعدادات Cookies والإنتاج
- `COOKIE_SECURE` أصبح آمنًا افتراضيًا في الإنتاج.
- التطبيق يرفض `ENVIRONMENT=production` مع `COOKIE_SECURE=false`.
- `.env.example` يضبط `COOKIE_SECURE=true`، ويوضح أن `false` للتطوير المحلي عبر HTTP فقط.

### Dependencies وSecurity Audit
- تحديث تثبيتات Backend وDevelopment إلى إصدارات أحدث متوافقة ومثبتة بإصدارات محددة.
- إضافة `pip-audit -r requirements-dev.txt` إلى CI.
- إضافة Ruff وMypy إلى CI بجانب الاختبارات.
- لم أتمكن من تشغيل `pip-audit` محليًا أو تنزيل الاعتماديات الجديدة بسبب منع الاتصال بمستودع الحزم في بيئة التنفيذ. وجود خطوة الفحص في CI لا يعني أنها اجتازت بعد؛ يلزم تشغيل GitHub Actions بعد رفع التعديلات.

## 2. التحسينات الإضافية

- **Accounting Periods:** إنشاء صف الفترة عند الحاجة ثم قفله بـ`SELECT FOR UPDATE`؛ الإغلاق وإعادة الفتح والتحقق قبل الترحيل يتسلسلون على الصف نفسه، بما في ذلك أول استخدام للفترة.
- **Audit Trail:** التحقق من عدد سجلات السلسلة مقابل آخر قيمة محفوظة في `DocumentSequence`. أضيفت Triggers تمنع حذف سجل التسلسل أو إنقاص قيمته، حتى لا يمكن إخفاء حذف آخر سجل بمجرد إنقاص العداد عبر SQL عادي.
- **Frontend:** إعادة تصميم CSS لتكون الواجهة أكثر تماسكًا واستجابة للشاشات الصغيرة، مع تحسين حالات الجداول والنماذج والمؤشرات والتنقل، وإضافة `aria-live` و`aria-busy` ورسالة واضحة لحساب المشاهدة فقط.
- **Read-only Demo:** إضافة إعداد اختياري `DEMO_VIEWER_EMAIL` و`DEMO_VIEWER_PASSWORD`. عند ضبطهما يُنشأ حساب `VIEWER` فقط، ولا توجد بيانات دخول تجريبية افتراضية مضمنة في الكود. لم يتم نشر Live Demo أو إنشاء رابط عام.
- **Playwright:** إضافة اختبارات Browser لتسجيل دخول مستخدم إداري وإنشاء قيد متوازن، وللتأكد من أن حساب VIEWER لا يرى أدوات التعديل على شاشة هاتف. الاختبارات تحفظ لقطات Dashboard والقيد المنشور عند نجاحها.
- **CI:** إضافة Jobs منفصلة للاختبارات على PostgreSQL 16، وPlaywright/Chromium، وDocker Compose smoke test، وفحص تطابق Alembic migrations مع SQLAlchemy metadata. كما يتم رفع لقطات المتصفح كـArtifact عند توليدها.
- **README:** توثيق البنية، متطلبات Idempotency، خيارات التشغيل المحلي والإنتاج، حساب العرض، والاختبارات.

## 3. نتائج التحقق الفعلية

| الفحص | النتيجة | الملاحظة |
|---|---:|---|
| اختبارات Backend المحلية | **81 passed / 5 skipped** | آخر تشغيل مكتمل: 26.92 ثانية (دون احتساب خطوة coverage الإضافية) |
| Coverage | **93.21%** | أعلى من حد المشروع 85% |
| Alembic SQLite upgrade/downgrade + parity | ناجح | شمل Migration الجديدة |
| `alembic check` بعد الترقية | ناجح | `No new upgrade operations detected.` على SQLite |
| `python -m compileall` | ناجح | ملفات التطبيق والاختبارات وAlembic |
| `node --check app/static/app.js` | ناجح | فحص Syntax للواجهة |
| PostgreSQL raw-SQL constraints والتزامن | غير منفذ محليًا | لا توجد خدمة PostgreSQL أو Docker/psql في البيئة الحالية؛ 5 اختبارات PostgreSQL-only ظلت skipped |
| Playwright / Chromium | لم يكتمل محليًا | سياسة Chromium في بيئة التنفيذ تمنع فتح أي URL (`URLBlocklist: *`)؛ تم تجهيز تشغيله على GitHub Actions |
| Ruff / Mypy / pip-audit | غير منفذ محليًا | الأدوات غير مثبتة وتعذر تنزيل الحزم بسبب قيود الشبكة |
| GitHub Actions | لم يتم تشغيله بعد | يحتاج رفع هذه النسخة إلى المستودع ثم فحص نتيجة الـWorkflow |

الـ81 اختبارًا الناجحة شملت اختبارات التطبيق المتاحة محليًا بعد استثناء ملف Hypothesis (`tests/test_properties.py`) لأن Hypothesis غير مثبت في بيئة التنفيذ الحالية. خمس حالات تخطّاها pytest كانت مخصصة لـPostgreSQL وتتطلب قاعدة البيانات الفعلية. لذلك لا أعتبر اختبارات التزامن أو Deferred Triggers مثبتة محليًا قبل نجاحها على PostgreSQL في CI.

## 4. خطوات التشغيل بعد استلام النسخة

1. استبدل ملفات المشروع بالنسخة المعدلة، ثم شغّل `make migrate` محليًا أو `docker compose up --build`؛ صورة Docker تطبق `alembic upgrade head` قبل تشغيل API.
2. في بيئة الإنتاج، اضبط `SECRET_KEY` و`POSTGRES_PASSWORD`، واترك `ENVIRONMENT=production` و`COOKIE_SECURE=true`.
3. ارفع التعديلات إلى GitHub وتأكد من نجاح Jobs: PostgreSQL tests، Playwright، `pip-audit`، Ruff، Mypy، migration parity، وDocker smoke test.
4. للحصول على حساب عرض قراءة فقط، اضبط `DEMO_VIEWER_EMAIL` و`DEMO_VIEWER_PASSWORD` في إعدادات النشر، ثم انشر التطبيق عبر HTTPS.

**الحالة النهائية:** الإصلاحات المطلوبة ونظام الاختبارات والتهيئة تم تنفيذها في الكود. اعتمادها للإنتاج يظل مشروطًا بنجاح PostgreSQL وPlaywright وSecurity Audit في GitHub Actions؛ هذه النتائج لم يتم اختلاقها أو احتسابها كنجاح قبل تشغيلها.
