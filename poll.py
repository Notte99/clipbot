"""ClipBot (โหมด GitHub Actions): ตื่นมาอ่านข้อความใหม่ นับสถิติ ตอบคำสั่ง แล้วจบ"""
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")
REPORT_HOUR = 23          # ส่งสรุปอัตโนมัติรอบแรกที่รันหลัง 23:00 เวลาไทย
DB_PATH = "stats.db"


def call(method, **params):
    token = os.environ["BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v
         for k, v in params.items()}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(res)
    return res["result"]


def init_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS stats(
        day TEXT, chat_id INTEGER, user_id INTEGER, name TEXT,
        msgs INTEGER DEFAULT 0, videos INTEGER DEFAULT 0, photos INTEGER DEFAULT 0,
        PRIMARY KEY(day, chat_id, user_id));
    CREATE TABLE IF NOT EXISTS members(
        chat_id INTEGER, user_id INTEGER, name TEXT,
        PRIMARY KEY(chat_id, user_id));
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def meta_get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db, key, value):
    db.execute("INSERT INTO meta VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def full_name(u):
    return " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or str(u["id"])


def remember(db, chat_id, u):
    db.execute("INSERT INTO members VALUES(?,?,?) "
               "ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name",
               (chat_id, u["id"], full_name(u)))


def bump(db, day, chat_id, u, col):
    remember(db, chat_id, u)
    db.execute("INSERT OR IGNORE INTO stats(day,chat_id,user_id,name) VALUES(?,?,?,?)",
               (day, chat_id, u["id"], full_name(u)))
    db.execute(f"UPDATE stats SET {col}={col}+1, name=? "
               "WHERE day=? AND chat_id=? AND user_id=?",
               (full_name(u), day, chat_id, u["id"]))


def process(db, updates):
    """นับสถิติจากข้อความ คืนค่าคำสั่งที่ต้องตอบ"""
    pending = set()
    for up in updates:
        m = up.get("message")
        if not m:
            print("skip: update without message")
            continue
        if m["chat"]["type"] not in ("group", "supergroup"):
            print(f"skip: chat type {m['chat']['type']} (ต้องพิมพ์ในกลุ่ม)")
            continue
        chat_id = m["chat"]["id"]
        for u in m.get("new_chat_members", []):
            if not u.get("is_bot"):
                remember(db, chat_id, u)
        text = m.get("text", "")
        user = m.get("from")
        anon = (not user) or bool(user.get("is_bot"))
        if text.startswith("/"):                     # คำสั่งทำงานได้แม้เป็นแอดมินไม่ระบุตัวตน
            cmd = text.split()[0].split("@")[0].lower()
            known = cmd in ("/report", "/missing")
            if known:
                pending.add((chat_id, cmd))
            print(f"msg: command known={known} anonymous={anon}")
            continue
        if anon:
            print("msg: skipped (ผู้ส่งเป็นบอทหรือแอดมินไม่ระบุตัวตน)")
            continue
        day = datetime.fromtimestamp(m["date"], TZ).date().isoformat()  # นับตามวันที่ส่งจริง
        if ("video" in m or "video_note" in m
                or m.get("document", {}).get("mime_type", "").startswith("video/")):
            bump(db, day, chat_id, user, "videos")
            print("msg: video")
        elif "photo" in m:
            bump(db, day, chat_id, user, "photos")
            print("msg: photo")
        elif text:
            bump(db, day, chat_id, user, "msgs")
            print("msg: text")
        else:
            print("msg: other (sticker/อื่นๆ ไม่นับ)")
    return pending


def report_text(db, chat_id, day):
    rows = db.execute(
        "SELECT name,msgs,videos,photos FROM stats WHERE day=? AND chat_id=? "
        "ORDER BY videos DESC, msgs DESC", (day, chat_id)).fetchall()
    if not rows:
        return f"ยังไม่มีข้อมูลวันที่ {day}"
    return f"📊 สรุปวันที่ {day}\n" + "\n".join(
        f"{n}: 💬{a} 🎬{v} 🖼{p}" for n, a, v, p in rows)


def missing_text(db, chat_id, day):
    rows = db.execute(
        "SELECT name FROM members WHERE chat_id=? AND user_id NOT IN "
        "(SELECT user_id FROM stats WHERE day=? AND chat_id=? AND videos>0) "
        "ORDER BY name", (chat_id, day, chat_id)).fetchall()
    if not rows:
        return f"✅ วันที่ {day} ทุกคนลงคลิปครบแล้ว"
    return f"❌ ยังไม่ลงคลิปวันที่ {day} ({len(rows)} คน)\n" + "\n".join(
        f"{i}. {r[0]}" for i, r in enumerate(rows, 1))


def send(chat_id, text):
    try:
        for i in range(0, len(text), 3800):
            call("sendMessage", chat_id=chat_id, text=text[i:i + 3800])
        print("sent ok to", chat_id)
    except Exception as e:
        print("send failed", chat_id, e)


def run(db, now=None):
    offset = int(meta_get(db, "offset", "0"))
    pending = set()
    while True:
        ups = call("getUpdates", offset=offset, limit=100, timeout=0,
                   allowed_updates=["message"])
        print(f"fetched {len(ups)} updates (offset {offset})")
        if not ups:
            break
        pending |= process(db, ups)
        offset = ups[-1]["update_id"] + 1
        meta_set(db, "offset", offset)
        db.commit()

    now = now or datetime.now(TZ)
    day = now.date().isoformat()
    n_members = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    print(f"pending commands: {sorted(pending)}, known members: {n_members}")
    for chat_id, cmd in pending:
        send(chat_id, report_text(db, chat_id, day) if cmd == "/report"
             else missing_text(db, chat_id, day))

    if now.hour >= REPORT_HOUR:
        for (chat_id,) in db.execute("SELECT DISTINCT chat_id FROM members").fetchall():
            if meta_get(db, f"report:{chat_id}") != day:
                send(chat_id, report_text(db, chat_id, day) + "\n\n"
                     + missing_text(db, chat_id, day))
                meta_set(db, f"report:{chat_id}", day)
                db.commit()
    db.commit()


if __name__ == "__main__":
    run(init_db(DB_PATH))
