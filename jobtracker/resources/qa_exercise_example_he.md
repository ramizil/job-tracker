# דוגמת תרגיל: שאלת תרחיש בדיקות (מבנה מלא לחיקוי)

## 📋 הגדרת הבעיה והדרישה (תיאור משימת הראיון)

**רקע ומערכת:** חברתנו מפתחת מערכת ניהול ציים חכמה (IoT Fleet Management).
רכיב החומרה המותקן ברכב משדר לשרת באופן רציף נתונים מלוח המחוונים (Dashboard)
של המכונית. מערכת ה-Backend מפעילה מנוע חוקים (Rule Engine) שמטרתו להתריע
לנהג או למנהל הצי מתי הרכב נדרש להיכנס למוסך לטיפול דחוף או תקופתי.

**חוקי העסק (Business Logic):** על המערכת להקפיץ התראת טיפול (Service Alert)
אם לפחות אחד מהתנאים הבאים מתקיים:

1. **תנאי קילומטרז' (Odometer):** הרכב עבר חצייה של כל 15,000 ק״מ מאז הטיפול האחרון שלו.
2. **תנאי חום מנוע (Engine Temperature):** חיישן הטמפרטורה מזהה חריגה מטווח
   העבודה התקין של המנוע (טווח תקין: בין 90 ל-105 מעלות צלזיוס).
3. **הערת אפיון:** כדי למנוע התראות שווא כתוצאה מרעש של חיישנים, התראת חום
   מנוע תישלח רק אם החריגה נמשכת יותר מ-30 שניות רצופות.

## 🎯 המשימה הנדרשת בראיון

**חלק א': תכנון וניתוח בדיקות (Test Design & Methodologies)**

1. הצע את מתודולוגיית הבדיקה (קופסה שחורה) הנכונה ביותר לפירוק הדרישה למקרי בדיקה.
2. כתוב סוויטת בדיקות (Test Cases) מקיפה הכוללת: תרחישים חיוביים (Happy Path),
   תרחישים שליליים (Negative Path), בדיקת ערכי גבול (Boundary Value Analysis),
   ומקרי קצה ומערכת (Edge Cases & Integration).

**חלק ב': ארכיטקטורת אוטומציה (Automation Design)**

1. כתוב סקריפט אוטומציה בפסאודו-קוד (בסגנון Playwright/Pytest) שמממש את הבדיקות.
2. הקפד על עקרונות עיצוב: בדיקות מבוססות נתונים (Data-Driven Testing) למניעת
   כפילויות קוד, והפרדה מבנית (Abstraction) בין קוד הטסט לבין הסימולטורים / קריאות ה-API.

## הפתרון המלא (רמת התשובה המצופה)

### 1. בדיקות פונקציונליות חיוביות (Happy Path — התראה צריכה לקפוץ)

