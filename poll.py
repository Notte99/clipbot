"""ClipBot (โหมด GitHub Actions): ตื่นมาอ่านข้อความใหม่ นับสถิติ ตอบคำสั่ง แล้วจบ

คำสั่งในกลุ่ม (ใช้ได้เฉพาะแอดมิน คนอื่นพิมพ์แล้วบอทไม่ตอบ):
  /top [จำนวนคน|all] [คลิป|วัน|รูป|ข้อความ]  ท็อปคนที่ลงมากสุดตั้งแต่เริ่มนับ (ค่าเริ่มต้น 10 คน เรียงตามคลิป)
  /daily [YYYY-MM-DD]    วันนี้/วันที่ระบุ ใครลงคลิป ใครไม่ลง
  /never [วัน]           คนที่ไม่เคยลงคลิปเลยตั้งแต่เข้ากลุ่ม (เข้ามาแล้วอย่างน้อย N วัน ค่าเริ่มต้น 1)
  /kick                  แตะตามรายชื่อที่ส่งต่อท้าย (ชื่อละบรรทัด) ถ้าเกิน 10 คนให้ใส่ ยืนยัน บรรทัดแรก
  /report [วันที่]       สถิติรายคนของวัน   /missing [วันที่]  คนที่ไม่ลงคลิปของวัน
  /history [วัน]         ย้อนหลังกี่วันที่ลงคลิป   /kicklist [วัน]  ไม่ลงคลิปติดกัน N วัน
"""
import json
import os
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")
REPORT_HOUR = 23          # ส่งสรุปอัตโนมัติรอบแรกที่รันหลัง 23:00 เวลาไทย
NIGHTLY_REPORT = True     # False = ไม่ส่งสรุปเข้ากลุ่มเอง
DB_PATH = "stats.db"
MAX_HISTORY_DAYS = 60
MAX_KICK_DAYS = 14
KICK_CONFIRM_OVER = 10    # แตะเกินกี่คนต้องใส่ ยืนยัน
GAP_HOURS = 20            # บอทหยุดทำงานเกินกี่ชั่วโมงถือว่าข้อมูลช่วงนั้นไม่น่าไว้ใจ
COMMANDS = ("/report", "/missing", "/daily", "/top", "/never",
            "/history", "/kicklist", "/kick")
KICK_HOWTO = ("\n\nสั่งแตะ: คัดลอกชื่อ (ลบบรรทัดคนที่ไม่ต้องการแตะออกได้) "
              "แล้วส่งเป็นข้อความเดียวแบบนี้\n/kick\n1. ชื่อ\n2. ชื่อ")


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


def norm_name(s):
    return " ".join(unicodedata.normalize("NFC", s).split()).casefold()


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


def admin_ids(chat_id):
    try:
        return {a["user"]["id"] for a in call("getChatAdministrators", chat_id=chat_id)}
    except Exception as e:
        print("admin list failed", e)
        return None


def process(db, updates):
    """นับสถิติจากข้อความ คืนค่าคำสั่งที่ต้องตอบ (เรียงตามลำดับที่พิมพ์)"""
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
        if left:  # คนออก/โดนแตะ ไม่ต้องค้างในรายชื่อ (สถิติเก่ายังเก็บไว้)
            db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?",
                       (chat_id, left["id"]))
            print("msg: member left")
            continue

        text = m.get("text", "")
        user = m.get("from")
        anon = (not user) or bool(user.get("is_bot"))
        if text.startswith("/"):                     # คำสั่งทำงานได้แม้เป็นแอดมินไม่ระบุตัวตน
            bits = text.split(None, 1)
            cmd = bits[0].split("@")[0].lower()
            arg = bits[1] if len(bits) > 1 else ""   # เก็บทั้งก้อน (รวมขึ้นบรรทัดใหม่)
            known = cmd in COMMANDS
            if known:
                item = (chat_id, cmd, arg, is_admin(chat_id, user, m))
                pending.append(item)
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


# ---------- รายงาน ----------

