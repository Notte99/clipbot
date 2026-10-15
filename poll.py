cd /mnt/user-data/outputs/clipbot-github && cat > poll.py << 'EOF'
"""ClipBot (โหมด GitHub Actions): ตื่นมาอ่านข้อความใหม่ นับสถิติ ตอบคำสั่ง แล้วจบ

คำสั่ง (ใช้ได้เฉพาะแอดมิน คนอื่นพิมพ์แล้วบอทไม่ตอบ):
  /top [จำนวน|all] [คลิป|รูป|ข้อความ]  อันดับคนลงเยอะสุดตั้งแต่เข้ามา
  /daily [YYYY-MM-DD]                  รายวัน: ใครลงคลิปแล้ว ใครยังไม่ลง
  /never [วันที่เข้ากลุ่มมาแล้วอย่างน้อย] รายชื่อคนที่ไม่เคยลงคลิปเลยตั้งแต่เข้ากลุ่ม
  /kick <แปะรายชื่อจาก /never>         แตะคนที่ระบุออกจากกลุ่ม
  /report, /missing, /history          สถิติรายวัน / ใครยังไม่ลง / ย้อนหลังหลายวัน
"""
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Bangkok")
REPORT_HOUR = 23          # ส่งสรุปอัตโนมัติรอบแรกที่รันหลัง 23:00 เวลาไทย
DB_PATH = "stats.db"
MAX_HISTORY_DAYS = 60
COMMANDS = ("/report", "/missing", "/daily", "/top", "/never", "/history", "/kick")
KICK_CONFIRM_OVER = 10    # แตะเกินกี่คนต้องพิมพ์ ยืนยัน
GAP_HOURS = 20            # บอทหยุดทำงานเกินกี่ชั่วโมงถือว่าข้อมูลช่วงนั้นไม่น่าไว้ใจ


# ---------------------------------------------------------------- Telegram / DB
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


# ---------------------------------------------------------------- สิทธิ์แอดมิน
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


# ---------------------------------------------------------------- อ่านข้อความ
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
            head = text.split(None, 1)
            cmd = head[0].split("@")[0].lower()
            rest = head[1] if len(head) > 1 else ""
            arg = rest if cmd == "/kick" else " ".join(rest.split())  # /kick เก็บขึ้นบรรทัดไว้
            known = cmd in COMMANDS
            if known:
                item = (chat_id, cmd, arg, is_admin(chat_id, user, m))
                if item not in pending:
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


# ---------------------------------------------------------------- รายงาน
def parse_day(arg, today):
    word = arg.split()[0] if arg.split() else ""
    if not word:
        return today, None
    try:
        return date.fromisoformat(word).isoformat(), None
    except ValueError:
        return today, "รูปแบบวันที่ไม่ถูกต้อง ใช้แบบนี้: /daily 2026-10-12"


def report_text(db, chat_id, day):
    rows = db.execute(
        "SELECT name,msgs,videos,photos FROM stats WHERE day=? AND chat_id=? "
        "ORDER BY videos DESC, msgs DESC", (day, chat_id)).fetchall()
    if not rows:
        return f"ยังไม่มีข้อมูลวันที่ {day}"
    return f"📊 สรุปวันที่ {day}\n" + "\n".join(
        f"{n}: 💬{a} 🎬{v} 🖼{p}" for n, a, v, p in rows)


def not_posted(db, chat_id, day):
    """สมาชิกที่เข้ากลุ่มแล้วในวันนั้น แต่ยังไม่ลงคลิป"""
    return db.execute(
        "SELECT name FROM members WHERE chat_id=? AND (first_day IS NULL OR first_day<=?) "
        "AND user_id NOT IN (SELECT user_id FROM stats WHERE day=? AND chat_id=? AND videos>0) "
        "ORDER BY name", (chat_id, day, day, chat_id)).fetchall()


