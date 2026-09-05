import os
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage

# ─── App Setup ──────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Email settings
EMAIL_SENDER   = os.environ["EMAIL_SENDER"]    # Gmail address ที่ใช้ส่ง
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]  # Gmail App Password (16 ตัวอักษร)
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "joeyusho@gmail.com")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

BANGKOK_TZ = pytz.timezone("Asia/Bangkok")
DB_PATH = "messages.db"

# ─── Database ─────────────────────────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    TEXT    NOT NULL,
                group_name  TEXT,
                sender_name TEXT,
                message     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.commit()


def save_message(group_id: str, group_name: str, sender_name: str, message: str):
    now = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (group_id, group_name, sender_name, message, created_at) VALUES (?,?,?,?,?)",
            (group_id, group_name, sender_name, message, now),
        )
        conn.commit()


def get_messages_by_date(date_str: str):
    """Return {group_id: {name, messages[]}} for a given date (YYYY-MM-DD)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT group_id, group_name, sender_name, message FROM messages WHERE created_at LIKE ?",
            (f"{date_str}%",),
        ).fetchall()

    groups: dict = {}
    for group_id, group_name, sender_name, message in rows:
        if group_id not in groups:
            groups[group_id] = {"name": group_name or group_id, "messages": []}
        groups[group_id]["messages"].append(f"{sender_name}: {message}")
    return groups


# ─── Summariser ───────────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """คุณคือ Construction Project Management Assistant สำหรับทีมงานก่อสร้าง
หน้าที่ของคุณคือวิเคราะห์ข้อความทั้งหมดจาก LINE Group / LINE Chat ที่ได้รับ แล้วสรุปเฉพาะข้อมูลที่มีความสำคัญต่อการบริหารงานก่อสร้าง โดยต้องแยกข้อมูลตาม Project อย่างชัดเจน

หลักการสำคัญ:
- อ่านข้อความทั้งหมดก่อน แล้ววิเคราะห์ว่าแต่ละข้อความเกี่ยวข้องกับ Project ใด
- ห้ามสรุปตามลำดับข้อความใน LINE
- ให้จัดกลุ่มใหม่เป็น: Project → ประเด็นสำคัญ → สถานะ → ผู้รับผิดชอบ → กำหนดเวลา → สิ่งที่ต้องติดตาม
- หากข้อความไม่มีชื่อ Project ชัดเจน ให้ใช้บริบทจากข้อความก่อนหน้าเพื่อระบุ Project
- หากไม่สามารถระบุ Project ได้จริงๆ ให้จัดไว้ใน OTHER / UNIDENTIFIED PROJECT

ระดับความสำคัญ:
🔴 CRITICAL: กระทบ Completion Date / Critical Path / งานหยุด / Delay / ต้องตัดสินใจทันที
🟠 HIGH: มีแนวโน้มกระทบ Schedule / งานล่าช้า / Drawing/Material/Approval ที่กำลังจะกระทบงาน
🟡 MEDIUM: งานที่ต้องติดตาม / Pending / รอข้อมูล / รออนุมัติ
🟢 LOW: General Update / งานปกติ On Track / ข้อมูลเพื่อรับทราบ

สถานะงาน: 🟢 ON TRACK | 🟡 WATCH | 🟠 AT RISK | 🔴 DELAY | ⚪ PENDING | 🔵 COMPLETED

กฎเหล็ก:
- ห้ามสร้างข้อมูลที่ไม่มีในข้อความ / ห้ามเดา Due Date หรือ Responsible Person
- รวมข้อความที่พูดถึง Issue เดียวกัน / แยก Project ให้ถูกต้อง
- หากมีข้อมูลขัดแย้งกัน ให้แสดง ⚠️ Conflicting Information"""

USER_PROMPT_TEMPLATE = """กลุ่ม: {group_name}
วันที่: {date}

ข้อความจาก LINE:
{chat_log}

---
สร้างรายงาน DAILY LINE PROJECT SUMMARY ตาม format นี้:

# 📊 DAILY LINE PROJECT SUMMARY
**Date:** {date}

### 🚨 TOP PRIORITIES TODAY
[รายการ 1-5 อันดับแรกที่ต้องดูทันที]

**Total Projects:** X | **Critical Issues:** X | **High Priority:** X | **At Risk:** X | **Delayed:** X | **Pending Actions:** X

---
## 🏗️ PROJECT: [ชื่อ Project]
### 🔴 Critical Issues
1. **[หัวข้อ]**
   - Issue: [ปัญหา] | Impact: [ผลกระทบ] | Action Required: [ต้องทำอะไร]
   - Responsible: [ถ้าระบุได้ / TBC] | Due Date: [ถ้ามี / TBC] | Status: 🔴 DELAY

### 🟠 High Priority
### 🟡 Follow-up / Watch List
### 🟢 Completed / Progress Update

---
# 6. PROJECT SUMMARY TABLE
| Project | Priority | Key Issue | Action Required | Responsible | Due Date | Status |
|---------|----------|-----------|-----------------|-------------|----------|--------|

