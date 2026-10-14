"""ClipBot (โหมด GitHub Actions): ตื่นมาอ่านข้อความใหม่ นับสถิติ ตอบคำสั่ง แล้วจบ

คำสั่งในกลุ่ม (ใช้ได้เฉพาะแอดมิน คนอื่นพิมพ์แล้วบอทไม่ตอบ):
  /report [YYYY-MM-DD]   สถิติรายคนของวันนี้ (รีเซ็ตทุกเที่ยงคืนเวลาไทย) หรือของวันที่ระบุ
  /missing [YYYY-MM-DD]  คนที่ยังไม่ลงคลิปวันนี้ หรือวันที่ระบุ
  /history [จำนวนวัน]    ย้อนหลังกี่วันที่ลงคลิป ต่อคน (ค่าเริ่มต้น 7 วัน)
  /kicklist [จำนวนวัน]   รายชื่อคนที่ไม่ลงคลิปติดกัน N วัน (ไม่นับวันนี้) ค่าเริ่มต้น 1
  /kick N [ยืนยัน]       แตะคนในรายชื่อนั้นออกจากกลุ่ม (แอดมินเท่านั้น ต้องระบุ N เอง)
"""
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")
REPORT_HOUR = 23          # ส่งสรุปอัตโนมัติรอบแรกที่รันหลัง 23:00 เวลาไทย
DB_PATH = "stats.db"
MAX_HISTORY_DAYS = 60
COMMANDS = ("/report", "/missing", "/history", "/kicklist", "/kick")
MAX_KICK_DAYS = 14
KICK_CONFIRM_OVER = 10    # แตะเกินกี่คนต้องพิมพ์ ยืนยัน
GAP_HOURS = 20            # บอทหยุดทำงานเกินกี่ชั่วโมงถือว่าข้อมูลช่วงนั้นไม่น่าไว้ใจ


