import os
import re
import json
import base64
import datetime
import httpx
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent,
    TextMessageContent, JoinEvent
)
from linebot.v3.exceptions import InvalidSignatureError

# นำเข้า SDK เวอร์ชันใหม่ล่าสุดของ Google
from google import genai
from google.genai import types

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = Flask(__name__)

# ============================================================
#  CONFIGURATION
# ============================================================
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
REQUIRED_PHOTOS           = int(os.environ.get('REQUIRED_PHOTOS', '20'))

# คีย์ของ Gemini
GEMINI_API_KEY            = os.environ.get('GEMINI_API_KEY', '')

DATA_FILE    = 'submissions.json'
GROUPS_FILE  = 'groups.json'
SESSION_FILE = 'sessions.json'   # เก็บ session ปัจจุบันของแต่ละคน

# ============================================================
#  INIT
# ============================================================
handler          = WebhookHandler(LINE_CHANNEL_SECRET)
line_config      = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# ตั้งค่า Client ของ Gemini (เวอร์ชันใหม่)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
#  DATA HELPERS
# ============================================================
def load_json(path):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_today():
    tz = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(tz).strftime('%Y-%m-%d')


# ============================================================
#  PARSE SUBMISSION TEXT
# ============================================================
def parse_submission(text):
    pattern = r'^([A-Za-z0-9]+)\s+(\d{6})\s+(\d+)\s*ถัง'
    m = re.match(pattern, text.strip(), re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2), int(m.group(3))
    return None


# ============================================================
#  LINE API HELPERS
# ============================================================
def get_user_name(user_id, group_id=None):
    try:
        with ApiClient(line_config) as api_client:
            api = MessagingApi(api_client)
            if group_id:
                profile = api.get_group_member_profile(group_id, user_id)
            else:
                profile = api.get_profile(user_id)
            return profile.display_name
    except Exception:
        return user_id

def download_image(message_id):
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

def send_reply(reply_token, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        ))

def send_push(to, text):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).push_message(PushMessageRequest(
            to=to,
            messages=[TextMessage(text=text)]
        ))


# ============================================================
#  IMAGE ANALYSIS (ใช้งาน Gemini API ใหม่)
# ============================================================
def analyze_image(image_data):
    try:
        prompt = (
            "ดูรูปนี้แล้วตอบว่าเป็นรูปหลักฐานการทำงาน "
            "(เช่น ตู้กาแฟ สินค้า หน้าร้าน การเติมน้ำ งานภาคสนาม) หรือไม่? "
            "ตอบแค่ YES หรือ NO ตามด้วยเหตุผลสั้น 1 ประโยคเป็นภาษาไทย"
        )
        
        # ส่งให้ Gemini ประมวลผลด้วยรูปแบบคำสั่งของ google-genai
        response = gemini_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[
                prompt,
                types.Part.from_bytes(data=image_data, mime_type='image/jpeg')
            ]
        )
        result = response.text.strip()
        print(f"[GEMINI SUCCESS]: {result}")
        
        return result.upper().startswith('YES'), result
    except Exception as e:
        print(f"[GEMINI ERROR]: {e}")
        return True, f"วิเคราะห์ไม่ได้: {e}"


# ============================================================
#  WEBHOOK ENDPOINT
# ============================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


# ============================================================
#  EVENT: บอทเข้ากลุ่ม
# ============================================================
@handler.add(JoinEvent)
def on_join(event):
    if event.source.type == 'group':
        gid = event.source.group_id
        groups = load_json(GROUPS_FILE)
        groups[gid] = {'joined': get_today(), 'active': True}
        save_json(GROUPS_FILE, groups)
        send_reply(event.reply_token,
            f"✅ Taobin Bot พร้อมแล้วครับ!\n"
            f"📌 Group ID: {gid}\n\n"
            f"📝 วิธีส่งงาน:\n"
            f"1. พิมพ์: [สาย] [เลขตู้ 6 หลัก] [จำนวน] ถัง\n"
            f"   ตัวอย่าง: NPT1 200243 5 ถัง\n"
            f"2. ส่งรูปหลักฐาน {REQUIRED_PHOTOS} รูป\n\n"
            f"🕗 Bot สรุปรายงานทุกวัน 20:00 น. อัตโนมัติ\n"
            f"💬 พิมพ์ /report เพื่อดูรายงานทันที"
        )


# ============================================================
#  EVENT: ข้อความ
# ============================================================
@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    if event.source.type != 'group':
        return

    text     = event.message.text.strip()
    user_id  = event.source.user_id
    group_id = event.source.group_id

    if text == '/id':
        send_reply(event.reply_token, f"Group ID: {group_id}")
        return

    if text == '/report':
        send_reply(event.reply_token, build_report())
        return

    if text == '/status':
        send_reply(event.reply_token, build_status())
        return

    parsed = parse_submission(text)
    if parsed:
        route, machine, tanks = parsed
        name = get_user_name(user_id, group_id)

        sessions = load_json(SESSION_FILE)
        sessions[user_id] = {
            'route'    : route,
            'machine'  : machine,
            'tanks'    : tanks,
            'name'     : name,
            'group_id' : group_id,
            'date'     : get_today(),
            'count'    : 0,
            'valid'    : 0,
            'invalid'  : 0
        }
        save_json(SESSION_FILE, sessions)

        send_reply(event.reply_token,
            f"📋 รับทราบครับ {name}!\n"
            f"🛣 สาย: {route}\n"
            f"🤖 ตู้: {machine}\n"
            f"💧 เติมน้ำ: {tanks} ถัง\n\n"
            f"กรุณาส่งรูปหลักฐาน {REQUIRED_PHOTOS} รูปได้เลยครับ"
        )


