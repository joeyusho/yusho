import os
import sqlite3
from datetime import datetime, timedelta

import anthropic
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ─── App Setup ──────────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

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
def summarise(group_name: str, messages: list) -> str:
    chat_log = "\n".join(messages)
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f'สรุปงานและประเด็นสำคัญจากข้อความในกลุ่ม "{group_name}" เมื่อวานนี้:\n\n'
                    f"{chat_log}\n\n"
                    "กรุณาสรุปเป็นภาษาไทย แบ่งเป็น:\n"
                    "1. 📋 งานที่ต้องทำ (To-do)\n"
                    "2. ✅ งานที่เสร็จแล้ว\n"
                    "3. ⚠️ ประเด็นสำคัญ / ปัญหา\n"
                    "4. 📅 นัดหมาย / กำหนดการ\n\n"
                    "หัวข้อใดไม่มีข้อมูล ให้ข้ามได้เลย"
                ),
            }
        ],
    )
    return response.content[0].text


# ─── Daily Job ────────────────────────────────────────────────────────────────────────────────────
def daily_summary_job():
    yesterday = (datetime.now(BANGKOK_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    groups = get_messages_by_date(yesterday)

    if not groups:
        print(f"[{datetime.now(BANGKOK_TZ)}] No messages yesterday — skipping summary.")
        return

    for group_id, data in groups.items():
        try:
            summary = summarise(data["name"], data["messages"])
            text = (
                f"📊 สรุปงานประจำวัน {yesterday}\n"
                f"กลุ่ม: {data['name']}\n\n"
                f"{summary}"
            )
            line_bot_api.push_message(group_id, TextSendMessage(text=text))
            print(f"[OK] Sent summary to {group_id}")
        except Exception as e:
            print(f"[ERROR] group {group_id}: {e}")


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
