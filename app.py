import streamlit as st
import anthropic
import json
import os
import requests
import time
import xml.etree.ElementTree as ET
import base64
import csv
from io import BytesIO, StringIO
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REDIRECT_URI = "http://localhost:8501"
STRAVA_TOKEN_FILE = "strava_token.json"

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")

WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET")
WHOOP_REDIRECT_URI = "http://localhost:8501"
WHOOP_TOKEN_FILE = "whoop_token.json"


def save_strava_token(token_data):
    with open(STRAVA_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f)


def load_strava_token():
    if os.path.exists(STRAVA_TOKEN_FILE):
        with open(STRAVA_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def refresh_strava_token(refresh_token):
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    })
    return resp.json()


def get_valid_strava_token():
    token = load_strava_token()
    if not token:
        return None
    if token.get("expires_at", 0) < time.time():
        token = refresh_strava_token(token["refresh_token"])
        save_strava_token(token)
    return token


def exchange_code_for_token(code):
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    })
    return resp.json()


def fetch_strava_activities(access_token, per_page=20):
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": per_page, "type": "Ride"}
    )
    return resp.json()


def get_garmin_activities(limit=20):
    from garminconnect import Garmin
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    activities = client.get_activities(0, limit)
    rides = []
    for a in activities:
        if a.get("activityType", {}).get("typeKey", "") not in ("cycling", "road_biking", "mountain_biking", "virtual_ride"):
            continue
        distance_km = round(a.get("distance", 0) / 1000, 2)
        duration_min = round(a.get("movingDuration", a.get("duration", 0)) / 60)
        avg_speed = round(a.get("averageSpeed", 0) * 3.6, 1)
        rides.append({
            "date": a.get("startTimeLocal", "")[:10],
            "distance": distance_km,
            "duration": duration_min,
            "elevation": round(a.get("elevationGain", 0)),
            "avg_hr": round(a.get("averageHR", 0)),
            "avg_speed": avg_speed,
            "calories": a.get("calories", 0),
            "notes": f"[Garmin] {a.get('activityName', '')}",
            "garmin_id": a.get("activityId")
        })
    return rides


def save_whoop_token(token_data):
    with open(WHOOP_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f)


def load_whoop_token():
    if os.path.exists(WHOOP_TOKEN_FILE):
        with open(WHOOP_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def refresh_whoop_token(refresh_token):
    resp = requests.post("https://api.prod.whoop.com/oauth/oauth2/token", data={
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    })
    return resp.json()


def get_valid_whoop_token():
    token = load_whoop_token()
    if not token:
        return None
    if token.get("expires_at", 0) < time.time():
        token = refresh_whoop_token(token.get("refresh_token"))
        if "access_token" in token:
            token["expires_at"] = time.time() + token.get("expires_in", 3600)
            save_whoop_token(token)
    return token


def exchange_whoop_code(code):
    resp = requests.post("https://api.prod.whoop.com/oauth/oauth2/token", data={
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
        "code": code,
        "redirect_uri": WHOOP_REDIRECT_URI,
        "grant_type": "authorization_code"
    })
    data = resp.json()
    if "access_token" in data:
        data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def fetch_whoop_recovery(access_token, limit=7):
    resp = requests.get(
        "https://api.prod.whoop.com/developer/v1/recovery",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": limit}
    )
    return resp.json().get("records", [])


def fetch_whoop_cycles(access_token, limit=7):
    resp = requests.get(
        "https://api.prod.whoop.com/developer/v1/cycle",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"limit": limit}
    )
    return resp.json().get("records", [])


def parse_tcx(file_content):
    NS = {
        'tcx': 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2',
        'ext': 'http://www.garmin.com/xmlschemas/ActivityExtension/v2'
    }
    root = ET.fromstring(file_content)
    activities = root.findall('.//tcx:Activity', NS)
    rides = []

    for activity in activities:
        sport = activity.get('Sport', 'Biking')

        # Date
        id_el = activity.find('tcx:Id', NS)
        raw_date = id_el.text[:10] if id_el is not None else str(date.today())

        # Aggregate lap data
        total_time = 0
        total_distance = 0
        total_calories = 0
        hr_values = []
        alt_values = []

        for lap in activity.findall('tcx:Lap', NS):
            t = lap.find('tcx:TotalTimeSeconds', NS)
            d = lap.find('tcx:DistanceMeters', NS)
            c = lap.find('tcx:Calories', NS)
            hr = lap.find('tcx:AverageHeartRateBpm/tcx:Value', NS)
            if t is not None:
                total_time += float(t.text)
            if d is not None:
                total_distance += float(d.text)
            if c is not None:
                total_calories += int(c.text)
            if hr is not None:
                hr_values.append(float(hr.text))

            for tp in lap.findall('.//tcx:Trackpoint', NS):
                alt = tp.find('tcx:AltitudeMeters', NS)
                if alt is not None:
                    alt_values.append(float(alt.text))

        # Elevation gain
        elevation_gain = 0
        for i in range(1, len(alt_values)):
            diff = alt_values[i] - alt_values[i - 1]
            if diff > 0:
                elevation_gain += diff

        distance_km = round(total_distance / 1000, 2)
        duration_min = round(total_time / 60)
        avg_hr = round(sum(hr_values) / len(hr_values)) if hr_values else 0
        avg_speed = round((distance_km / (total_time / 3600)), 1) if total_time > 0 else 0

        rides.append({
            "date": raw_date,
            "distance": distance_km,
            "duration": duration_min,
            "elevation": round(elevation_gain),
            "avg_hr": avg_hr,
            "avg_speed": avg_speed,
            "calories": total_calories,
            "sport": sport,
            "notes": "[TrainingPeaks TCX]"
        })

    return rides