- **טסט 1 (רק קילומטרז'):** רכב עם חום מנוע תקין (95 מעלות) אבל הגיע ל-15,001 ק"מ.
  תוצאה צפויה: נשלחת התראה על טיפול תקופתי.
- **טסט 2 (רק חום מנוע):** רכב שנסע רק 2,000 ק"מ אבל חום המנוע קפץ ל-110 מעלות.
  תוצאה צפויה: התראה מיידית עקב התחממות.
- **טסט 3 (שני התנאים יחד):** 16,000 ק"מ וחום מנוע 108 מעלות.
  תוצאה צפויה: נשלחת התראה (מוודא שאין התנגשות בין שני החוקים).

### 2. בדיקות פונקציונליות שליליות (Negative Path — התראה לא צריכה לקפוץ)

- **טסט 4 (הכל תקין):** 10,000 ק"מ וחום מנוע יציב של 95 מעלות.
  תוצאה צפויה: אין התראה, סטטוס ירוק.

### 3. בדיקות ערכי גבול (Boundary Value Analysis — הלב של הראיון!)

- **טסט 5 (על גבול הקילומטרז'):** 14,999 ק"מ (אין התראה) לעומת בדיוק 15,000 ק"מ (חובה התראה).
- **טסט 6 (גבול תחתון של חום):** בדיוק 90 מעלות (תקין) לעומת 89.9 מעלות (חריגה — התראה).
- **טסט 7 (גבול עליון של חום):** בדיוק 105 מעלות (תקין) לעומת 105.1 מעלות (חריגה — התראה).

### 4. בדיקות רמת מערכת ומקרי קצה (Edge Cases & System)

- **טסט 8 (Debounce):** חום קופץ ל-106 מעלות לחצי שנייה בלבד (רעש סנסור) וחוזר ל-95.
  תוצאה צפויה: אין התראת שווא (חוק 30 השניות הרצופות).
- **טסט 9 (איבוד קליטה):** הרכב במנהרה ללא קליטה והחום קפץ.
  תוצאה צפויה: שמירה מקומית (Offline Storage) ושליחה כשהקליטה חוזרת.
- **טסט 10 (ערכים לא הגיוניים):** הסנסור שולח מינוס 1 מעלות או קילומטרז' שלילי.
  תוצאה צפויה: התראת "סנסור תקול" ולא התראת טיפול רגילה.
- **טסט 11 (איפוס טיפול):** אחרי טיפול במוסך — מד הקילומטרז' לטיפול הבא מתאפס
  (הבא ב-30,000 ק"מ) וההתראה נעלמת.

**💡 טיפ זהב:** לפני כתיבת הטסטים, אמור למראיין: "הייתי בונה טבלת החלטה
(Decision Table) כדי לוודא שאני מכסה את כל השילובים האפשריים". המשפט הזה לבד
שווה מעבר של הראיון.

### חלק ב': האוטומציה (פסאודו-קוד בסגנון Playwright/Pytest)

```python
# רשימת מקרי הבדיקה (Data Provider / Parametrization)
TEST_DATA = [
    {"id": "Happy Path - high odometer", "km": 15001, "temp": 95,  "duration": 0,  "expected_alert": True},
    {"id": "Happy Path - engine overheat", "km": 2000, "temp": 110, "duration": 35, "expected_alert": True},
    {"id": "Negative - all normal",        "km": 10000, "temp": 95, "duration": 0,  "expected_alert": False},
    {"id": "Boundary - exactly at km limit", "km": 15000, "temp": 95, "duration": 0, "expected_alert": True},
    {"id": "Boundary - one km below limit",  "km": 14999, "temp": 95, "duration": 0, "expected_alert": False},
    {"id": "Edge - overheat too short (sensor noise)", "km": 2000, "temp": 106, "duration": 5, "expected_alert": False},
]

def test_vehicle_service_alert(vehicle_simulator, dashboard_api):
    for data in TEST_DATA:
        # Arrange: reset the system to a clean baseline
        vehicle_simulator.reset_to_default()
        # Act: inject the simulated drive data
        vehicle_simulator.set_odometer(data["km"])
        vehicle_simulator.set_engine_temperature(data["temp"])
        vehicle_simulator.hold_conditions_for_seconds(data["duration"])
        # Assert: read the alert state from the backend API
        alert = dashboard_api.get_alert_status_for_vehicle(vehicle_simulator.id)
        assert alert.is_displayed == data["expected_alert"], f"Failed on {data['id']}"


class VehicleSimulator:
    """Simulates the vehicle's IoT hardware components."""
    def reset_to_default(self): ...
    def set_odometer(self, km_value): ...
    def set_engine_temperature(self, temp_value): ...
    def hold_conditions_for_seconds(self, seconds): ...


class DashboardAPI:
    """Talks to the backend and reads the outcome."""
    def get_alert_status_for_vehicle(self, vehicle_id):
        return http.get(f"/api/vehicles/{vehicle_id}/alerts").json()
```

**למה הפתרון הזה מרשים מראיינים:** Data-Driven Testing (קוד גנרי אחד במקום 6
טסטים משוכפלים — שינוי דרישה = שורה אחת בטבלה), והפרדת שכבות
(POM/Infrastructure): הסימולטור מופרד לחלוטין מלוגיקת הטסט.
