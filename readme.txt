להלן תוכן קובץ **README.md** מוכן לשמירה בתיקייה של הפרויקט. הוא מסביר בדיוק מה למלא באפליקציה כדי להתחבר לאותה מצלמת Dahua בשתי תצורות: **RTSP ישיר** ו-**ONVIF → RTSP**. בסוף הוספתי גם בדיקות מהירות ושורת פתרון תקלות קצרה.

---

# ONVIF/RTSP — Simple App (v3.x) — מדריך התחברות למצלמת Dahua

מסמך זה מסביר מה למלא בשדות של האפליקציה כדי להתחבר למצלמה שנבחנה.

> **דוגמת פרטי המצלמה** (כפי שסופקו):
> IP: `98.173.8.28`
> שם משתמש: `DNA`
> סיסמה: `DNA2025!`
> ONVIF/HTTP Port (מהעמוד של Dahua): `4416`
> **הערה:** אם הפרטים אינם מעודכנים בפועל – תתקבל שגיאת 401/403; במקרה כזה נדרש לאשר מול מי שנותן את הגישה שהקרדנצ׳לס נכונים ושה-RTSP ו-ONVIF מאופשרים במצלמה.

---

## 1) חיבור **RTSP** ישיר (מומלץ כשידוע ה-URL)

במסך **Real camera (RTSP/ONVIF)** בחרו:

* **Mode:** `RTSP`
* **Host/IP:** `98.173.8.28`  *(אפשר גם להדביק URL מלא; האפליקציה תפרק אוטומטית Host/Port/Path)*
* **Username / Password:** `DNA` / `DNA2025!`
* **RTSP port:** `554`  *(ברירת מחדל בדאהואה; אם לא עובד אפשר לנסות `5544`)*
* **RTSP path:** אחת מהאפשרויות הבאות (נסו לפי הסדר):

  1. `/cam/realmonitor?channel=1&subtype=1`  ← תת־זרם (Sub) — קל יותר לרוחב פס
  2. `/cam/realmonitor?channel=1&subtype=0`  ← זרם ראשי (Main)
  3. `/Streaming/Channels/101`               ← סגנון “Hik-style” שלעתים נתמך גם בדאהואה
* השאירו מסומן: **Force RTSP over TCP**
* לחצו **Connect Camera**

**דוגמת URL מלאה שעובדת אם הקרדנצ'לס נכונים:**
`rtsp://DNA:DNA2025!@98.173.8.28:554/cam/realmonitor?channel=1&subtype=1`

> טיפים:
> • אם הודבק URL מלא בשדה Host/IP, האפליקציה תמלא עבורכם את ה-RTSP Port ואת ה-Path.
> • אם מתקבל 404 — זה בדרך כלל נתיב/ערוץ/תת־סטרים לא נכונים. נסו את שלושת הנתיבים למעלה ו/או channel 2–4.

---

## 2) חיבור **ONVIF → RTSP** (האפליקציה שולפת את ה-URL לבד)

במסך **Real camera (RTSP/ONVIF)** בחרו:

* **Mode:** `ONVIF → RTSP`
* **Host/IP:** `98.173.8.28`
* **Username / Password:** `DNA` / `DNA2025!`
* **ONVIF port:** `4416`
  *(זהו ה־HTTP/ONVIF Port לפי העמוד של Dahua. **אל תבלבלו** עם “TCP Port” של Dahua — זה פורט SDK פרטי שלא רלוונטי כאן.)*
* השאירו מסומן: **Force RTSP over TCP**
* לחצו **Connect Camera**

האפליקציה תתחבר ל־ONVIF, תיקח את הפרופיל הראשון, ותבקש ממנו את ה־RTSP URI. אם יש הרשאות ותצורה נכונה — תקבלו תצוגה מיידית, וה־URL יתועד בלוג.

---

## 3) בדיקות מהירות (אופציונלי)

### בדיקת פורט (Windows PowerShell)

```powershell
Test-NetConnection 98.173.8.28 -Port 554
```

אם `TcpTestSucceeded : True` — הפורט פתוח מהעמדה שלכם.

### בדיקת זרם עם ffprobe (ללא timeout flags מיוחדים)

```bash
ffprobe -hide_banner -loglevel error -rtsp_transport tcp \
  -select_streams v -show_entries stream=codec_name -of default=nk=1:nw=1 \
  "rtsp://DNA:DNA2025!@98.173.8.28:554/cam/realmonitor?channel=1&subtype=1"
```