def labeled(db, chat_id, cands):
    """ชื่อซ้ำกันให้ต่อท้ายด้วย [id ...] เพื่อแยกคนตอนสั่งแตะ"""
    cnt = Counter(norm_name(n) for (n,) in db.execute(
        "SELECT name FROM members WHERE chat_id=?", (chat_id,)))
    return [f"{n} [id {u}]" if cnt[norm_name(n)] > 1 else n for u, n in cands]


def report_text(db, chat_id, day):
    rows = db.execute(
        "SELECT name,msgs,videos,photos FROM stats WHERE day=? AND chat_id=? "
        "ORDER BY videos DESC, msgs DESC", (day, chat_id)).fetchall()
    if not rows:
        return f"ยังไม่มีข้อมูลวันที่ {day}"
    return f"📊 สรุปวันที่ {day}\n" + "\n".join(
        f"{n}: 💬{a} 🎬{v} 🖼{p}" for n, a, v, p in rows)


def missing_rows(db, chat_id, day):
    skip = admin_ids(chat_id) or set()   # แอดมินเป็นคนตรวจ ไม่นับว่าขาด
    return [r for r in db.execute(
        "SELECT user_id,name FROM members WHERE chat_id=? AND user_id NOT IN "
        "(SELECT user_id FROM stats WHERE day=? AND chat_id=? AND videos>0) "
        "ORDER BY name", (chat_id, day, chat_id)).fetchall() if r[0] not in skip]


def missing_text(db, chat_id, day):
    rows = missing_rows(db, chat_id, day)
    if not rows:
        return f"✅ วันที่ {day} ทุกคนลงคลิปครบแล้ว"
    return f"❌ ยังไม่ลงคลิปวันที่ {day} ({len(rows)} คน)\n" + "\n".join(
        f"{i}. {n}" for i, n in enumerate(labeled(db, chat_id, rows), 1))


def daily_text(db, chat_id, day):
    posted = db.execute(
        "SELECT name,videos FROM stats WHERE day=? AND chat_id=? AND videos>0 "
        "ORDER BY videos DESC, name", (day, chat_id)).fetchall()
    missing = missing_rows(db, chat_id, day)
    if not posted and not missing:
        return f"ยังไม่มีข้อมูลวันที่ {day}"
    out = [f"📅 วันที่ {day}", f"✅ ลงคลิปแล้ว ({len(posted)} คน)"]
    out += [f"{i}. {n} 🎬{v}" for i, (n, v) in enumerate(posted, 1)] or ["- ยังไม่มี"]
    out += ["", f"❌ ยังไม่ลง ({len(missing)} คน)"]
    out += [f"{i}. {n}" for i, n in enumerate(labeled(db, chat_id, missing), 1)] or ["- ไม่มี"]
    return "\n".join(out)


