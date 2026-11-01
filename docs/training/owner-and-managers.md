# AI CCTV dashboard: guide for owners and managers

For the training session at the end of shadow mode (plan 9.6), about one hour, on the
client's own computers and phones. Print it or share it after the session. The Arabic
version follows.

## Before the session

Every person has their invitation email, has set a password, and (owners) has set up the
authenticator app for 2-step sign-in. Each manager sees only their branches.

## 1. Signing in

- Open the dashboard address and sign in with your email and password. Owners also enter
  the 6-digit code from the authenticator app.
- If you use the operational system, you can open the dashboard from there without
  signing in again.
- Language: **العربية / English** at the top. Your choice is remembered on that device.
- Forgot your password: **Forgot your password?** on the sign-in page. Lost your phone: ask the
  owner to reset your 2-step sign-in.
- You are signed out after 8 hours without use.

## 2. Overview: how is today going?

**Monitoring › Overview** shows each branch today so far: events and how many are not
reviewed, kitchen compliance issues, cashier and after-hours issues, staff attendance
(on time, late, absent), customers and the busiest hour, whether cameras and the AI box
are working, and the latest events. It updates every minute. A feature that is switched
off for a branch has no section.

## 3. Events: what happened, and was it real?

- **Monitoring › Live events**: new events appear by themselves. Click one to see the
  clip and the snapshot.
- Mark each event: **Valid** (it happened), **False alarm** (it didn't), or **Reviewed**
  (real, but no problem). Add a note when it helps. Your answers make the system better:
  false alarms are used to tune it.
- **Monitoring › Daily review**: the day's events nobody has reviewed, one at a time.
  Keys: `V` valid, `F` false alarm, `R` reviewed, `S` skip.
- **Monitoring › Search**: find any event by date, camera, type, severity or review
  result; export the list (CSV).
- **Live view** and **Playback** (under Monitoring): watch a camera now, or a recording from the NVR.
- Every time you watch a clip, live view or a recording, it is written in the audit log.
  This protects staff and customers; only watch what you need.

## 4. Alerts: who is told, and how

**Settings › Alerts** (each person sets their own):

- Choose which events, which branches and the minimum severity. **Critical** is always
  sent; other alerts can be paused at night (quiet hours).
- Channels: in the dashboard, browser notifications, and **WhatsApp** (enter your number
  and agree to receive messages).
- Messages never contain pictures: they have a link that opens the event after sign-in.
- Use **Test** to check a message arrives.
- You also choose here whether to get a link when the daily, weekly or monthly report is
  ready.
- **On your phone**: open the dashboard on the phone and install it as an app (**My account
  › Phone app**: on Android the Install button or Chrome's menu › Install app; on iPhone
  Safari › Share › Add to Home Screen). Then turn on browser notifications here, and alerts
  arrive as phone notifications.

## 5. Reports

**Monitoring › Reports**: daily, weekly and monthly reports per branch: events,
compliance, attendance per person, customers by hour and alert times. Download as PDF or
Excel in English or Arabic. Reports include attendance per person: share them only with
people who should see it.

## 6. Settings (owners and managers)

- **Working hours** of each branch: after-hours security depends on them. Update them
  for holidays and Ramadan.
- **Features**: switch features on or off for the whole company, a branch or one camera,
  and change thresholds. Ask the support team before changing thresholds: a small change
  can mean many more or many fewer alerts.
- **Zones** are drawn by the support team or the installer; tell them if a camera was
  moved.

## 7. Staff and attendance

- **People › Staff**: add staff with their employee code and shift.
- **Face recognition only with consent**: the staff member signs the consent form, the
  manager records the date, then enrols the face (3-5 photos). Without a recorded consent
  the dashboard refuses. When someone leaves or withdraws consent, set it in the
  dashboard: the face data is deleted at once.
- **People › Attendance**: each day's check-in and check-out. Correct a mistake (for
  example a day off) with a note; the correction is logged.