def parse_fit(file_bytes):
    from fitparse import FitFile
    fitfile = FitFile(BytesIO(file_bytes))

    ride_date = str(date.today())
    total_distance = 0
    total_time = 0
    hr_values = []
    alt_values = []
    speed_values = []
    calories = 0
    sport = "Biking"

    for record in fitfile.get_messages():
        name = record.name

        if name == "sport":
            s = record.get_value("sport")
            if s:
                sport = str(s)

        elif name == "session":
            d = record.get_value("total_distance")
            t = record.get_value("total_elapsed_time")
            c = record.get_value("total_calories")
            hr = record.get_value("avg_heart_rate")
            ts = record.get_value("start_time")
            if d:
                total_distance = d
            if t:
                total_time = t
            if c:
                calories = c
            if hr:
                hr_values.append(hr)
            if ts:
                ride_date = str(ts)[:10]

        elif name == "record":
            hr = record.get_value("heart_rate")
            alt = record.get_value("altitude")
            spd = record.get_value("speed")
            if hr:
                hr_values.append(hr)
            if alt:
                alt_values.append(alt)
            if spd:
                speed_values.append(spd)

    elevation_gain = 0
    for i in range(1, len(alt_values)):
        diff = alt_values[i] - alt_values[i - 1]
        if diff > 0:
            elevation_gain += diff

    distance_km = round(total_distance / 1000, 2) if total_distance else 0
    duration_min = round(total_time / 60) if total_time else 0
    avg_hr = round(sum(hr_values) / len(hr_values)) if hr_values else 0
    avg_speed = round(sum(speed_values) / len(speed_values) * 3.6, 1) if speed_values else (
        round(distance_km / (total_time / 3600), 1) if total_time > 0 else 0
    )

    return [{
        "date": ride_date,
        "distance": distance_km,
        "duration": duration_min,
        "elevation": round(elevation_gain),
        "avg_hr": avg_hr,
        "avg_speed": avg_speed,
        "calories": calories,
        "sport": sport,
        "notes": "[FIT file]"
    }]


def strava_activity_to_ride(activity):
    return {
        "date": activity["start_date_local"][:10],
        "distance": round(activity.get("distance", 0) / 1000, 2),
        "duration": round(activity.get("moving_time", 0) / 60),
        "elevation": round(activity.get("total_elevation_gain", 0)),
        "avg_hr": activity.get("average_heartrate", 0) or 0,
        "avg_speed": round(activity.get("average_speed", 0) * 3.6, 1),
        "notes": f"[Strava] {activity.get('name', '')}",
        "strava_id": activity.get("id")
    }