def top_text(db, chat_id, arg, today):
    """อันดับคนที่ลงมากที่สุดตั้งแต่เริ่มนับ: /top [จำนวนคน|all] [คลิป|วัน|รูป|ข้อความ]"""
    limit, mode = 10, "video"
    for tok in arg.lower().split():
        if tok.isdigit():
            limit = max(1, int(tok))
        elif tok in ("all", "ทั้งหมด"):
            limit = None
        elif tok in ("วัน", "day", "days", "บ่อย"):
            mode = "day"
        elif tok in ("photo", "รูป"):
            mode = "photo"
        elif tok in ("msg", "text", "ข้อความ"):
            mode = "msg"
        elif tok in ("video", "clip", "คลิป"):
            mode = "video"
    rows = db.execute(
        "SELECT m.name, COALESCE(SUM(s.msgs),0), COALESCE(SUM(s.videos),0), "
        "COALESCE(SUM(s.photos),0), COUNT(CASE WHEN s.videos>0 THEN 1 END), m.first_day "
        "FROM members m LEFT JOIN stats s "
        "ON s.chat_id=m.chat_id AND s.user_id=m.user_id "
        "WHERE m.chat_id=? GROUP BY m.user_id, m.name, m.first_day", (chat_id,)).fetchall()
    if not rows:
        return "ยังไม่มีสมาชิกในระบบ"
    order = {"video": lambda r: (-r[2], -r[4], -r[3], -r[1], r[0]),
             "day": lambda r: (-r[4], -r[2], -r[3], -r[1], r[0]),
             "photo": lambda r: (-r[3], -r[2], -r[1], r[0]),
             "msg": lambda r: (-r[1], -r[2], -r[3], r[0])}[mode]
    rows.sort(key=order)
    shown = rows[:limit] if limit else rows
    label = {"video": "ส่งคลิปมากสุด", "day": "ลงคลิปบ่อยสุด (นับจำนวนวัน)",
             "photo": "ส่งรูปมากสุด", "msg": "ส่งข้อความมากสุด"}[mode]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    t = date.fromisoformat(today)
    lines = []
    for i, (n, a, v, p, d, first) in enumerate(shown, 1):
        rank = medals.get(i, f"{i}.")
        if mode == "day":
            possible = max((t - date.fromisoformat(first)).days + 1, 1) if first else d
            lines.append(f"{rank} {n}: 🗓{d}/{possible} วัน 🎬{v}")
        else:
            lines.append(f"{rank} {n}: 🎬{v} 🖼{p} 💬{a}")
    head = f"🏆 ท็อป {len(shown)} คน {label} (ตั้งแต่เริ่มนับ"
    head += f", จากทั้งหมด {len(rows)} คน)" if len(shown) < len(rows) else ")"
    return head + "\n" + "\n".join(lines)


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


# ---------- รายชื่อที่จะแตะ ----------

def gap_days(db, limit=5):
    return [k[4:] for (k,) in db.execute(
        "SELECT key FROM meta WHERE key LIKE 'gap:%' ORDER BY key DESC LIMIT ?", (limit,))]


def never_text(db, chat_id, today, arg):
    """คนที่ไม่เคยส่งคลิปเลยตั้งแต่เข้ากลุ่ม (เข้ามาแล้วอย่างน้อย n วัน)"""
    first_tok = (arg.split() or [""])[0]
    n = int(first_tok) if first_tok.isdigit() else 1
    admins = admin_ids(chat_id)
    skip = admins or set()
    t = date.fromisoformat(today)
    rows = db.execute(
        "SELECT m.user_id, m.name, m.first_day, COALESCE(SUM(s.videos),0) "
        "FROM members m LEFT JOIN stats s ON s.chat_id=m.chat_id AND s.user_id=m.user_id "
        "WHERE m.chat_id=? GROUP BY m.user_id, m.name, m.first_day", (chat_id,)).fetchall()
    cands = sorted(((u, nm) for u, nm, first, vids in rows
                    if vids == 0 and u not in skip and first
                    and (t - date.fromisoformat(first)).days >= n), key=lambda c: c[1])
    head = f"🚫 ไม่เคยส่งคลิปเลยตั้งแต่เข้ากลุ่ม (เข้ามาแล้วอย่างน้อย {n} วัน)"
    if not cands:
        out = head + "\nไม่มีใครเข้าเกณฑ์"
    else:
        out = (head + f" รวม {len(cands)} คน\n"
               + "\n".join(f"{i}. {x}" for i, x in enumerate(labeled(db, chat_id, cands), 1))
               + KICK_HOWTO)
    gaps = gap_days(db)
    if gaps:
        out += ("\n\n⚠️ บอทเคยหยุดทำงานนานในวันที่ " + ", ".join(gaps)
                + " บางคนอาจถูกนับพลาด ตรวจก่อนแตะ")
    if admins is None:
        out += "\n\n⚠️ ตรวจรายชื่อแอดมินไม่ได้ รายชื่อนี้อาจมีแอดมินปนอยู่"
    return out