פלט כמו `h264` מעיד שיש וידאו.

### בדיקת VLC ישירה

```bash
vlc --rtsp-tcp "rtsp://DNA:DNA2025!@98.173.8.28:554/cam/realmonitor?channel=1&subtype=1"
```

---

## 4) הקלטה בתוך האפליקציה

סמנו **Record to MP4 (FFmpeg)** ובחרו נתיב קובץ (למשל `C:\onvif_rtsp_simple_app\output.mp4`).
כשתתחברו, האפליקציה תריץ FFmpeg כתהליך נפרד ב־`copy` (ללא קידוד מחדש) ותקליט בזמן אמת.

---

## 5) פתרון תקלות נפוצות

* **401 Unauthorized** — שם משתמש/סיסמה שגויים, או שהמשתמש חסום. ודאו שהקרדנצ׳לס נכונים ושאין נעילת חשבון לאחר ניסיונות כושלים.
* **403 Forbidden** — למשתמש אין הרשאת Live/RTSP, RTSP כבוי במצלמה, או יש ACL/רשימת IPs מורשים. בדקו:

  * בתפריט המצלמה: **Network → RTSP** שה־RTSP **מאופשר**.
  * בתפריט משתמשים/הרשאות: למשתמש יש **Live View / Video**.
  * אין חסימת IP/Firewall בין המצלמה לעמדה.
* **404 Not Found** — נתיב לא נכון או Channel/Subtype לא קיימים. נסו:

  * `...channel=1&subtype=1` (תת־סטרים),
  * `...channel=1&subtype=0` (ראשי),
  * `/Streaming/Channels/101`.
* **Connection refused / Timeout** — פורט לא נכון או חסימת רשת. בדקו `Test-NetConnection` ל־554/5544.
* **שגיאות VLC כמו “buffer deadlock prevented / SetThumbNailClip failed”** — בדרך כלל הודעות לא קריטיות ב-Windows; אם אין תמונה:

  * ודאו ש־**Force RTSP over TCP** מסומן,
  * נסו **subtype=1** (פחות כבד).
* **ניסיתם הכול ועדיין לא**: חזרו ל־**Try Dahua (auto)** — הזינו Host, User/Pass ולחצו. הכלי ינסה קומבינציות נפוצות של פורטים/נתיבים/ערוצים וימלא אוטומטית אם מצא.

---

## 6) קונפיגורציות לדוגמה לאותה מצלמה

**RTSP ישיר (תת־סטרים):**

```
Host/IP:     98.173.8.28
Username:    DNA
Password:    DNA2025!
RTSP port:   554
RTSP path:   /cam/realmonitor?channel=1&subtype=1
```

**RTSP ישיר (זרם ראשי):**

```
Host/IP:     98.173.8.28
Username:    DNA
Password:    DNA2025!
RTSP port:   554
RTSP path:   /cam/realmonitor?channel=1&subtype=0
```

**ONVIF → RTSP (שולף אוטומטית):**

```
Mode:        ONVIF → RTSP
Host/IP:     98.173.8.28
Username:    DNA
Password:    DNA2025!
ONVIF port:  4416
(Force RTSP over TCP = V)
```

> אם משהו מהנ״ל מחזיר 401/403 — זה לא עניין של קוד אלא של קרדנצ׳לס/הרשאות/תצורת מצלמה. ודאו עם בעל הגישה שהמשתמש פעיל, שיש לו Live/RTSP ושלא בוצעה נעילה/שינוי סיסמה.

---

## 7) בדיקת Mock (לסינון בעיות במחשב/קוד)

אם יש ספק שהבעיה מקומית:

1. במסך **Mockup (local RTSP)** בחרו קובץ MP4.
2. שימו נתיב ל־`mediamtx.exe` ואל `ffmpeg.exe`.
3. לחצו **Start Mock Server** ואז **Connect Preview**.
   אם זה עובד — הנגן/הקלטה תקינים מקומית, והבעיה מול המצלמה היא הרשאות/פורט/נתיב.

---

### הערות אבטחה

* אל תפרסמו שם משתמש/סיסמה החוצה.
* אם הסיסמה מכילה תווים כמו `@` או `:` — אל תדביקו URL ידני; הזינו בשדות **Username/Password** והאפליקציה תבנה URL בטוח.

---

זהו. אם תרצה—אוכל גם להכין לך גרסת README באנגלית / להוסיף צילומי מסך.