## 8. Owners only

- **Administration › Users**: invite people, choose their role (owner, branch manager,
  reviewer, installer) and branches; deactivate people who leave, at once.
- **Administration › Audit log**: who signed in, watched or downloaded what, and changed
  which settings. Export it as CSV.
- **Administration › Integrations**: API keys and webhooks for other software (the
  operational system). Set up with the support team.

## 9. Help

Support team: [name, phone, WhatsApp, email]. Tell us when a camera is moved, an NVR is
replaced, the internet or power is down for long, or alerts look wrong.

---

# لوحة تحكم كاميرات الذكاء الاصطناعي: دليل المالكين والمديرين

لجلسة التدريب في نهاية وضع الظل (الخطة 9.6)، نحو ساعة، على أجهزة العميل وهواتفه.

## قبل الجلسة

لدى كل شخص بريد الدعوة، وقد عيّن كلمة مرور، و(المالكون) أعدّوا تطبيق المصادقة لتسجيل
الدخول بخطوتين. يرى كل مدير فروعه فقط.

## 1. تسجيل الدخول

- افتح عنوان لوحة التحكم وسجّل الدخول ببريدك وكلمة المرور. يُدخل المالكون أيضًا الرمز
  المكوّن من 6 أرقام من تطبيق المصادقة.
- إذا كنت تستخدم نظام التشغيل، يمكنك فتح لوحة التحكم منه دون تسجيل الدخول مرة أخرى.
- اللغة: **العربية / English** في الأعلى، ويُحفظ اختيارك على ذلك الجهاز.
- نسيت كلمة المرور: **نسيت كلمة المرور؟** في صفحة الدخول. فقدت هاتفك: اطلب من المالك إعادة
  ضبط التحقق بخطوتين.
- يُسجَّل خروجك بعد 8 ساعات دون استخدام.

## 2. نظرة عامة: كيف يسير اليوم؟

**المراقبة › نظرة عامة** تعرض كل فرع اليوم حتى الآن: الأحداث وعدد ما لم يُراجع، ومخالفات
المطبخ، ومخالفات الكاشير وما بعد ساعات العمل، وحضور الموظفين (في الموعد، متأخر، غائب)،
والعملاء وأكثر ساعة ازدحامًا، وهل تعمل الكاميرات وجهاز الذكاء الاصطناعي، وآخر الأحداث.
تُحدَّث كل دقيقة. الميزة المتوقفة في فرع لا يظهر لها قسم.

## 3. الأحداث: ماذا حدث، وهل هو حقيقي؟

- **المراقبة › الأحداث المباشرة**: تظهر الأحداث الجديدة تلقائيًا. انقر على حدث لرؤية المقطع
  والصورة.
- علّم كل حدث: **صحيح** (حدث فعلًا)، أو **إنذار كاذب** (لم يحدث)، أو **تمت المراجعة** (حقيقي
  لكن لا مشكلة). أضف ملاحظة عند الحاجة. إجاباتك تحسّن النظام: تُستخدم الإنذارات الكاذبة
  لضبطه.
- **المراقبة › المراجعة اليومية**: أحداث اليوم التي لم يراجعها أحد، واحدًا تلو الآخر.
  المفاتيح: `V` صحيح، `F` إنذار كاذب، `R` تمت المراجعة، `S` تخطٍّ.
- **المراقبة › البحث**: ابحث عن أي حدث بالتاريخ أو الكاميرا أو النوع أو الخطورة أو نتيجة
  المراجعة، وصدّر القائمة (CSV).
- **البث المباشر** و**التسجيلات** (تحت المراقبة): شاهد كاميرا الآن، أو تسجيلًا من جهاز التسجيل.
- كل مشاهدة لمقطع أو بث مباشر أو تسجيل تُكتب في سجل التدقيق. هذا يحمي الموظفين والعملاء؛
  شاهد ما تحتاجه فقط.

## 4. التنبيهات: من يُبلَّغ وكيف