def call(method, **params):
    token = os.environ["BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict, bool)) else v
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
        chat_id INTEGER, user_id INTEGER, name TEXT, first_day TEXT,
        PRIMARY KEY(chat_id, user_id));
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS kicks(day TEXT, chat_id INTEGER, user_id INTEGER, name TEXT);
    """)
    try:  # อัปเกรดฐานข้อมูลเก่าที่ยังไม่มีคอลัมน์ first_day
        db.execute("ALTER TABLE members ADD COLUMN first_day TEXT")
    except sqlite3.OperationalError:
        pass
    # สมาชิกเก่าที่ไม่มีวันเข้ากลุ่ม ให้นับเริ่มจากวันนี้ (มีวันผ่อนผัน)
    db.execute("UPDATE members SET first_day=? WHERE first_day IS NULL",
               (datetime.now(TZ).date().isoformat(),))
    return db


def meta_get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db, key, value):
    db.execute("INSERT INTO meta VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def full_name(u):
    return " ".join(p for p in (u.get("first_name"), u.get("last_name")) if p) or str(u["id"])


def remember(db, chat_id, u, day):
    db.execute("INSERT INTO members(chat_id,user_id,name,first_day) VALUES(?,?,?,?) "
               "ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name",
               (chat_id, u["id"], full_name(u), day))


def bump(db, day, chat_id, u, col):
    remember(db, chat_id, u, day)
    db.execute("INSERT OR IGNORE INTO stats(day,chat_id,user_id,name) VALUES(?,?,?,?)",
               (day, chat_id, u["id"], full_name(u)))
    db.execute(f"UPDATE stats SET {col}={col}+1, name=? "
               "WHERE day=? AND chat_id=? AND user_id=?",
               (full_name(u), day, chat_id, u["id"]))


_ADMIN_CACHE = {}


def is_admin(chat_id, user, m):
    """แอดมินไม่ระบุตัวตนส่งในนามกลุ่ม (sender_chat = กลุ่ม) หรือตรวจสถานะผ่าน Telegram"""
    if m.get("sender_chat", {}).get("id") == chat_id:
        return True
    if not user or user.get("is_bot"):
        return False
    key = (chat_id, user["id"])
    if key not in _ADMIN_CACHE:
        try:
            st = call("getChatMember", chat_id=chat_id, user_id=user["id"]).get("status")
            _ADMIN_CACHE[key] = st in ("creator", "administrator")
        except Exception as e:
            print("admin check failed", e)
            return False
    return _ADMIN_CACHE[key]


def process(db, updates):
    """นับสถิติจากข้อความ คืนค่าคำสั่งที่ต้องตอบ"""
    pending = []
    for up in updates:
        m = up.get("message")
        if not m:
            print("skip: update without message")
            continue
        if m["chat"]["type"] not in ("group", "supergroup"):
            print(f"skip: chat type {m['chat']['type']} (ต้องพิมพ์ในกลุ่ม)")
            continue
        chat_id = m["chat"]["id"]
        day = datetime.fromtimestamp(m["date"], TZ).date().isoformat()  # นับตามวันที่ส่งจริง

        for u in m.get("new_chat_members", []):
            if not u.get("is_bot"):
                remember(db, chat_id, u, day)
        left = m.get("left_chat_member")
        if left:  # คนออก/โดนแตะ ไม่ต้องค้างใน /missing (สถิติเก่ายังเก็บไว้)
            db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?",
                       (chat_id, left["id"]))
            print("msg: member left")
            continue

        text = m.get("text", "")
        user = m.get("from")
        anon = (not user) or bool(user.get("is_bot"))
        if text.startswith("/"):                     # คำสั่งทำงานได้แม้เป็นแอดมินไม่ระบุตัวตน
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()
            arg = " ".join(parts[1:])
            known = cmd in COMMANDS
            if known:
                authorized = is_admin(chat_id, user, m)   # ทุกคำสั่งใช้ได้เฉพาะแอดมิน
                pending.append((chat_id, cmd, arg, authorized))
            print(f"msg: command known={known} anonymous={anon}")
            continue
        if anon:
            print("msg: skipped (ผู้ส่งเป็นบอทหรือแอดมินไม่ระบุตัวตน)")
            continue
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


def history_text(db, chat_id, today, n):
    """ย้อนหลัง n วัน (รวมวันนี้) นับเฉพาะวันที่แต่ละคนอยู่ในกลุ่มแล้ว เรียงคนที่ลงน้อยสุดก่อน"""
    end = date.fromisoformat(today)
    start = end - timedelta(days=n - 1)
    members = db.execute("SELECT user_id,name,first_day FROM members WHERE chat_id=?",
                         (chat_id,)).fetchall()
    if not members:
        return "ยังไม่มีสมาชิกในระบบ"
    rows = []
    for uid, name, first in members:
        s = max(start, date.fromisoformat(first)) if first else start
        possible = max((end - s).days + 1, 1)
        posted, clips = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(videos),0) FROM stats "
            "WHERE chat_id=? AND user_id=? AND day>=? AND day<=? AND videos>0",
            (chat_id, uid, s.isoformat(), end.isoformat())).fetchone()
        rows.append((posted / possible, name, posted, possible, clips))
    rows.sort(key=lambda r: (r[0], r[1]))
    return (f"🗓 ย้อนหลัง {n} วัน (ถึง {today} รวมวันนี้)\nลงคลิป/จำนวนวันที่อยู่ในกลุ่ม\n"
            + "\n".join(f"{nm}: {p}/{d} วัน (🎬{c})" for _, nm, p, d, c in rows))


def send(chat_id, text):
    try:
        for i in range(0, len(text), 3800):
            call("sendMessage", chat_id=chat_id, text=text[i:i + 3800])
        print("sent ok to", chat_id)
    except Exception as e:
        print("send failed", chat_id, e)


def admin_ids(chat_id):
    try:
        return {a["user"]["id"] for a in call("getChatAdministrators", chat_id=chat_id)}
    except Exception as e:
        print("admin list failed", e)
        return None


def kick_candidates(db, chat_id, today, n, skip=frozenset()):
    """ไม่ลงคลิปติดกัน n วันที่จบแล้ว (ไม่นับวันนี้) และเข้ากลุ่มก่อนช่วงนั้น"""
    end = date.fromisoformat(today) - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    gaps = [d for d in days if meta_get(db, f"gap:{d}")]
    cands = []
    for uid, name, first in db.execute(
            "SELECT user_id,name,first_day FROM members WHERE chat_id=?", (chat_id,)).fetchall():
        if uid in skip:
            continue  # แอดมิน/เจ้าของกลุ่มไม่อยู่ในเกณฑ์แตะ
        if not first or date.fromisoformat(first) >= start:
            continue  # เพิ่งเข้ากลุ่ม ยังไม่ครบช่วง ผ่อนผัน
        posted = db.execute(
            "SELECT COUNT(*) FROM stats WHERE chat_id=? AND user_id=? "
            "AND day>=? AND day<=? AND videos>0",
            (chat_id, uid, days[0], days[-1])).fetchone()[0]
        if posted == 0:
            cands.append((uid, name))
    cands.sort(key=lambda c: c[1])
    return cands, gaps, days


def gap_warning(gaps):
    return ("⚠️ บอทเคยหยุดทำงานนานในวันที่ " + ", ".join(gaps)
            + " ข้อมูลอาจไม่ครบ ยังไม่ควรแตะจากรายชื่อนี้")


def kicklist_text(db, chat_id, today, n):
    admins = admin_ids(chat_id)
    cands, gaps, days = kick_candidates(db, chat_id, today, n, admins or frozenset())
    head = (f"👢 เข้าเกณฑ์โดนแตะ: ไม่ลงคลิป {n} วันติด "
            f"({days[0]} ถึง {days[-1]} ไม่นับวันนี้)")
    if not cands:
        out = head + "\nไม่มีใครเข้าเกณฑ์"
    else:
        out = (head + f" รวม {len(cands)} คน\n"
               + "\n".join(f"{i}. {nm}" for i, (_, nm) in enumerate(cands, 1))
               + f"\n\nสั่งแตะด้วย /kick {n} (แอดมินเท่านั้น)\n"
               "คนที่เพิ่งเข้ากลุ่มไม่ครบช่วงนี้ไม่ถูกนับ")
    if gaps:
        out += "\n\n" + gap_warning(gaps)
    if admins is None:
        out += "\n\n⚠️ ตรวจรายชื่อแอดมินไม่ได้ รายชื่อนี้อาจมีแอดมินปนอยู่"
    return out


def do_kick(db, chat_id, arg, today, authorized):
    if not authorized:
        return "คำสั่งนี้ใช้ได้เฉพาะแอดมินของกลุ่ม"
    parts = arg.split()
    if not parts or not parts[0].isdigit():
        return "ต้องระบุจำนวนวัน เช่น /kick 2 (ดูรายชื่อก่อนด้วย /kicklist 2)"
    n = max(1, min(int(parts[0]), MAX_KICK_DAYS))
    admins = admin_ids(chat_id)
    if admins is None:
        return "ตรวจรายชื่อแอดมินของกลุ่มไม่ได้ ยังไม่แตะ ลองใหม่อีกครั้ง"
    cands, gaps, days = kick_candidates(db, chat_id, today, n, admins)
    if gaps:
        return gap_warning(gaps)
    if not cands:
        return f"ไม่มีใครเข้าเกณฑ์ (ไม่ลงคลิป {n} วันติด)"
    if len(cands) > KICK_CONFIRM_OVER and "ยืนยัน" not in parts[1:]:
        return (f"จะแตะ {len(cands)} คน ซึ่งเยอะผิดปกติ ตรวจรายชื่อด้วย /kicklist {n} ก่อน\n"
                f"ถ้าถูกต้องแล้วสั่ง: /kick {n} ยืนยัน")
    ok, failed, stuck = [], [], []
    for uid, name in cands:
        try:
            call("banChatMember", chat_id=chat_id, user_id=uid)
        except Exception as e:
            print("ban failed", e)
            failed.append(name)
            continue
        ok.append(name)
        try:  # ปลดแบนทันที ให้กลับเข้ากลุ่มผ่านลิงก์ได้
            call("unbanChatMember", chat_id=chat_id, user_id=uid, only_if_banned=True)
        except Exception as e:
            print("unban failed", e)
            stuck.append(name)
        db.execute("INSERT INTO kicks VALUES(?,?,?,?)", (today, chat_id, uid, name))
        db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid))
    db.commit()
    out = f"👢 แตะแล้ว {len(ok)} คน\n" + "\n".join(f"- {x}" for x in ok)
    if failed:
        out += "\n\n❗ แตะไม่สำเร็จ (บอทอาจไม่มีสิทธิ์ Ban users หรือเป็นแอดมิน):\n" \
               + "\n".join(f"- {x}" for x in failed)
    if stuck:
        out += "\n\n❗ ปลดแบนไม่สำเร็จ (ยังเข้ากลุ่มใหม่ไม่ได้ ต้องปลดเอง):\n" \
               + "\n".join(f"- {x}" for x in stuck)
    return out


def send(chat_id, text):
    try:
        for i in range(0, len(text), 3800):
            call("sendMessage", chat_id=chat_id, text=text[i:i + 3800])
        print("sent ok to", chat_id)
    except Exception as e:
        print("send failed", chat_id, e)


def answer(db, chat_id, cmd, arg, today, authorized=False):
    if cmd == "/kick":
        return do_kick(db, chat_id, arg, today, authorized)
    if cmd in ("/history", "/kicklist"):
        n = int(arg.split()[0]) if arg.split() and arg.split()[0].isdigit() else (7 if cmd == "/history" else 1)
        if cmd == "/kicklist":
            return kicklist_text(db, chat_id, today, max(1, min(n, MAX_KICK_DAYS)))
        return history_text(db, chat_id, today, max(1, min(n, MAX_HISTORY_DAYS)))
    day = today
    if arg:
        try:
            day = date.fromisoformat(arg.split()[0]).isoformat()
        except ValueError:
            return "รูปแบบวันที่ไม่ถูกต้อง ใช้แบบนี้: /report 2026-10-12"
    return report_text(db, chat_id, day) if cmd == "/report" else missing_text(db, chat_id, day)


def run(db, now=None):
    now = now or datetime.now(TZ)
    day = now.date().isoformat()
    _ADMIN_CACHE.clear()

    # บันทึกช่วงที่บอทหยุดทำงานนาน (ใช้กันแตะคนจากข้อมูลไม่ครบ)
    last = meta_get(db, "last_run")
    if last and now.timestamp() - float(last) > GAP_HOURS * 3600:
        d = datetime.fromtimestamp(float(last), TZ).date()
        while d <= now.date():
            meta_set(db, f"gap:{d.isoformat()}", 1)
            d += timedelta(days=1)
    meta_set(db, "last_run", now.timestamp())

    offset = int(meta_get(db, "offset", "0"))
    pending = []
    while True:
        ups = call("getUpdates", offset=offset, limit=100, timeout=0,
                   allowed_updates=["message"])
        print(f"fetched {len(ups)} updates (offset {offset})")
        if not ups:
            break
        for item in process(db, ups):
            if item not in pending:
                pending.append(item)
        offset = ups[-1]["update_id"] + 1
        meta_set(db, "offset", offset)
        db.commit()

    n_members = db.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    print(f"pending commands: {sorted((c, cmd) for c, cmd, _, _ in pending)}, known members: {n_members}")
    for chat_id, cmd, arg, authorized in pending:
        if not authorized:
            print("ignored: command from non-admin")   # ไม่ตอบ เพื่อไม่ให้กลุ่มรก
            continue
        send(chat_id, answer(db, chat_id, cmd, arg, day, authorized))

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