def missing_text(db, chat_id, day):
    rows = not_posted(db, chat_id, day)
    if not rows:
        return f"✅ วันที่ {day} ทุกคนลงคลิปครบแล้ว"
    return f"❌ ยังไม่ลงคลิปวันที่ {day} ({len(rows)} คน)\n" + "\n".join(
        f"{i}. {r[0]}" for i, r in enumerate(rows, 1))


def daily_text(db, chat_id, day):
    """รายวัน: ใครลงคลิปแล้ว / ใครยังไม่ลง"""
    posted = db.execute(
        "SELECT m.name, s.videos FROM stats s JOIN members m "
        "ON m.chat_id=s.chat_id AND m.user_id=s.user_id "
        "WHERE s.day=? AND s.chat_id=? AND s.videos>0 ORDER BY s.videos DESC, m.name",
        (day, chat_id)).fetchall()
    miss = not_posted(db, chat_id, day)
    out = [f"📅 วันที่ {day}", f"✅ ลงคลิปแล้ว ({len(posted)} คน)"]
    out += [f"{i}. {n} (🎬{v})" for i, (n, v) in enumerate(posted, 1)] or ["- ยังไม่มี"]
    out += ["", f"❌ ยังไม่ลง ({len(miss)} คน)"]
    out += [f"{i}. {r[0]}" for i, r in enumerate(miss, 1)] or ["- ไม่มี ครบทุกคน 🎉"]
    return "\n".join(out)


def top_text(db, chat_id, arg):
    words = arg.lower().split()
    limit = 10
    key, label = 1, "คลิป"
    for w in words:
        if w == "all":
            limit = None
        elif w.isdigit():
            limit = max(1, int(w))
        elif w in ("รูป", "photos", "photo"):
            key, label = 2, "รูป"
        elif w in ("ข้อความ", "msgs", "msg"):
            key, label = 3, "ข้อความ"
    rows = db.execute(
        "SELECT m.name, COALESCE(SUM(s.videos),0), COALESCE(SUM(s.photos),0), "
        "COALESCE(SUM(s.msgs),0) FROM members m LEFT JOIN stats s "
        "ON s.chat_id=m.chat_id AND s.user_id=m.user_id "
        "WHERE m.chat_id=? GROUP BY m.user_id", (chat_id,)).fetchall()
    if not rows:
        return "ยังไม่มีสมาชิกในระบบ"
    rows.sort(key=lambda r: (-r[key], -r[1], -r[2], -r[3], r[0]))
    shown = rows if limit is None else rows[:limit]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 อันดับตั้งแต่เข้ามา (เรียงตาม{label}) แสดง {len(shown)}/{len(rows)} คน"]
    for i, (n, v, p, t) in enumerate(shown):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {n} — 🎬{v} 🖼{p} 💬{t}")
    if limit is not None and len(rows) > limit:
        lines.append("\nดูทั้งหมด: /top all")
    return "\n".join(lines)


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


# ---------------------------------------------------------------- ระบบแตะ
def gap_days_all(db):
    return sorted(k[0][4:] for k in db.execute(
        "SELECT key FROM meta WHERE key LIKE 'gap:%'").fetchall())


def total_videos(db, chat_id, uid):
    return db.execute("SELECT COALESCE(SUM(videos),0) FROM stats WHERE chat_id=? AND user_id=?",
                      (chat_id, uid)).fetchone()[0]


def never_candidates(db, chat_id, today, min_days, skip=frozenset()):
    """ไม่เคยลงคลิปเลยตั้งแต่เข้ากลุ่ม และเข้ามาแล้วอย่างน้อย min_days วัน"""
    cutoff = date.fromisoformat(today) - timedelta(days=min_days)
    out = []
    for uid, name, first in db.execute(
            "SELECT user_id,name,first_day FROM members WHERE chat_id=?", (chat_id,)).fetchall():
        if uid in skip or not first or date.fromisoformat(first) > cutoff:
            continue
        if total_videos(db, chat_id, uid) == 0:
            out.append((uid, name, first))
    out.sort(key=lambda c: (c[2], c[1]))
    return out


