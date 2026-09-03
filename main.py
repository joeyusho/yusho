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

app = Flask(__name__)
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
BANGKOK_TZ = pytz.timezone("Asia/Bangkok")
DB_PATH = "messages.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, group_name TEXT, sender_name TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.commit()

def save_message(group_id, group_name, sender_name, message):
    now = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO messages (group_id, group_name, sender_name, message, created_at) VALUES (?,?,?,?,?)", (group_id, group_name, sender_name, message, now))
        conn.commit()

def get_messages_by_date(date_str):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT group_id, group_name, sender_name, message FROM messages WHERE created_at LIKE ?", (f"{date_str}%",)).fetchall()
    groups = {}
    for group_id, group_name, sender_name, message in rows:
        if group_id not in groups:
            groups[group_id] = {"name": group_name or group_id, "messages": []}
        groups[group_id]["messages"].append(f"{sender_name}: {message}")
    return groups

def summarise(group_name, messages):
    chat_log = "\n".join(messages)
    response = claude.messages.create(model="claude-sonnet-4-5", max_tokens=1024, messages=[{"role": "user", "content": f'สรุปงานจากกลุ่ม "{group_name}":\n{chat_log}\nสรุปเป็นภาษาไทย: 1.งานที่ต้องทำ 2.งานเสร็จ 3.ปัญหา 4.นัดหมาย'}])
    return response.content[0].text

def daily_summary_job():
    yesterday = (datetime.now(BANGKOK_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    groups = get_messages_by_date(yesterday)
    if not groups: return
    for group_id, data in groups.items():
        try:
            summary = summarise(data["name"], data["messages"])
            line_bot_api.push_message(group_id, TextSendMessage(text=f"สรุปงาน {yesterday}\n\n{summary}"))
        except Exception as e:
            print(f"[ERROR] {group_id}: {e}")

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if event.source.type != "group": return
    group_id = event.source.group_id
    message = event.message.text
    try:
        gs = line_bot_api.get_group_summary(group_id)
        group_name = gs.group_name
    except: group_name = group_id
    try:
        p = line_bot_api.get_group_member_profile(group_id, event.source.user_id)
        sender_name = p.display_name
    except: sender_name = "unknown"
    save_message(group_id, group_name, sender_name, message)

def start_scheduler():
    s = BackgroundScheduler(timezone=BANGKOK_TZ)
    s.add_job(daily_summary_job, "cron", hour=6, minute=30)
    s.start()

if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))