def kicklist_text(db, chat_id, today, n):
    """ไม่ลงคลิปติดกัน n วันที่จบแล้ว (ไม่นับวันนี้) และเข้ากลุ่มก่อนช่วงนั้น"""
    admins = admin_ids(chat_id)
    skip = admins or set()
    end = date.fromisoformat(today) - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    d0, d1 = start.isoformat(), end.isoformat()
    cands = []
    for uid, name, first in db.execute(
            "SELECT user_id,name,first_day FROM members WHERE chat_id=?", (chat_id,)).fetchall():
        if uid in skip or not first or date.fromisoformat(first) >= start:
            continue  # แอดมิน หรือเพิ่งเข้ากลุ่ม ยังไม่ครบช่วง ผ่อนผัน
        posted = db.execute(
            "SELECT COUNT(*) FROM stats WHERE chat_id=? AND user_id=? "
            "AND day>=? AND day<=? AND videos>0", (chat_id, uid, d0, d1)).fetchone()[0]
        if posted == 0:
            cands.append((uid, name))
    cands.sort(key=lambda c: c[1])
    head = f"👢 ไม่ลงคลิป {n} วันติด ({d0} ถึง {d1} ไม่นับวันนี้)"
    if not cands:
        out = head + "\nไม่มีใครเข้าเกณฑ์"
    else:
        out = (head + f" รวม {len(cands)} คน\n"
               + "\n".join(f"{i}. {x}" for i, x in enumerate(labeled(db, chat_id, cands), 1))
               + KICK_HOWTO)
    gaps = [g for g in gap_days(db, 60) if d0 <= g <= d1]
    if gaps:
        out += "\n\n⚠️ บอทเคยหยุดทำงานนานในวันที่ " + ", ".join(gaps) + " ข้อมูลอาจไม่ครบ ตรวจก่อนแตะ"
    return out


def parse_kick(arg):
    lines = arg.split("\n")
    confirm = False
    if lines and lines[0].strip() == "ยืนยัน":
        confirm, lines = True, lines[1:]
    reqs = []
    for ln in lines:
        ln = re.sub(r"^\s*(?:\d+\s*[\.\)]|[-•*])\s*", "", ln).strip()
        if not ln:
            continue
        mid = re.search(r"\s*\[id\s*(\d+)\]\s*$", ln)
        if mid:
            reqs.append(("id", int(mid.group(1)), ln[:mid.start()].strip()))
        else:
            reqs.append(("name", norm_name(ln), ln))
    return confirm, reqs


def do_kick(db, chat_id, arg, today):
    confirm, reqs = parse_kick(arg)
    if not reqs:
        return "ยังไม่มีรายชื่อ ส่งแบบนี้:\n/kick\n1. ชื่อ\n2. ชื่อ\n(คัดลอกจากรายชื่อของ /never หรือ /kicklist)"
    admins = admin_ids(chat_id)
    if admins is None:
        return "ตรวจรายชื่อแอดมินของกลุ่มไม่ได้ ยังไม่แตะ ลองใหม่อีกครั้ง"
    members = db.execute("SELECT user_id,name FROM members WHERE chat_id=?", (chat_id,)).fetchall()
    by_id = dict(members)
    by_name = {}
    for uid, nm in members:
        by_name.setdefault(norm_name(nm), []).append(uid)

    targets, notfound, ambiguous, admin_skip, seen = [], [], [], [], set()
    for kind, val, raw in reqs:
        if kind == "id":
            if val not in by_id:
                notfound.append(raw or str(val))
                continue
            uid = val
        else:
            ids = by_name.get(val, [])
            if not ids:
                notfound.append(raw)
                continue
            if len(ids) > 1:
                ambiguous.append(raw + " → " + " หรือ ".join(
                    f"{by_id[i]} [id {i}]" for i in ids))
                continue
            uid = ids[0]
        if uid in seen:
            continue
        seen.add(uid)
        if uid in admins:
            admin_skip.append(by_id[uid])
            continue
        targets.append((uid, by_id[uid]))

    notes = ""
    if notfound:
        notes += "\n\n❓ ไม่พบชื่อนี้ในกลุ่ม (สะกดต้องตรงกับที่บอทแสดง):\n" + "\n".join(f"- {x}" for x in notfound)
    if ambiguous:
        notes += "\n\n❓ ชื่อซ้ำกัน ยังไม่แตะ ให้ส่งใหม่พร้อม [id ...]:\n" + "\n".join(f"- {x}" for x in ambiguous)
    if admin_skip:
        notes += "\n\n🛡 เป็นแอดมิน แตะไม่ได้:\n" + "\n".join(f"- {x}" for x in admin_skip)
    if not targets:
        return "ไม่มีใครถูกแตะ" + notes
    if len(targets) > KICK_CONFIRM_OVER and not confirm:
        return (f"จะแตะ {len(targets)} คน ซึ่งเยอะ ยังไม่ได้แตะ\n"
                "ถ้าถูกต้อง ส่งรายชื่อเดิมอีกครั้ง โดยใส่คำว่า ยืนยัน ไว้บรรทัดแรกหลัง /kick\n"
                "/kick\nยืนยัน\n1. ชื่อ ..." + notes)

    ok, failed, stuck = [], [], []
    for uid, name in targets:
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
    out = f"👢 แตะแล้ว {len(ok)} คน\n" + "\n".join(f"- {x}" for x in ok) if ok else "ไม่มีใครถูกแตะ"
    if failed:
        out += "\n\n❗ แตะไม่สำเร็จ (บอทอาจไม่มีสิทธิ์ Ban users):\n" + "\n".join(f"- {x}" for x in failed)
    if stuck:
        out += "\n\n❗ ปลดแบนไม่สำเร็จ (เข้ากลุ่มใหม่ไม่ได้จนกว่าจะปลดเอง):\n" + "\n".join(f"- {x}" for x in stuck)
    return out + notes