st.set_page_config(
    page_title="מאמן אופניים אישי",
    page_icon="🚴",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    body { direction: rtl; }
    .stApp { direction: rtl; }
    .stTextInput input { direction: rtl; }
    .stTextArea textarea { direction: rtl; }
    [data-testid="stSidebar"] { direction: rtl; }
    .chat-message { padding: 1rem; border-radius: 10px; margin: 0.5rem 0; }
    .user-message { background-color: #e3f2fd; text-align: right; }
    .coach-message { background-color: #f3e5f5; text-align: right; }
    h1, h2, h3 { text-align: right; }
</style>
""", unsafe_allow_html=True)

SYSTEM_PROMPT = """# Role: Expert Cycling Coach

Your name is "ניצן". You are an elite cycling coach with deep expertise in physiology, training science, and performance optimization.

## Skillset
- Cycling Physiology & Training Science
- Data Analysis (FTP, Power Zones, HR Zones, TSS, CTL/ATL/TSB)
- Personalized Plan Creation (Base, Build, Peak phases)
- Nutrition & Recovery strategy
- Biomechanics and bike fitting awareness

## Steering Guidelines
1. Always ask for current RPE (Rate of Perceived Exertion) and recent sleep/stress metrics before adjusting plans.
2. Prioritize long-term progress and injury prevention over short-term gains.
3. When providing a workout, always explain the *purpose* (e.g., "This Z2 ride builds mitochondrial efficiency").
4. Maintain a supportive, coaching-oriented persona.
5. Keep explanations concise, scientific, and actionable.
6. When analyzing ride data, reference specific metrics (power, HR, pace) rather than speaking in generalities.
7. If the athlete shows signs of overtraining (elevated RHR, poor HRV, declining performance), immediately recommend rest.

## Communication Rules
- Always respond in Hebrew (עברית).
- Use professional coaching language — warm but evidence-based.
- Use metric units (km, watts, bpm).

## Onboarding
If you don't yet know the athlete's profile, ask for:
- Current FTP (Functional Threshold Power) or estimated fitness level
- Training goals (gran fondo, race, weight loss, general fitness)
- Weekly training availability (hours/days)
- Any injuries or physical limitations
- Access to power meter? Heart rate monitor?"""


SUPPORTED_IMAGES = ["jpg", "jpeg", "png", "gif", "webp"]
SUPPORTED_FILES = ["pdf", "csv", "txt"]


def encode_image(image_bytes, media_type):
    return base64.standard_b64encode(image_bytes).decode("utf-8"), media_type


def extract_file_text(file_bytes, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    elif ext == "csv":
        text = file_bytes.decode("utf-8", errors="ignore")
        reader = csv.reader(StringIO(text))
        rows = list(reader)
        return "\n".join([", ".join(r) for r in rows[:100]])
    elif ext == "txt":
        return file_bytes.decode("utf-8", errors="ignore")
    return ""


def build_api_message(text, attachments):
    if not attachments:
        return {"role": "user", "content": text}
    content = []
    for a in attachments:
        if a["type"] == "image":
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": a["media_type"],
                    "data": a["data"]
                }
            })
        elif a["type"] == "file":
            content.append({
                "type": "text",
                "text": f"[קובץ מצורף: {a['name']}]\n{a['text']}"
            })
    if text:
        content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def get_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("לא נמצא API key. בדוק את קובץ ה-.env")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def build_system_prompt_with_context():
    prompt = SYSTEM_PROMPT
    token = get_valid_strava_token()
    rides = []

    if token:
        try:
            activities = fetch_strava_activities(token["access_token"], per_page=10)
            if isinstance(activities, list):
                rides = [strava_activity_to_ride(a) for a in activities if a.get("type") == "Ride"]
        except Exception:
            pass

    if not rides:
        rides = load_rides()[-10:]

    if rides:
        context = "\n\n---\nנתוני הרכיבות האחרונות של הספורטאי (10 האחרונות):\n"
        for r in rides:
            context += (
                f"- {r['date']}: {r['distance']} ק\"מ | "
                f"{r['duration']} דקות | "
                f"עליות: {r['elevation']} מ' | "
                f"מהירות: {r['avg_speed']} קמ\"ש"
            )
            if r.get("avg_hr"):
                context += f" | קצב לב: {r['avg_hr']} bpm"
            if r.get("notes"):
                context += f" | {r['notes']}"
            context += "\n"
        context += "\nהשתמש בנתונים אלו כדי לתת המלצות מדויקות ומותאמות אישית.\n---"
        prompt += context

    return prompt


def chat_with_coach(messages):
    client = get_client()
    api_messages = []
    for m in messages:
        if m["role"] == "assistant":
            api_messages.append({"role": "assistant", "content": m["content"]})
        else:
            api_messages.append(build_api_message(m.get("text", ""), m.get("attachments", [])))
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=build_system_prompt_with_context(),
        messages=api_messages
    )
    return response.content[0].text


def generate_training_plan(profile):
    client = get_client()
    prompt = f"""בנה תוכנית אימון שבועית מפורטת עבור רוכב עם הפרופיל הבא:
- רמה: {profile.get('level', 'בינוני')}
- מטרה: {profile.get('goal', 'כושר כללי')}
- ימי אימון בשבוע: {profile.get('days', 3)}
- משך אימון ממוצע: {profile.get('duration', 60)} דקות

פרמט את התוכנית בצורה ברורה עם כל יום, סוג האימון, עצימות ומטרה."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def analyze_ride(ride_data):
    client = get_client()
    prompt = f"""נתח את נתוני הרכיבה הבאים ותן המלצות:
{ride_data}

כלול בניתוח:
1. הערכת הביצועים
2. נקודות חזקות
3. תחומים לשיפור
4. המלצות לאימון הבא"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


CHAT_HISTORY_FILE = "chat_history.json"
GARMIN_CACHE_FILE = "garmin_cache.json"
WHOOP_CACHE_FILE = "whoop_cache.json"


def save_chat_history(messages):
    with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def load_chat_history():
    if os.path.exists(CHAT_HISTORY_FILE):
        with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_cache(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_cache(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_ride(ride):
    rides_file = "rides.json"
    rides = []
    if os.path.exists(rides_file):
        with open(rides_file, "r", encoding="utf-8") as f:
            rides = json.load(f)
    rides.append(ride)
    with open(rides_file, "w", encoding="utf-8") as f:
        json.dump(rides, f, ensure_ascii=False, indent=2)


def load_rides():
    rides_file = "rides.json"
    if os.path.exists(rides_file):
        with open(rides_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# --- Sidebar ---
with st.sidebar:
    st.title("🚴 מאמן אופניים")
    st.markdown("---")
    page = st.radio(
        "ניווט",
        ["💬 צ'אט עם המאמן", "📅 תוכנית אימון", "🟠 Strava", "🔵 TrainingPeaks", "⚫ Garmin", "🟣 WHOOP", "📊 נתוני רכיבה", "📈 סטטיסטיקות"],
        label_visibility="collapsed"
    )
    st.markdown("---")
    strava_token = get_valid_strava_token()
    if strava_token:
        athlete = strava_token.get("athlete", {})
        st.success(f"✅ Strava: {athlete.get('firstname', '')} {athlete.get('lastname', '')}")
    else:
        st.warning("🟠 Strava לא מחובר")

    if GARMIN_EMAIL and GARMIN_PASSWORD:
        st.success(f"✅ Garmin: {GARMIN_EMAIL}")
    else:
        st.warning("⚫ Garmin לא מחובר")

    whoop_token = get_valid_whoop_token()
    if whoop_token:
        st.success("✅ WHOOP מחובר")
    else:
        st.warning("🟣 WHOOP לא מחובר")
    st.markdown("---")
    if st.button("🗑️ נקה היסטוריית צ'אט"):
        st.session_state.messages = []
        if os.path.exists(CHAT_HISTORY_FILE):
            os.remove(CHAT_HISTORY_FILE)
        st.rerun()
    st.caption("מאמן אישי מבוסס AI")


# --- Chat Page ---
if page == "💬 צ'אט עם המאמן":
    st.title("💬 צ'אט עם ניצן — המאמן שלך")

    if "messages" not in st.session_state:
        saved = load_chat_history()
        if saved:
            st.session_state.messages = saved
        else:
            st.session_state.messages = []
            welcome = "שלום! אני ניצן, המאמן האישי שלך לאופניים 🚴\n\nאני כאן לעזור לך להתקדם, לתכנן אימונים ולענות על כל שאלה.\n\nכדי להתאים לך המלצות מדויקות — ספר לי קצת על עצמך: FTP נוכחי, מטרות, וכמה שעות בשבוע אתה יכול לאמן?"
            st.session_state.messages.append({"role": "assistant", "content": welcome})

    # Display messages
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user"):
                if msg.get("text"):
                    st.markdown(msg["text"])
                for a in msg.get("attachments", []):
                    if a["type"] == "image":
                        img_bytes = base64.b64decode(a["data"])
                        st.image(img_bytes, width=300)
                    elif a["type"] == "file":
                        st.caption(f"📎 {a['name']}")
        else:
            with st.chat_message("assistant", avatar="🚴"):
                st.markdown(msg["content"])

    # File uploader
    with st.expander("📎 צרף תמונה או קובץ"):
        uploaded = st.file_uploader(
            "תמונה (jpg/png/gif/webp) או קובץ (pdf/csv/txt)",
            type=SUPPORTED_IMAGES + SUPPORTED_FILES,
            accept_multiple_files=True,
            key="chat_upload"
        )

    user_input = st.chat_input("כתוב הודעה למאמן...")
    if user_input or (uploaded and st.session_state.get("pending_send")):
        attachments = []
        if uploaded:
            for f in uploaded:
                ext = f.name.rsplit(".", 1)[-1].lower()
                raw = f.read()
                if ext in SUPPORTED_IMAGES:
                    media_type = f"image/{'jpeg' if ext == 'jpg' else ext}"
                    data, mt = encode_image(raw, media_type)
                    attachments.append({"type": "image", "media_type": mt, "data": data, "name": f.name})
                else:
                    text = extract_file_text(raw, f.name)
                    attachments.append({"type": "file", "text": text, "name": f.name})

        user_msg = {"role": "user", "text": user_input or "", "attachments": attachments}
        st.session_state.messages.append(user_msg)

        with st.spinner("ניצן חושב..."):
            response = chat_with_coach(st.session_state.messages)
        st.session_state.messages.append({"role": "assistant", "content": response})
        save_chat_history(st.session_state.messages)
        st.rerun()


# --- Strava Page ---
elif page == "🟠 Strava":
    st.title("🟠 חיבור ל-Strava")

    # Handle OAuth callback
    params = st.query_params
    if "code" in params and not get_valid_strava_token():
        with st.spinner("מתחבר ל-Strava..."):
            token_data = exchange_code_for_token(params["code"])
            if "access_token" in token_data:
                save_strava_token(token_data)
                st.query_params.clear()
                st.success("התחברת ל-Strava בהצלחה!")
                st.rerun()
            else:
                st.error(f"שגיאה בהתחברות: {token_data.get('message', 'שגיאה לא ידועה')}")

    strava_token = get_valid_strava_token()

    if not strava_token:
        st.info("חבר את חשבון ה-Strava שלך כדי לייבא רכיבות אוטומטית.")
        auth_url = (
            f"https://www.strava.com/oauth/authorize"
            f"?client_id={STRAVA_CLIENT_ID}"
            f"&redirect_uri={STRAVA_REDIRECT_URI}"
            f"&response_type=code"
            f"&scope=activity:read_all"
        )
        st.link_button("🔗 התחבר ל-Strava", auth_url, type="primary")
    else:
        athlete = strava_token.get("athlete", {})
        col1, col2 = st.columns([1, 3])
        with col1:
            if athlete.get("profile"):
                st.image(athlete["profile"], width=80)
        with col2:
            st.subheader(f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}")
            st.caption(f"{athlete.get('city', '')} {athlete.get('country', '')}")

        st.markdown("---")

        col_fetch, col_disconnect = st.columns([2, 1])
        with col_fetch:
            num_activities = st.slider("כמה רכיבות לטעון?", 5, 50, 20)
            fetch_btn = st.button("🔄 טען רכיבות מ-Strava", type="primary")
        with col_disconnect:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔌 נתק Strava"):
                if os.path.exists(STRAVA_TOKEN_FILE):
                    os.remove(STRAVA_TOKEN_FILE)
                st.rerun()

        if fetch_btn or "strava_activities" in st.session_state:
            if fetch_btn:
                with st.spinner("מוריד רכיבות מ-Strava..."):
                    activities = fetch_strava_activities(strava_token["access_token"], num_activities)
                    if isinstance(activities, list):
                        st.session_state.strava_activities = activities
                    else:
                        st.error("שגיאה בטעינת הרכיבות מ-Strava")
                        st.session_state.strava_activities = []

            activities = st.session_state.get("strava_activities", [])
            ride_activities = [a for a in activities if a.get("type") == "Ride"]

            if not ride_activities:
                st.info("לא נמצאו רכיבות אופניים ב-Strava.")
            else:
                st.markdown(f"### נמצאו {len(ride_activities)} רכיבות")
                for activity in ride_activities:
                    ride = strava_activity_to_ride(activity)
                    with st.expander(f"📅 {ride['date']} — {activity.get('name', '')} — {ride['distance']} ק\"מ"):
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("מרחק", f"{ride['distance']} ק\"מ")
                        c2.metric("זמן", f"{ride['duration']} דקות")
                        c3.metric("עליות", f"{ride['elevation']} מ'")
                        c4.metric("מהירות", f"{ride['avg_speed']} קמ\"ש")

                        col_save, col_analyze = st.columns(2)
                        with col_save:
                            if st.button("💾 שמור", key=f"save_{activity['id']}"):
                                rides = load_rides()
                                existing_ids = [r.get("strava_id") for r in rides]
                                if activity["id"] not in existing_ids:
                                    save_ride(ride)
                                    st.success("נשמר!")
                                else:
                                    st.info("כבר קיים")
                        with col_analyze:
                            if st.button("🔍 נתח עם המאמן", key=f"analyze_{activity['id']}"):
                                ride_summary = f"""שם: {activity.get('name')}
תאריך: {ride['date']}
מרחק: {ride['distance']} ק"מ
משך: {ride['duration']} דקות
עליות: {ride['elevation']} מטר
מהירות ממוצעת: {ride['avg_speed']} קמ"ש
קצב לב ממוצע: {ride['avg_hr']} bpm"""
                                with st.spinner("המאמן מנתח..."):
                                    analysis = analyze_ride(ride_summary)
                                st.markdown("**ניתוח המאמן:**")
                                st.markdown(analysis)


# --- TrainingPeaks Page ---
elif page == "🔵 TrainingPeaks":
    st.title("🔵 ייבוא מ-TrainingPeaks")

    st.info("""
**איך מייצאים מ-TrainingPeaks:**
1. לך לאתר TrainingPeaks ← **Calendar**
2. לחץ על אימון שרוצה לייבא
3. לחץ **...** (שלוש נקודות) ← **Export Workout**
4. בחר פורמט **TCX**
5. גרור את הקובץ לכאן ↓
""")

    uploaded_files = st.file_uploader(
        "גרור קבצי TCX או FIT מ-TrainingPeaks / Garmin",
        type=["tcx", "fit"],
        accept_multiple_files=True,
        label_visibility="collapsed"
    )

    if uploaded_files:
        all_rides = []
        errors = []

        for f in uploaded_files:
            try:
                if f.name.lower().endswith(".tcx"):
                    content = f.read().decode("utf-8")
                    rides = parse_tcx(content)
                else:
                    content = f.read()
                    rides = parse_fit(content)
                all_rides.extend(rides)
            except Exception as e:
                errors.append(f"{f.name}: {str(e)}")

        if errors:
            for err in errors:
                st.error(f"שגיאה בקובץ {err}")

        if all_rides:
            st.success(f"נמצאו {len(all_rides)} אימונים!")
            st.markdown("---")

            for i, ride in enumerate(all_rides):
                with st.expander(f"📅 {ride['date']} — {ride['distance']} ק\"מ | {ride['sport']}"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("מרחק", f"{ride['distance']} ק\"מ")
                    c2.metric("זמן", f"{ride['duration']} דקות")
                    c3.metric("עליות", f"{ride['elevation']} מ'")
                    c4.metric("מהירות", f"{ride['avg_speed']} קמ\"ש")

                    if ride.get("avg_hr"):
                        st.caption(f"💓 קצב לב ממוצע: {ride['avg_hr']} bpm")
                    if ride.get("calories"):
                        st.caption(f"🔥 קלוריות: {ride['calories']}")

                    col_save, col_analyze = st.columns(2)
                    with col_save:
                        if st.button("💾 שמור", key=f"tp_save_{i}"):
                            save_ride(ride)
                            st.success("נשמר!")
                    with col_analyze:
                        if st.button("🔍 נתח עם המאמן", key=f"tp_analyze_{i}"):
                            ride_summary = f"""מקור: TrainingPeaks
תאריך: {ride['date']}
סוג: {ride['sport']}
מרחק: {ride['distance']} ק"מ
משך: {ride['duration']} דקות
עליות: {ride['elevation']} מטר
מהירות ממוצעת: {ride['avg_speed']} קמ"ש
קצב לב ממוצע: {ride['avg_hr']} bpm
קלוריות: {ride.get('calories', 'לא ידוע')}"""
                            with st.spinner("המאמן מנתח..."):
                                analysis = analyze_ride(ride_summary)
                            st.markdown("**ניתוח המאמן:**")
                            st.markdown(analysis)

            st.markdown("---")
            if st.button("💾 שמור הכל", type="primary"):
                existing = load_rides()
                existing_dates = {r["date"] for r in existing}
                new_rides = [r for r in all_rides if r["date"] not in existing_dates]
                for r in new_rides:
                    save_ride(r)
                st.success(f"נשמרו {len(new_rides)} אימונים חדשים!")


# --- Garmin Page ---
elif page == "⚫ Garmin":
    st.title("⚫ חיבור ל-Garmin Connect")

    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        st.info("הוסף את פרטי Garmin לקובץ `.env`:")
        st.code("GARMIN_EMAIL=האימייל-שלך@example.com\nGARMIN_PASSWORD=הסיסמה-שלך")
        st.warning("פתח את קובץ `.env` ב-Notepad, הוסף את השורות, שמור והרץ מחדש.")
    else:
        st.success(f"מחובר כ: {GARMIN_EMAIL}")
        num = st.slider("כמה רכיבות לטעון?", 5, 50, 20)

        if "garmin_rides" not in st.session_state:
            cached = load_cache(GARMIN_CACHE_FILE)
            if cached:
                st.session_state.garmin_rides = cached

        if st.button("🔄 טען רכיבות מ-Garmin", type="primary"):
            with st.spinner("מתחבר ל-Garmin Connect..."):
                try:
                    rides = get_garmin_activities(num)
                    st.session_state.garmin_rides = rides
                    save_cache(GARMIN_CACHE_FILE, rides)
                except Exception as e:
                    st.error(f"שגיאה: {str(e)}")
                    st.session_state.garmin_rides = []

        if "garmin_rides" in st.session_state:
            rides = st.session_state.garmin_rides
            if not rides:
                st.info("לא נמצאו רכיבות אופניים.")
            else:
                st.markdown(f"### נמצאו {len(rides)} רכיבות")
                for i, ride in enumerate(rides):
                    with st.expander(f"📅 {ride['date']} — {ride['notes'].replace('[Garmin] ', '')} — {ride['distance']} ק\"מ"):
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("מרחק", f"{ride['distance']} ק\"מ")
                        c2.metric("זמן", f"{ride['duration']} דקות")
                        c3.metric("עליות", f"{ride['elevation']} מ'")
                        c4.metric("מהירות", f"{ride['avg_speed']} קמ\"ש")
                        if ride.get("avg_hr"):
                            st.caption(f"💓 {ride['avg_hr']} bpm  |  🔥 {ride.get('calories', 0)} קל'")

                        col_save, col_analyze = st.columns(2)
                        with col_save:
                            if st.button("💾 שמור", key=f"g_save_{i}"):
                                existing = load_rides()
                                existing_ids = [r.get("garmin_id") for r in existing]
                                if ride.get("garmin_id") not in existing_ids:
                                    save_ride(ride)
                                    st.success("נשמר!")
                                else:
                                    st.info("כבר קיים")
                        with col_analyze:
                            if st.button("🔍 נתח עם המאמן", key=f"g_analyze_{i}"):
                                summary = f"""מקור: Garmin Connect
תאריך: {ride['date']}
מרחק: {ride['distance']} ק"מ
משך: {ride['duration']} דקות
עליות: {ride['elevation']} מטר
מהירות ממוצעת: {ride['avg_speed']} קמ"ש
קצב לב ממוצע: {ride['avg_hr']} bpm
קלוריות: {ride.get('calories', 0)}"""
                                with st.spinner("המאמן מנתח..."):
                                    analysis = analyze_ride(summary)
                                st.markdown("**ניתוח המאמן:**")
                                st.markdown(analysis)

                if st.button("💾 שמור הכל", type="primary"):
                    existing = load_rides()
                    existing_ids = {r.get("garmin_id") for r in existing}
                    new_rides = [r for r in rides if r.get("garmin_id") not in existing_ids]
                    for r in new_rides:
                        save_ride(r)
                    st.success(f"נשמרו {len(new_rides)} רכיבות חדשות!")


# --- WHOOP Page ---
elif page == "🟣 WHOOP":
    st.title("🟣 חיבור ל-WHOOP")

    params = st.query_params
    if "code" in params and not get_valid_whoop_token():
        with st.spinner("מתחבר ל-WHOOP..."):
            token_data = exchange_whoop_code(params["code"])
            if "access_token" in token_data:
                save_whoop_token(token_data)
                st.query_params.clear()
                st.success("התחברת ל-WHOOP בהצלחה!")
                st.rerun()
            else:
                st.error(f"שגיאה: {token_data}")

    whoop_token = get_valid_whoop_token()

    if not WHOOP_CLIENT_ID or not WHOOP_CLIENT_SECRET:
        st.info("צריך להוסיף פרטי WHOOP לקובץ `.env` אחרי ההרשמה ב-developer.whoop.com:")
        st.code("WHOOP_CLIENT_ID=...\nWHOOP_CLIENT_SECRET=...")
    elif not whoop_token:
        st.info("חבר את חשבון ה-WHOOP שלך.")
        auth_url = (
            "https://api.prod.whoop.com/oauth/oauth2/auth"
            f"?client_id={WHOOP_CLIENT_ID}"
            f"&redirect_uri={WHOOP_REDIRECT_URI}"
            "&response_type=code"
            "&scope=read:recovery read:cycles read:workout read:sleep read:profile"
        )
        st.link_button("🔗 התחבר ל-WHOOP", auth_url, type="primary")
    else:
        st.success("WHOOP מחובר!")
        days = st.slider("כמה ימים לטעון?", 3, 30, 7)

        if "whoop_recovery" not in st.session_state:
            cached = load_cache(WHOOP_CACHE_FILE)
            if cached:
                st.session_state.whoop_recovery = cached.get("recovery", [])
                st.session_state.whoop_cycles = cached.get("cycles", [])

        if st.button("🔄 טען נתוני WHOOP", type="primary"):
            with st.spinner("טוען נתונים מ-WHOOP..."):
                try:
                    recovery = fetch_whoop_recovery(whoop_token["access_token"], days)
                    cycles = fetch_whoop_cycles(whoop_token["access_token"], days)
                    st.session_state.whoop_recovery = recovery
                    st.session_state.whoop_cycles = cycles
                    save_cache(WHOOP_CACHE_FILE, {"recovery": recovery, "cycles": cycles})
                except Exception as e:
                    st.error(f"שגיאה: {str(e)}")

        if "whoop_recovery" in st.session_state:
            recovery = st.session_state.whoop_recovery
            cycles = st.session_state.whoop_cycles

            st.markdown("### Recovery & Strain")
            for rec in recovery:
                score = rec.get("score", {})
                recovery_score = score.get("recovery_score", 0)
                hrv = score.get("hrv_rmssd_milli", 0)
                rhr = score.get("resting_heart_rate", 0)
                day = rec.get("created_at", "")[:10]

                color = "🟢" if recovery_score >= 67 else ("🟡" if recovery_score >= 34 else "🔴")
                with st.expander(f"{color} {day} — Recovery: {recovery_score}%"):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Recovery", f"{recovery_score}%")
                    c2.metric("HRV", f"{round(hrv)} ms")
                    c3.metric("RHR", f"{round(rhr)} bpm")

            if recovery:
                st.markdown("---")
                if st.button("🔍 שאל את המאמן לגבי ה-Recovery שלי"):
                    rec_summary = "נתוני WHOOP Recovery לאחרונה:\n"
                    for rec in recovery[:7]:
                        score = rec.get("score", {})
                        rec_summary += (
                            f"- {rec.get('created_at', '')[:10]}: "
                            f"Recovery {score.get('recovery_score', 0)}% | "
                            f"HRV {round(score.get('hrv_rmssd_milli', 0))} ms | "
                            f"RHR {round(score.get('resting_heart_rate', 0))} bpm\n"
                        )
                    prompt = f"{rec_summary}\nבהתבסס על נתוני ה-Recovery האלו, האם כדאי לאמן היום? מה רמת העצימות המומלצת?"
                    with st.spinner("המאמן מנתח..."):
                        client = get_client()
                        resp = client.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=1024,
                            system=SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}]
                        )
                    st.markdown("**המלצת המאמן:**")
                    st.markdown(resp.content[0].text)

        if st.button("🔌 נתק WHOOP"):
            if os.path.exists(WHOOP_TOKEN_FILE):
                os.remove(WHOOP_TOKEN_FILE)
            st.rerun()


# --- Training Plan Page ---
elif page == "📅 תוכנית אימון":
    st.title("📅 בניית תוכנית אימון")

    col1, col2 = st.columns(2)
    with col1:
        level = st.selectbox("רמת ניסיון", ["מתחיל", "בינוני", "מתקדם"])
        goal = st.selectbox("מטרה", ["כושר כללי", "ירידה במשקל", "הכנה לתחרות", "רכיבות ארוכות"])
    with col2:
        days = st.slider("ימי אימון בשבוע", 1, 7, 3)
        duration = st.slider("משך אימון (דקות)", 30, 180, 60)

    if st.button("🎯 צור תוכנית אימון", type="primary"):
        profile = {"level": level, "goal": goal, "days": days, "duration": duration}
        with st.spinner("בונה תוכנית מותאמת אישית..."):
            plan = generate_training_plan(profile)
        st.markdown("### התוכנית שלך:")
        st.markdown(plan)
        st.download_button("📥 הורד תוכנית", plan, file_name="training_plan.txt", mime="text/plain")


# --- Ride Data Page ---
elif page == "📊 נתוני רכיבה":
    st.title("📊 הזן נתוני רכיבה")

    with st.form("ride_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            ride_date = st.date_input("תאריך", value=date.today())
            distance = st.number_input("מרחק (ק\"מ)", min_value=0.0, step=0.5)
        with col2:
            duration_ride = st.number_input("משך (דקות)", min_value=0, step=5)
            elevation = st.number_input("עליות (מטר)", min_value=0, step=10)
        with col3:
            avg_hr = st.number_input("קצב לב ממוצע", min_value=0, step=1)
            avg_speed = st.number_input("מהירות ממוצעת (קמ\"ש)", min_value=0.0, step=0.5)

        notes = st.text_area("הערות", placeholder="איך הרגשת? מה היה מיוחד?")
        submitted = st.form_submit_button("💾 שמור רכיבה", type="primary")

        if submitted and distance > 0:
            ride = {
                "date": str(ride_date),
                "distance": distance,
                "duration": duration_ride,
                "elevation": elevation,
                "avg_hr": avg_hr,
                "avg_speed": avg_speed,
                "notes": notes
            }
            save_ride(ride)

            ride_summary = f"""תאריך: {ride_date}
מרחק: {distance} ק"מ
משך: {duration_ride} דקות
עליות: {elevation} מטר
קצב לב ממוצע: {avg_hr}
מהירות ממוצעת: {avg_speed} קמ"ש
הערות: {notes}"""

            st.success("הרכיבה נשמרה!")
            with st.spinner("המאמן מנתח את הרכיבה..."):
                analysis = analyze_ride(ride_summary)
            st.markdown("### 🔍 ניתוח המאמן:")
            st.markdown(analysis)


# --- Stats Page ---
elif page == "📈 סטטיסטיקות":
    st.title("📈 סטטיסטיקות")

    rides = load_rides()
    if not rides:
        st.info("עדיין אין רכיבות שמורות. הכנס נתוני רכיבה בעמוד 'נתוני רכיבה'.")
    else:
        total_distance = sum(r.get("distance", 0) for r in rides)
        total_rides = len(rides)
        total_elevation = sum(r.get("elevation", 0) for r in rides)
        avg_speed_all = sum(r.get("avg_speed", 0) for r in rides) / total_rides if total_rides > 0 else 0

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("סה\"כ רכיבות", total_rides)
        col2.metric("סה\"כ ק\"מ", f"{total_distance:.1f}")
        col3.metric("סה\"כ עליות", f"{total_elevation:,} מ'")
        col4.metric("מהירות ממוצעת", f"{avg_speed_all:.1f} קמ\"ש")

        st.markdown("---")
        st.markdown("### הרכיבות האחרונות")
        for ride in reversed(rides[-10:]):
            with st.expander(f"📅 {ride['date']} — {ride.get('distance', 0)} ק\"מ"):
                c1, c2, c3 = st.columns(3)
                c1.write(f"⏱ {ride.get('duration', 0)} דקות")
                c2.write(f"⬆️ {ride.get('elevation', 0)} מטר")
                c3.write(f"💓 {ride.get('avg_hr', 0)} bpm")
                if ride.get("notes"):
                    st.caption(ride["notes"])