# ============================================================
#  EVENT: รูปภาพ
# ============================================================
@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event):
    if event.source.type != 'group':
        return

    user_id  = event.source.user_id
    group_id = event.source.group_id
    today    = get_today()

    sessions = load_json(SESSION_FILE)
    session  = sessions.get(user_id)

    if not session or session.get('date') != today:
        send_reply(event.reply_token,
            "⚠️ กรุณาพิมพ์ข้อมูลก่อนส่งรูปนะครับ\n"
            "ตัวอย่าง: NPT1 200243 5 ถัง"
        )
        return

    try:
        img_bytes = download_image(event.message.id)
        is_valid, _ = analyze_image(img_bytes)

        session['count'] += 1
        if is_valid:
            session['valid'] += 1
        else:
            session['invalid'] += 1

        sessions[user_id] = session
        save_json(SESSION_FILE, sessions)

        data    = load_json(DATA_FILE)
        key     = f"{user_id}_{session['route']}_{session['machine']}"
        if today not in data:
            data[today] = {}
        data[today][key] = {
            'user_id' : user_id,
            'name'    : session['name'],
            'route'   : session['route'],
            'machine' : session['machine'],
            'tanks'   : session['tanks'],
            'count'   : session['count'],
            'valid'   : session['valid'],
            'invalid' : session['invalid'],
            'group_id': group_id
        }
        save_json(DATA_FILE, data)

        count = session['count']
        if count == REQUIRED_PHOTOS:
            send_reply(event.reply_token,
                f"🎉 {session['name']} ส่งรูปครบ {REQUIRED_PHOTOS} รูปแล้วครับ!\n"
                f"🛣 สาย: {session['route']} | ตู้: {session['machine']}"
            )
        elif count % 5 == 0:
            send_reply(event.reply_token,
                f"📸 {session['name']} ส่งรูปแล้ว {count}/{REQUIRED_PHOTOS} รูป"
            )

    except Exception as e:
        print(f"[ERROR] on_image: {e}")


# ============================================================
#  DAILY REPORT
# ============================================================
def build_report():
    today = get_today()
    data  = load_json(DATA_FILE).get(today, {})

    if not data:
        return f"📋 รายงานวันที่ {today}\nยังไม่มีการส่งงานวันนี้ครับ 📭"

    submitted = []
    pending   = []

    by_route = {}
    for entry in data.values():
        r = entry['route']
        if r not in by_route:
            by_route[r] = []
        by_route[r].append(entry)

    for route in sorted(by_route.keys()):
        entries = by_route[route]
        for e in sorted(entries, key=lambda x: x['machine']):
            line = (
                f"[ ] {e['name']} | ตู้ {e['machine']} | "
                f"น้ำ {e['tanks']} ถัง | "
                f"รูป {e['count']}/{REQUIRED_PHOTOS}"
            )
            if e['count'] >= REQUIRED_PHOTOS:
                submitted.append(f"✅ {line}")
            else:
                pending.append(f"❌ {line}")

    report = (
        f"📋 Task Submission Checklist\n"
        f"Task/Project Name: รูปหลักฐานการทำงาน\n"
        f"Date: {today}\n\n"
        f"✅ Submitted ({len(submitted)} รายการ)\n"
        + ("\n".join(submitted) if submitted else "(ยังไม่มี)") +
        f"\n\n❌ Not Submitted / Pending ({len(pending)} รายการ)\n"
        + ("\n".join(pending) if pending else "🎉 ทุกรายการส่งครบแล้ว!") +
        f"\n\n💡 สรุป ณ เวลา 20:00 น. | ต้องส่งครบ {REQUIRED_PHOTOS} รูป"
    )
    return report

def build_status():
    today   = get_today()
    data    = load_json(DATA_FILE).get(today, {})
    if not data:
        return "ยังไม่มีการส่งงานวันนี้ครับ 📭"

    lines = [f"📊 สถานะวันนี้ ({today})\n"]
    for e in sorted(data.values(), key=lambda x: (x['route'], x['machine'])):
        icon = "✅" if e['count'] >= REQUIRED_PHOTOS else "⏳"
        lines.append(
            f"{icon} {e['name']} [{e['route']}] ตู้ {e['machine']} "
            f"— {e['count']}/{REQUIRED_PHOTOS} รูป"
        )
    return "\n".join(lines)

def send_daily_report():
    report = build_report()
    groups = load_json(GROUPS_FILE)
    for gid, info in groups.items():
        if info.get('active'):
            try:
                send_push(gid, report)
                print(f"[REPORT] Sent to {gid}")
            except Exception as e:
                print(f"[ERROR] send report to {gid}: {e}")


# ============================================================
#  SCHEDULER
# ============================================================
scheduler = BackgroundScheduler(timezone='Asia/Bangkok')
scheduler.add_job(
    send_daily_report,
    CronTrigger(hour=20, minute=0, timezone='Asia/Bangkok')
)
scheduler.start()


# ============================================================
#  UTILITY ROUTES
# ============================================================
@app.route('/', methods=['GET'])
def index():
    return '🤖 Taobin Bot is running!'

@app.route('/manual-report', methods=['GET'])
def manual_report():
    send_daily_report()
    return 'Report sent! ✅'

@app.route('/health', methods=['GET'])
def health():
    return json.dumps({'status': 'ok', 'today': get_today()})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