**الإعدادات › التنبيهات** (يضبطها كل شخص لنفسه):

- اختر الأحداث والفروع وأدنى خطورة. **الحرجة** تُرسَل دائمًا؛ ويمكن إيقاف غيرها ليلًا
  (ساعات الهدوء).
- القنوات: داخل لوحة التحكم، وإشعارات المتصفح، و**واتساب** (أدخل رقمك ووافق على استلام
  الرسائل).
- لا تحتوي الرسائل على صور أبدًا: فيها رابط يفتح الحدث بعد تسجيل الدخول.
- استخدم **تجربة** للتأكد من وصول الرسالة.
- تختار هنا أيضًا استلام رابط عند جاهزية التقرير اليومي أو الأسبوعي أو الشهري.
- **على هاتفك**: افتح لوحة التحكم على الهاتف وثبّتها كتطبيق (**حسابي › تطبيق الهاتف**: على
  أندرويد زر التثبيت أو قائمة Chrome › تثبيت التطبيق؛ على آيفون Safari › مشاركة › إضافة إلى
  الشاشة الرئيسية). ثم فعّل إشعارات المتصفح هنا، فتصل التنبيهات كإشعارات على الهاتف.

## 5. التقارير

**المراقبة › التقارير**: تقارير يومية وأسبوعية وشهرية لكل فرع: الأحداث، والالتزام، والحضور
لكل شخص، والعملاء حسب الساعة، وأزمنة التنبيه. نزّلها PDF أو Excel بالعربية أو الإنجليزية.
تتضمن التقارير الحضور لكل شخص: شاركها فقط مع من يحق له الاطلاع.

## 6. الإعدادات (المالكون والمديرون)

- **ساعات العمل** لكل فرع: يعتمد عليها أمن ما بعد ساعات العمل. حدّثها في العطل ورمضان.
- **الميزات**: شغّل الميزات أو أوقفها للشركة كلها أو لفرع أو لكاميرا، وغيّر الحدود. اسأل
  فريق الدعم قبل تغيير الحدود: تغيير صغير قد يعني تنبيهات أكثر بكثير أو أقل بكثير.
- **المناطق** يرسمها فريق الدعم أو الفني؛ أخبرهم إذا نُقلت كاميرا.

## 7. الموظفون والحضور

- **الأشخاص › الموظفون**: أضف الموظفين برقمهم الوظيفي ووردياتهم.
- **التعرف على الوجه بالموافقة فقط**: يوقّع الموظف نموذج الموافقة، ويسجّل المدير التاريخ، ثم
  يسجّل الوجه (3-5 صور). بدون موافقة مسجّلة ترفض لوحة التحكم ذلك. عند مغادرة شخص أو سحب
  موافقته، سجّل ذلك في لوحة التحكم: تُحذف بيانات الوجه فورًا.
- **الأشخاص › الحضور**: الحضور والانصراف لكل يوم. صحّح الخطأ (مثل يوم إجازة) مع ملاحظة؛
  يُسجَّل التصحيح.

## 8. للمالكين فقط

- **الإدارة › المستخدمون**: ادعُ الأشخاص، واختر دورهم (مالك، مدير فرع، مراجع، فني) وفروعهم؛
  وأوقف حساب من يغادر فورًا.
- **الإدارة › سجل التدقيق**: من سجّل الدخول، وشاهد أو نزّل ماذا، وغيّر أي إعدادات. صدّره CSV.
- **الإدارة › التكاملات**: مفاتيح API و Webhooks للبرامج الأخرى (نظام التشغيل)، تُعدّ مع
  فريق الدعم.

## 9. المساعدة

فريق الدعم: [الاسم، الهاتف، واتساب، البريد الإلكتروني]. أخبرنا عند نقل كاميرا، أو استبدال
جهاز التسجيل، أو انقطاع الإنترنت أو الكهرباء لمدة طويلة، أو إذا بدت التنبيهات غير صحيحة.