# 7. ACTION ITEMS
| # | Project | Action Item | Responsible | Due Date | Priority | Status |
|---|---------|-------------|-------------|----------|----------|--------|

หากไม่มีประเด็นสำคัญใน Project ใด ให้แสดง: 🟢 No Critical Issue / On Track
หากไม่มีข้อมูลในส่วนใด ให้ระบุ TBC"""


def summarise(group_name: str, messages: list, date_str: str = "") -> str:
    chat_log = "\n".join(messages)
    prompt = USER_PROMPT_TEMPLATE.format(
        group_name=group_name,
        date=date_str or "ไม่ระบุวันที่",
        chat_log=chat_log,
    )
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ─── Email Sender ─────────────────────────────────────────────────────────────────────────────────
def send_email(subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    print(f"[Email] Sent to {EMAIL_RECIPIENT}")


# ─── Daily Job ────────────────────────────────────────────────────────────────────────────────────
def daily_summary_job():
    yesterday = (datetime.now(BANGKOK_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    groups = get_messages_by_date(yesterday)

    if not groups:
        print(f"[{datetime.now(BANGKOK_TZ)}] No messages yesterday — skipping summary.")
        return

    all_summaries = []
    for group_id, data in groups.items():
        try:
            summary = summarise(data["name"], data["messages"], yesterday)
            all_summaries.append(
                f"━━━ กลุ่ม: {data['name']} ━━━\n{summary}"
            )
            print(f"[OK] Summarised group {group_id}")
        except Exception as e:
            print(f"[ERROR] group {group_id}: {e}")

    if all_summaries:
        body = (
            f"📊 สรุปงานประจำวัน {yesterday}\n\n"
            + "\n\n".join(all_summaries)
        )
        try:
            send_email(f"สรุปงาน LINE {yesterday}", body)
        except Exception as e:
            print(f"[ERROR] Email failed: {e}")


# ─── Health Check ────────────────────────────────────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200

# ─── Seed Test Data ──────────────────────────────────────────────────────────
@app.route("/seed", methods=["GET"])
def seed():
    today = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d")
    save_message("test_group_001", "\u0e17\u0b35\u0b21\u0b01\u0b48\u0e2a\u0e23\u0e49\u0e32\u0e07 TEST", "\u0e1c\u0e39\u0e49\u0e08\u0e31\u0e14\u0e01\u0e32\u0e23", "\u0e17\u0e14\u0e2a\u0e2d\u0e1a: Foundation B1 \u0e40\u0e2a\u0e23\u0e47\u0e08\u0e41\u0e25\u0e49\u0e27 On Track")
    save_message("test_group_001", "\u0e17\u0b35\u0b21\u0b01\u0b48\u0e2a\u0e23\u0e49\u0e32\u0e07 TEST", "\u0e42\u0e1f\u0e23\u0e41\u0e21\u0e19", "Rebar \u0e0a\u0e31\u0e49\u0e19 3 \u0e23\u0e2d inspect \u0e1e\u0e23\u0e38\u0e48\u0e07\u0e19\u0e35\u0e49 9\u0e42\u0e21\u0e07")
    return f"Seeded 2 test messages for {today}", 200



# ─── Manual Trigger ───────────────────────────────────────────────────────────────────────────────
@app.route("/trigger", methods=["GET"])
def trigger():
    try:
        date_param = request.args.get("date", "")
        if date_param == "today":
            target_date = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d")
        elif date_param:
            target_date = date_param
        else:
            target_date = (datetime.now(BANGKOK_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

        groups = get_messages_by_date(target_date)
        if not groups:
            return f"No messages found for {target_date}", 200

        all_summaries = []
        for group_id, data in groups.items():
            summary = summarise(data["name"], data["messages"], target_date)
            all_summaries.append(f"━━━ กลุ่ม: {data['name']} ━━━\n{summary}")

        body = f"📊 สรุปงานประจำวัน {target_date}\n\n" + "\n\n".join(all_summaries)
        send_email(f"สรุปงาน LINE {target_date}", body)
        return f"OK: trigger fired for {target_date}"
    except Exception as e:
        return f"ERROR: {e}", 500


# ─── Webhook ──────────────────────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if event.source.type != "group":
        return

    group_id = event.source.group_id
    message = event.message.text

    try:
        group_summary = line_bot_api.get_group_summary(group_id)
        group_name = group_summary.group_name
    except Exception:
        group_name = group_id

    try:
        profile = line_bot_api.get_group_member_profile(group_id, event.source.user_id)
        sender_name = profile.display_name
    except Exception:
        sender_name = "ไม่ทราบชื่อ"

    save_message(group_id, group_name, sender_name, message)


# ─── Scheduler ───────────────────────────────────────────────────────────────────────────────────
def start_scheduler():
    scheduler = BackgroundScheduler(timezone=BANGKOK_TZ)
    scheduler.add_job(daily_summary_job, "cron", hour=6, minute=30)
    scheduler.start()
    print("[Scheduler] Daily summary set for 06:30 Asia/Bangkok")


# ─── Initialize at module load (works with gunicorn) ───────────────────────────────
init_db()
start_scheduler()

# ─── Entry Point ──────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