def never_text(db, chat_id, today, min_days):
    admins = admin_ids(chat_id)
    cands = never_candidates(db, chat_id, today, min_days, admins or frozenset())
    head = f"🚫 ไม่เคยลงคลิปเลยตั้งแต่เข้ากลุ่ม (เข้ามาแล้วอย่างน้อย {min_days} วัน)"
    if not cands:
        out = head + "\nไม่มีใครเข้าเกณฑ์"
    else:
        out = (head + f" รวม {len(cands)} คน\n"
               + "\n".join(f"{i}. {nm} · {uid}" for i, (uid, nm, _) in enumerate(cands, 1))
               + "\n\nจะแตะ: พิมพ์ /kick แล้วแปะรายชื่อนี้ต่อท้าย (ลบบรรทัดที่ไม่อยากแตะออกก่อนได้)")
    gaps = gap_days_all(db)
    if gaps:
        out += ("\n\n⚠ บอทเคยหยุดทำงานนานในวันที่ " + ", ".join(gaps[-5:])
                + " คนที่เข้ามาก่อนวันนั้นอาจมีคลิปที่นับไม่ครบ ตรวจซ้ำก่อนแตะ")
    if admins is None:
        out += "\n\n⚠ ตรวจรายชื่อแอดมินไม่ได้ รายชื่อนี้อาจมีแอดมินปนอยู่"
    return out


def resolve_targets(db, chat_id, text):
    """อ่านรายชื่อที่แปะมา: ถ้ามี ID ใช้ ID เป็นหลัก ไม่งั้นจับคู่ตามชื่อ"""
    members = db.execute("SELECT user_id,name,first_day FROM members WHERE chat_id=?",
                         (chat_id,)).fetchall()
    by_id = {u: (n, f) for u, n, f in members}
    by_name = {}
    for u, n, _ in members:
        by_name.setdefault(n.strip().lower(), []).append(u)
    found, ambiguous, unknown = {}, [], []
    ids = [int(x) for x in re.findall(r"(?<!\d)\d{6,15}(?!\d)", text)]
    if ids:
        for i in ids:
            if i in by_id:
                found[i] = by_id[i][0]
            else:
                unknown.append(str(i))
    else:
        for line in text.splitlines():
            clean = re.sub(r"^\s*(\d+[.)]|[-•*])\s*", "", line).strip()
            if not clean:
                continue
            cand = by_name.get(clean.lower(), [])
            if len(cand) == 1:
                found[cand[0]] = by_id[cand[0]][0]
            elif len(cand) > 1:
                ambiguous.append(clean)
            else:
                unknown.append(clean)
    return found, ambiguous, unknown, by_id