# ---------- ตอบคำสั่ง ----------

def send(chat_id, text):
    try:
        for i in range(0, len(text), 3800):
            call("sendMessage", chat_id=chat_id, text=text[i:i + 3800])
        print("sent ok to", chat_id)
    except Exception as e:
        print("send failed", chat_id, e)


def answer(db, chat_id, cmd, arg, today):
    first = (arg.split() or [""])[0]
    num = int(first) if first.isdigit() else None
    if cmd == "/kick":
        return do_kick(db, chat_id, arg, today)
    if cmd == "/top":
        return top_text(db, chat_id, arg, today)
    if cmd == "/never":
        return never_text(db, chat_id, today, arg)
    if cmd == "/history":
        return history_text(db, chat_id, today, max(1, min(num or 7, MAX_HISTORY_DAYS)))
    if cmd == "/kicklist":
        return kicklist_text(db, chat_id, today, max(1, min(num or 1, MAX_KICK_DAYS)))
    day = today
    if first:
        try:
            day = date.fromisoformat(first).isoformat()
        except ValueError:
            return "รูปแบบวันที่ไม่ถูกต้อง ใช้แบบนี้: /daily 2026-10-12"
    if cmd == "/daily":
        return daily_text(db, chat_id, day)
    return report_text(db, chat_id, day) if cmd == "/report" else missing_text(db, chat_id, day)


def run(db, now=None):
    now = now or datetime.now(TZ)
    day = now.date().isoformat()
    _ADMIN_CACHE.clear()

    # บันทึกช่วงที่บอทหยุดทำงานนาน (ใช้เตือนว่าข้อมูลอาจไม่ครบ)
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
    print(f"pending commands: {[(cmd, auth) for _, cmd, _, auth in pending]}, known members: {n_members}")
    for chat_id, cmd, arg, authorized in pending:
        if not authorized:
            print("ignored: command from non-admin")   # ไม่ตอบ เพื่อไม่ให้กลุ่มรก
            continue
        send(chat_id, answer(db, chat_id, cmd, arg, day))

    if NIGHTLY_REPORT and now.hour >= REPORT_HOUR:
        for (chat_id,) in db.execute("SELECT DISTINCT chat_id FROM members").fetchall():
            if meta_get(db, f"report:{chat_id}") != day:
                send(chat_id, daily_text(db, chat_id, day))
                meta_set(db, f"report:{chat_id}", day)
                db.commit()
    db.commit()


if __name__ == "__main__":
    run(init_db(DB_PATH))
