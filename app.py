import os
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
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = Flask(__name__)

# ============================================================
#  CONFIGURATION — ค่าเหล่านี้ตั้งผ่าน Environment Variables
# ============================================================
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
ANTHROPIC_API_KEY         = os.environ.get('ANTHROPIC_API_KEY', '')
REQUIRED_PHOTOS           = int(os.environ.get('REQUIRED_PHOTOS', '20'))

DATA_FILE   = 'submissions.json'
GROUPS_FILE = 'groups.json'

# ============================================================
#  INIT
# ============================================================
handler          = WebhookHandler(LINE_CHANNEL_SECRET)
line_config      = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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
    """วันที่ตามเวลาประเทศไทย (UTC+7)"""
    tz = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(tz).strftime('%Y-%m-%d')


# ============================================================
#  LINE API HELPERS
# ============================================================
def get_user_name(user_id, group_id=None):
    """ดึงชื่อสมาชิกจาก LINE"""
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
    """Download รูปจาก LINE"""
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
#  IMAGE ANALYSIS
# ============================================================
def analyze_image(image_data):
    """วิเคราะห์รูปด้วย Claude — ตรวจว่าเป็นรูปหลักฐานการทำงานหรือไม่"""
    try:
        img_b64 = base64.standard_b64encode(image_data).decode('utf-8')
        msg = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "ดูรูปนี้แล้วตอบว่าเป็นรูปหลักฐานการทำงาน "
                            "(เช่น ตู้กาแฟ สินค้า หน้าร้าน การเติมน้ำ งานภาคสนาม) หรือไม่? "
                            "ตอบแค่ YES หรือ NO ตามด้วยเหตุผลสั้น 1 ประโยคเป็นภาษาไทย"
                        )
                    }
                ]
            }]
        )
        result = msg.content[0].text.strip()
        return result.upper().startswith('YES'), result
    except Exception as e:
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
            f"📌 Group ID: {gid}\n"
            f"🕗 จะสรุปรายงานทุกวัน 20:00 น. อัตโนมัติ\n"
            f"💬 พิมพ์ /report เพื่อดูรายงานตอนนี้ได้เลยครับ"
        )


# ============================================================
#  EVENT: ข้อความ (Commands)
# ============================================================
@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    text = event.message.text.strip().lower()

    if text == '/id':
        if event.source.type == 'group':
            send_reply(event.reply_token, f"Group ID: {event.source.group_id}")
        else:
            send_reply(event.reply_token, f"User ID: {event.source.user_id}")

    elif text == '/report':
        report = build_report()
        send_reply(event.reply_token, report)

    elif text == '/status':
        today = get_today()
        data = load_json(DATA_FILE).get(today, {})
        if not data:
            send_reply(event.reply_token, "ยังไม่มีการส่งรูปวันนี้ครับ 📭")
            return
        lines = [f"📊 สถานะวันนี้ ({today})\n"]
        for info in data.values():
            icon = "✅" if info['count'] >= REQUIRED_PHOTOS else "⏳"
            lines.append(f"{icon} {info['name']}: {info['count']}/{REQUIRED_PHOTOS} รูป")
        send_reply(event.reply_token, "\n".join(lines))


# ============================================================
#  EVENT: รูปภาพ
# ============================================================
@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event):
    if event.source.type != 'group':
        return

    group_id = event.source.group_id
    user_id  = event.source.user_id
    today    = get_today()

    # โหลดและอัพเดทข้อมูล
    data = load_json(DATA_FILE)
    if today not in data:
        data[today] = {}
    if user_id not in data[today]:
        data[today][user_id] = {
            'name'     : get_user_name(user_id, group_id),
            'count'    : 0,
            'valid'    : 0,
            'invalid'  : 0,
            'group_id' : group_id
        }

    # Download + วิเคราะห์รูป
    try:
        img_bytes = download_image(event.message.id)
        is_valid, _ = analyze_image(img_bytes)

        data[today][user_id]['count'] += 1
        if is_valid:
            data[today][user_id]['valid'] += 1
        else:
            data[today][user_id]['invalid'] += 1

        save_json(DATA_FILE, data)

        # แจ้งเมื่อส่งครบ
        if data[today][user_id]['count'] == REQUIRED_PHOTOS:
            name = data[today][user_id]['name']
            send_reply(event.reply_token,
                f"🎉 {name} ส่งรูปครบ {REQUIRED_PHOTOS} รูปแล้วครับ!")

    except Exception as e:
        print(f"[ERROR] on_image: {e}")


# ============================================================
#  DAILY REPORT
# ============================================================
def build_report():
    today = get_today()
    data  = load_json(DATA_FILE).get(today, {})

    submitted  = []
    pending    = []

    for info in data.values():
        if info['count'] >= REQUIRED_PHOTOS:
            submitted.append(
                f"[ ] {info['name']} "
                f"({info['valid']} รูปผ่าน / {info['count']} รูปทั้งหมด)"
            )
        else:
            pending.append(
                f"[ ] {info['name']} "
                f"(ส่งมา {info['count']}/{REQUIRED_PHOTOS} รูป)"
            )

    report = (
        f"📋 Task Submission Checklist\n"
        f"Task/Project Name: รูปหลักฐานการทำงาน\n"
        f"Date: {today}\n\n"
        f"✅ Submitted ({len(submitted)} คน)\n"
        + ("\n".join(submitted) if submitted else "(ยังไม่มี)") +
        f"\n\n❌ Not Submitted / Pending ({len(pending)} คน)\n"
        + ("\n".join(pending) if pending else "🎉 ทุกคนส่งครบแล้ว!") +
        f"\n\n💡 สรุป ณ เวลา 20:00 น. | ต้องส่งครบ {REQUIRED_PHOTOS} รูป"
    )
    return report

def send_daily_report():
    """ส่งรายงานไปยังทุกกลุ่มที่ Bot อยู่"""
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
#  SCHEDULER — 20:00 ทุกวัน (Bangkok Time)
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
    """เรียกรายงานด้วยมือ (ทดสอบ)"""
    send_daily_report()
    return 'Report sent! ✅'

@app.route('/health', methods=['GET'])
def health():
    return json.dumps({'status': 'ok', 'today': get_today()})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