def do_kick(db, chat_id, arg, today, authorized):
    if not authorized:
        return "คำสั่งนี้ใช้ได้เฉพาะแอดมินของกลุ่ม"
    confirm = "ยืนยัน" in arg
    arg = arg.replace("ยืนยัน", "")
    found, ambiguous, unknown, by_id = resolve_targets(db, chat_id, arg)
    if not (found or ambiguous or unknown):
        return "ต้องแปะรายชื่อต่อท้าย /kick (คัดลอกจากผลของ /never)"
    admins = admin_ids(chat_id)
    if admins is None:
        return "ตรวจรายชื่อแอดมินของกลุ่มไม่ได้ ยังไม่แตะ ลองใหม่อีกครั้ง"
    gaps = gap_days_all(db)

    targets, skipped = [], []
    for uid, name in found.items():
        if uid in admins:
            skipped.append((name, "เป็นแอดมิน/เจ้าของกลุ่ม"))
            continue
        reasons = []
        vids = total_videos(db, chat_id, uid)
        if vids > 0:
            reasons.append(f"เคยลงคลิปแล้ว {vids} คลิป")
        first = by_id[uid][1]
        if first and any(g >= first for g in gaps):
            reasons.append("ข้อมูลช่วงที่บอทหยุดทำงานอาจไม่ครบ")
        if reasons and not confirm:
            skipped.append((name, ", ".join(reasons)))
            continue
        targets.append((uid, name))

    if len(targets) > KICK_CONFIRM_OVER and not confirm:
        return (f"จะแตะ {len(targets)} คน ซึ่งเยอะผิดปกติ ตรวจรายชื่อด้วย /never ก่อน\n"
                "ถ้าถูกต้องแล้ว ส่งคำสั่งเดิมอีกครั้งโดยเติมคำว่า ยืนยัน")

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

    out = [f"👢 แตะแล้ว {len(ok)} คน"] + [f"- {x}" for x in ok]
    if skipped:
        out += ["", "⏭ ข้าม (ยังไม่แตะ):"] + [f"- {n} ({why})" for n, why in skipped]
        if any(n for n, why in skipped if "แอดมิน" not in why):
            out.append("ถ้าตรวจแล้วอยากแตะจริง เติมคำว่า ยืนยัน ต่อท้ายคำสั่ง")
    if ambiguous:
        out += ["", "❓ ชื่อซ้ำหลายคน แยกไม่ได้ (ใช้รายการที่มีเลข ID):"] + [f"- {x}" for x in ambiguous]
    if unknown:
        out += ["", "❓ ไม่พบในรายชื่อสมาชิกที่บอทรู้จัก:"] + [f"- {x}" for x in unknown]
    if failed:
        out += ["", "❗ แตะไม่สำเร็จ (บอทอาจไม่มีสิทธิ์ Ban users):"] + [f"- {x}" for x in failed]
    if stuck:
        out += ["", "❗ ปลดแบนไม่สำเร็จ (กลับเข้ากลุ่มไม่ได้จนกว่าจะปลดเอง):"] + [f"- {x}" for x in stuck]
    return "\n".join(out)


# ---------------------------------------------------------------- ส่งข้อความ / ตอบคำสั่ง
def chunks(text, limit=3800):
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                yield cur
                cur = ""
            yield line[:limit]
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            yield cur
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        yield cur


def send(chat_id, text):
    try:
        for part in chunks(text):
            call("sendMessage", chat_id=chat_id, text=part)
        print("sent ok to", chat_id)
    except Exception as e:
        print("send failed", chat_id, e)


def answer(db, chat_id, cmd, arg, today, authorized=False):
    first = arg.split()[0] if arg.split() else ""
    if cmd == "/kick":
        return do_kick(db, chat_id, arg, today, authorized)
    if cmd == "/never":
        return never_text(db, chat_id, today, int(first) if first.isdigit() else 1)
    if cmd == "/top":
        return top_text(db, chat_id, arg)
    if cmd == "/history":
        n = int(first) if first.isdigit() else 7
        return history_text(db, chat_id, today, max(1, min(n, MAX_HISTORY_DAYS)))
    day, err = parse_day(arg, today)
    if err:
        return err
    if cmd == "/daily":
        return daily_text(db, chat_id, day)
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
    print(f"pending commands: {[(c, cmd) for c, cmd, _, _ in pending]}, known members: {n_members}")
    for chat_id, cmd, arg, authorized in pending:
        if not authorized:
            print("ignored: command from non-admin")   # ไม่ตอบ เพื่อไม่ให้กลุ่มรก
            continue
        send(chat_id, answer(db, chat_id, cmd, arg, day, authorized))

    if now.hour >= REPORT_HOUR:
        for (chat_id,) in db.execute("SELECT DISTINCT chat_id FROM members").fetchall():
            if meta_get(db, f"report:{chat_id}") != day:
                send(chat_id, daily_text(db, chat_id, day))
                meta_set(db, f"report:{chat_id}", day)
                db.commit()
    db.commit()


if __name__ == "__main__":
    run(init_db(DB_PATH))
EOF
python3 -c "import ast;ast.parse(open('poll.py').read());print('syntax ok')"
