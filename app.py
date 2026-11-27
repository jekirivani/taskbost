#!/usr/bin/env python3
"""TASKBOST - toko Follow / Like / Comment (stdlib only, untuk Termux).
Jalankan: python app.py  ->  http://localhost:8081
"""
import os
import re
import html
import json
import secrets
import sqlite3
import hashlib
import hmac
import time
import smtplib
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse, urlencode, quote, unquote
from email.message import EmailMessage

PORT = int(os.environ.get("PORT", "8081")) 
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "taskbost.db")
PORT = 8081

# Akun admin tetap
ADMIN_USER = "admin"
ADMIN_EMAIL = "admin@taskbost.local"
ADMIN_PASS = os.environ.get("TASKBOST_ADMIN_PASS", "taskbost123")

# SMTP opsional. Jika kosong, link reset password dicetak di terminal.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

# Chat admin via WhatsApp
WA_NUMBER = "6285607137599"
WA_TEXT = "Halo Admin TASKBOST, saya mau tanya order"


def wa_link(text):
    return f"https://wa.me/{WA_NUMBER}?text={quote(text)}"


WA_LINK = wa_link(WA_TEXT)

# Galeri bukti foto: folder bukti/ di samping app.py
BUKTI_DIR = os.path.join(BASE_DIR, "bukti")
BUKTI_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".webp": "image/webp"}
MAX_UPLOAD = 8 * 1024 * 1024  # 8 MB per request

# ---------------------------------------------------------------- harga (dikunci)
PRICES = {
    "follow":  {"standard": 200, "premium": 250},
    "like":    {"standard": 50,  "premium": 75},
    "comment": {"standard": 200, "premium": 250},
}
QTY_MIN, QTY_MAX = 100, 1000
QUICK_QTY = [100, 150, 250, 1000]
MODES = {"standard": "Standard", "premium": "Premium / FAST"}
TIPES = {"RANDOM": "Random (member yang menentukan isi)",
         "CUSTOM": "Custom (kamu yang menulis isi)"}

# sosmed -> (nama, [(kode_layanan, label_layanan), ...])
SOSMED = {
    "ig": ("Instagram", [("follow", "Follow"), ("like", "Like"), ("comment", "Comment")]),
    "tiktok": ("TikTok", [("follow", "Follow"), ("like", "Like"), ("comment", "Comment")]),
    "threads": ("Threads", [("follow", "Follow"), ("like", "Like"), ("reply", "Comment / Reply")]),
    "fb": ("Facebook", [("follow", "Follow"), ("like", "Like"), ("comment", "Comment")]),
    "wa": ("WhatsApp", [("channel", "Follow Channel WA"),
                        ("chat", "Comment / Kirim Chat ke Nomor")]),
}
PRICE_GROUP = {"follow": "follow", "like": "like", "comment": "comment",
               "reply": "comment", "channel": "follow", "chat": "comment"}
NEED_TIPE = ["comment", "reply", "chat"]
NOGENDER = ["channel", "chat"]  # layanan WA tidak pakai gender
HINT = {
    "channel": ["Link Channel WA", "https://whatsapp.com/channel/..."],
    "chat": ["Nomor WA tujuan", "contoh: 0812-3456-7890"],
    "default": ["Username atau link", "contoh: @namaakun atau https://..."],
}

GENDERS = {"female": "Female", "male": "Male", "bebas": "Bebas", "campur": "Campur"}
DURASI = [6, 14, 30]

# Admin hanya boleh mengubah menunggu -> diproses. Selesai otomatis setelah durasi.
ADMIN_STATUSES = ["menunggu", "diproses"]

# Jaminan: (nilai, keterangan, ikon, warna ikon, warna latar)
JAMINAN = [
    ("99.2%", "Task Valid", "check", "#1FA463", "#E3F8EC"),
    ("100%", "Uang kembali jika tidak valid", "wallet", "#B7892E", "#FFF1CC"),
    ("Gratis", "Jika drop, diganti gratis", "gift", "#E5484D", "#FDE7E8"),
    ("100%", "Orang asli, bukan bot", "users", "#2F6FEB", "#E6F0FF"),
    ("24/7", "CS via WA", "wa", "#1FA463", "#E3F8EC"),
]

# Ikon layanan: (ikon, warna, latar)
LAY_ICON = {
    "follow": ("user-plus", "#1FA463", "#E3F8EC"),
    "like": ("heart", "#E5484D", "#FDE7E8"),
    "comment": ("chat", "#2F6FEB", "#E6F0FF"),
    "reply": ("chat", "#2F6FEB", "#E6F0FF"),
    "channel": ("user-plus", "#1FA463", "#E3F8EC"),
    "chat": ("chat", "#2F6FEB", "#E6F0FF"),
}

# Bagian "Dikerjakan Akun Real, Bukan Bot"
AKUN_STEPS = [
    ("01", "Buat akun", "Member daftar pakai akun asli, dicek dulu sama admin, "
                        "kalau akun bodong / bot langsung ditolak."),
    ("02", "Pilih task", "Member pilih task Follow / Like / Comment sesuai platform dan gender."),
    ("03", "Kirim bukti", "Member kerjakan, kirim bukti, admin cek manual satu-satu. "
                          "Kalau curang / pakai bot, ditolak, tidak dibayar."),
    ("04", "Cairkan saldo", "Kalau bukti valid, baru saldo masuk, baru bisa cair."),
]

SESSIONS = {}      # sid -> user_id
RESET_TOKENS = {}  # token -> (user_id, expired_at)


# ---------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def q(sql, args=(), one=False):
    c = db()
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return (rows[0] if rows else None) if one else rows
    finally:
        c.close()


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'customer',
        saldo INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        paket TEXT NOT NULL,
        harga INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'menunggu',
        catatan TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    """)
    c.commit()
    c.close()


def seed_admin():
    """Pastikan akun admin tetap ada dengan password terbaru."""
    ph = hash_pw(ADMIN_PASS)
    row = q("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,), one=True)
    try:
        if row:
            q("UPDATE users SET password_hash=?, role='admin', username=? WHERE id=?",
              (ph, ADMIN_USER, row["id"]))
        else:
            q("INSERT INTO users (username,email,password_hash,role) VALUES (?,?,?,?)",
              (ADMIN_USER, ADMIN_EMAIL, ph, "admin"))
        print(f"[+] Admin siap: {ADMIN_EMAIL}")
    except sqlite3.IntegrityError:
        print("[!] Username 'admin' sudah dipakai akun lain. Hapus akun itu dulu.")


def auto_selesai():
    """Order yang sudah lewat durasinya otomatis jadi selesai."""
    now = datetime.now()
    rows = q("SELECT id, catatan, created_at FROM orders "
             "WHERE status IN ('menunggu','diproses')")
    for o in rows:
        m = re.search(r"Durasi: (\d+)", o["catatan"] or "")
        if not m:
            continue
        try:
            mulai = datetime.strptime(o["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if now >= mulai + timedelta(days=int(m.group(1))):
            q("UPDATE orders SET status='selesai' WHERE id=?", (o["id"],))


# ---------------------------------------------------------------- helpers
def h(s):
    return html.escape(str(s or ""))


def rp(n):
    return "Rp " + f"{n:,}".replace(",", ".")


def hash_pw(pw):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()
    return f"{salt}${digest}"


def check_pw(pw, stored):
    try:
        salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()
    return hmac.compare_digest(test, digest)


def send_mail(to, subject, text):
    if not SMTP_HOST:
        print(f"[RESET] {to}: {text}", flush=True)
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to
        msg.set_content(text)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        traceback.print_exc()
        print(f"[RESET fallback] {to}: {text}", flush=True)


def login_cookie(uid):
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = uid
    return f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"


def go(r, path, **kw):
    r.redirect(path + ("?" + urlencode(kw) if kw else ""))


def alert(msg, kind):
    return f'<p class="alert {kind}">{h(msg)}</p>' if msg else ""


def hitung_harga(layanan, mode, qty):
    return PRICES[PRICE_GROUP[layanan]][mode] * qty


def clean_target(layanan, raw):
    raw = raw.strip()
    if layanan == "channel":
        return raw if raw.startswith(("http://", "https://")) else None
    if layanan == "chat":
        digits = re.sub(r"[\s\-+()]", "", raw)
        return digits if re.fullmatch(r"(0|62)\d{7,13}", digits) else None
    return raw if 1 <= len(raw) <= 300 else None


def konfirmasi_link(o):
    """Link WA konfirmasi bayar, diisi otomatis dari order yang diklik."""
    m = re.search(r"Qty: (\d+)", o["catatan"] or "")
    qty = m.group(1) if m else "-"
    paket = (o["paket"] or "").replace(" · ", " - ")
    text = (f"Halo Admin TASKBOST, saya sudah order #{o['id']}, paket: {paket}, "
            f"Qty: {qty}, Total: {rp(o['harga'])}, saya mau kirim bukti bayar.")
    return wa_link(text)


# ---------------------------------------------------------------- upload & galeri bukti
def list_bukti():
    if not os.path.isdir(BUKTI_DIR):
        return []
    return sorted(f for f in os.listdir(BUKTI_DIR)
                  if os.path.splitext(f)[1].lower() in BUKTI_EXT
                  and os.path.isfile(os.path.join(BUKTI_DIR, f)))


def cek_gambar(data, ext):
    """Pastikan isi file benar-benar gambar, bukan file lain yang diganti namanya."""
    if ext in (".jpg", ".jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def parse_multipart(body, ctype):
    """Parser multipart sederhana (tanpa modul cgi yang sudah dihapus di Python baru)."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype)
    if not m:
        return {}, {}
    boundary = (m.group(1) or m.group(2)).encode()
    fields, files = {}, {}
    for part in body.split(b"--" + boundary):
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        disp = ""
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if line.lower().startswith("content-disposition"):
                disp = line
        name = re.search(r'name="([^"]*)"', disp)
        if not name:
            continue
        fname = re.search(r'filename="([^"]*)"', disp)
        if fname:
            files[name.group(1)] = (os.path.basename(fname.group(1)), data)
        else:
            fields[name.group(1)] = data.decode("utf-8", "replace").strip()
    return fields, files


def do_upload(r):
    item = r.files.get("foto")
    if not item or not item[1]:
        return go(r, "/admin", err="Pilih foto dulu sebelum upload.")
    fname, data = item
    ext = os.path.splitext(fname)[1].lower()
    if ext not in BUKTI_EXT or not cek_gambar(data, ext):
        return go(r, "/admin", err="Format harus JPG, PNG, atau WEBP yang benar.")
    if len(data) > MAX_UPLOAD:
        return go(r, "/admin", err="Ukuran foto maksimal 8 MB.")
    os.makedirs(BUKTI_DIR, exist_ok=True)
    name = f"bukti-{int(time.time())}-{secrets.token_hex(4)}{ext}"
    with open(os.path.join(BUKTI_DIR, name), "wb") as fh:
        fh.write(data)
    go(r, "/admin", ok="Foto bukti berhasil diupload dan sudah tampil di Galeri Bukti.")


def do_bukti_hapus(r, f):
    name = os.path.basename(f.get("name", ""))
    fp = os.path.join(BUKTI_DIR, name)
    if name in list_bukti() and os.path.isfile(fp):
        os.remove(fp)
        return go(r, "/admin", ok="Foto bukti dihapus dari Galeri.")
    go(r, "/admin", err="Foto tidak ditemukan.")


# ---------------------------------------------------------------- ikon (SVG bawaan, offline)
ICON = {
    "check": '<circle cx="12" cy="12" r="9"/><polyline points="8 12.5 11 15.5 16 9.5"/>',
    "wallet": '<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M3 10h18"/><circle cx="16.5" cy="14.5" r="1.3" fill="currentColor"/>',
    "gift": '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M5 12v8h14v-8M12 8v12"/><path d="M12 8c-1.5-3-5-3-5-1s3 1 5 1zm0 0c1.5-3 5-3 5-1s-3 1-5 1z"/>',
    "users": '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-6 7-6s7 2 7 6"/><path d="M16 4.5a4 4 0 0 1 0 7M22 21c0-3-1.5-5-4-5.8"/>',
    "user-plus": '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-6 7-6s7 2 7 6"/><path d="M19 8v6M16 11h6"/>',
    "heart": '<path d="M12 20s-7.5-4.6-9.2-9.1C1.6 7.6 3.8 4.5 7 4.5c2 0 3.5 1.2 5 3 1.5-1.8 3-3 5-3 3.2 0 5.4 3.1 4.2 6.4C19.5 15.4 12 20 12 20z"/>',
    "chat": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.7A8 8 0 1 1 21 12z"/>',
}


def ico(name, size=24, color="currentColor", stroke=2.0):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICON[name]}</svg>')


SOS_ICON = {
    "ig": '<rect x="2" y="2" width="20" height="20" rx="6" fill="url(#igg)"/>'
          '<circle cx="12" cy="12" r="4.5" fill="none" stroke="#fff" stroke-width="2"/>'
          '<circle cx="17.5" cy="6.5" r="1.4" fill="#fff"/>',
    "tiktok": '<path d="M16 3c.3 2.4 1.8 3.9 4 4.1v3.1c-1.5 0-2.9-.5-4-1.3v6.3a5.2 5.2 0 1 1-5.2-5.2c.3 0 .6 0 .9.1v3.2a2.1 2.1 0 1 0 1.2 1.9V3z" fill="#120F0C"/>',
    "threads": '<circle cx="12" cy="12" r="11" fill="#120F0C"/>'
               '<text x="12" y="16.5" text-anchor="middle" font-size="13" font-weight="700" fill="#fff" font-family="Arial">@</text>',
    "fb": '<circle cx="12" cy="12" r="11" fill="#1877F2"/>'
          '<path d="M13.3 20v-6.4h2.2l.3-2.7h-2.5V9.2c0-.8.2-1.3 1.3-1.3h1.4V5.5c-.3 0-1.1-.1-2.1-.1-2.1 0-3.5 1.3-3.5 3.6v2h-2.3v2.7h2.3V20z" fill="#fff"/>',
    "wa": '<circle cx="12" cy="12" r="11" fill="#25D366"/>'
          '<path d="M12 5.5a6.5 6.5 0 0 0-5.6 9.8L5.5 18.5l3.3-.9A6.5 6.5 0 1 0 12 5.5z" fill="none" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/>',
}
SOS_DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
            '<linearGradient id="igg" x1="0" y1="1" x2="1" y2="0">'
            '<stop offset="0" stop-color="#FEDA75"/><stop offset=".5" stop-color="#FA7E1E"/>'
            '<stop offset="1" stop-color="#D62976"/></linearGradient></defs></svg>')


def sos_svg(key, size=22):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" aria-hidden="true">'
            f'{SOS_ICON[key]}</svg>')


def lay_ic(key, size=34):
    icon, color, _ = LAY_ICON[key]
    return ico(icon, size, color, 2.2)


def jam_ic(name, color):
    if name == "wa":
        return sos_svg("wa", 30)
    return ico(name, 28, color, 2.2)


# Ikon WA putih untuk tombol melayang (path disederhanakan, offline)
WA_WHITE = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="#fff" aria-hidden="true">'
            '<path d="M12.04 2a9.9 9.9 0 0 0-8.5 15L2 22l5.1-1.5A9.9 9.9 0 1 0 12.04 2zm0 1.8a8.1 8.1 0 1 1-4.2 15.02l-.3-.17-3 .88.9-2.92-.2-.31A8.1 8.1 0 0 1 12.04 3.8zm-3.2 4.3c-.2 0-.5.07-.73.32-.25.27-.95.93-.95 2.27s.98 2.63 1.11 2.81c.14.18 1.9 3.02 4.7 4.1 2.32.91 2.8.73 3.3.68.5-.05 1.62-.66 1.85-1.3.23-.64.23-1.19.16-1.3-.07-.12-.25-.18-.52-.32-.27-.14-1.62-.8-1.87-.89-.25-.09-.43-.14-.62.14-.18.27-.7.89-.86 1.07-.16.18-.32.2-.59.07-.27-.14-1.15-.42-2.19-1.35-.81-.72-1.36-1.61-1.52-1.88-.16-.27-.02-.42.12-.55.12-.12.27-.32.4-.48.14-.16.18-.27.27-.45.09-.18.05-.34-.02-.48-.07-.14-.6-1.5-.83-2.04-.22-.52-.44-.45-.6-.46h-.51z"/>'
            '</svg>')


# ---------------------------------------------------------------- layout
CSS = """
:root{
  --ink:#120F0C;--ink2:#1E1913;--cream:#FFF8E7;--paper:#FFFFFF;
  --gold:#D4A537;--gold2:#FFC93C;--green:#1FA463;--red:#E5484D;--blue:#2F6FEB;
  --line:#EFE4CC;--muted:#6F6556;
  --shadow:0 14px 34px rgba(18,15,12,.08);
  --shadow-lg:0 22px 50px rgba(18,15,12,.16);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;line-height:1.65;
  background:radial-gradient(1100px 520px at 12% -8%,#FFF1CC 0,transparent 62%),radial-gradient(900px 480px at 100% 0%,#FBE7B5 0,transparent 55%),var(--cream);
  min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:var(--ink)}
main{max-width:1080px;margin:0 auto;padding:0 20px 36px}
section{margin:52px 0}
h2{font-size:23px;margin:0 0 16px;font-weight:900;letter-spacing:-.01em}
h2::after{content:"";display:block;width:46px;height:3px;border-radius:3px;background:linear-gradient(90deg,var(--gold),var(--gold2));margin:9px 0 0}
h2.center::after{margin:9px auto 0}
.center{text-align:center}
.lead{color:var(--muted);max-width:660px;margin:8px auto 26px;text-align:center;font-size:15.5px}

/* header */
.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;padding:14px 22px;
  background:linear-gradient(100deg,#120F0C 0%,#1E1913 100%);color:var(--cream);position:sticky;top:0;z-index:20;
  box-shadow:0 8px 24px rgba(18,15,12,.22);border-bottom:1px solid rgba(212,165,55,.25)}
.top a{color:var(--cream)}
.logo{display:flex;align-items:center;gap:11px;font-weight:900;letter-spacing:.2em;text-decoration:none;font-size:16px;color:var(--cream)}
.mark{width:34px;height:34px;background:linear-gradient(135deg,#FFE27A,#D4A537);color:var(--ink);border-radius:10px;display:grid;place-items:center;font-size:17px;font-weight:900;letter-spacing:0;box-shadow:0 6px 16px rgba(212,165,55,.45)}
nav{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
nav a.plain{margin-left:8px;text-decoration:none;font-weight:700;font-size:15px;opacity:.92;padding:6px 2px;border-bottom:2px solid transparent;transition:border-color .2s,opacity .2s}
nav a.plain:hover{opacity:1;border-bottom-color:var(--gold2)}
.inline{display:inline}
.link{background:none;border:0;color:var(--cream);cursor:pointer;font-size:15px;font-weight:700;padding:0 0 0 8px;opacity:.92;font-family:inherit}
.link:hover{opacity:1;color:var(--gold2)}
.top .btn-login{background:linear-gradient(135deg,#FFE27A,#D4A537);color:var(--ink);border-color:transparent}
.top .btn-daftar{background:transparent;color:var(--cream);border:1.5px solid rgba(255,248,231,.5);box-shadow:none}

/* hero */
.hero{display:grid;grid-template-columns:1.05fr .95fr;gap:28px;align-items:center;margin-top:26px;padding:50px 42px;border-radius:32px;overflow:hidden;
  background:radial-gradient(600px 300px at 90% 20%,rgba(255,226,122,.35),transparent 60%),linear-gradient(115deg,#120F0C 0%,#1E1913 52%,#8C6A1C 100%);
  color:var(--cream);box-shadow:var(--shadow-lg);position:relative}
@media(max-width:760px){.hero{grid-template-columns:1fr;padding:32px 24px}.hero-art{order:-1}}
.kicker{display:inline-block;background:linear-gradient(135deg,#FFE27A,#D4A537);color:var(--ink);font-weight:900;font-size:11.5px;letter-spacing:.18em;padding:7px 14px;border-radius:999px;margin-bottom:16px;box-shadow:0 6px 14px rgba(212,165,55,.35)}
.hero h1{font-size:clamp(30px,5vw,50px);line-height:1.1;margin:0 0 16px;font-weight:900;letter-spacing:-.02em}
.hero h1 em{font-style:normal;background:linear-gradient(90deg,#FFE27A,#FFC93C);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{color:rgba(255,248,231,.85);font-size:16.5px;max-width:530px;margin:0 0 18px}
.hero-points{list-style:none;padding:0;margin:0 0 26px;display:grid;gap:10px}
.hero-points li{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15px}
.hero-btns{display:flex;flex-wrap:wrap;gap:12px}
.hero-art{display:flex;justify-content:center}
.hero-art svg{width:100%;max-width:340px;height:auto;filter:drop-shadow(0 20px 30px rgba(0,0,0,.35))}

/* tombol */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:13px 28px;border-radius:999px;text-decoration:none;font-weight:800;border:1.5px solid transparent;cursor:pointer;font-size:15px;box-shadow:0 8px 20px rgba(18,15,12,.14);transition:transform .15s,box-shadow .25s,filter .2s;font-family:inherit;letter-spacing:.01em}
.btn:hover{box-shadow:0 12px 26px rgba(18,15,12,.22);filter:brightness(1.04);transform:translateY(-1px)}
.btn:active{transform:translateY(0) scale(.98)}
.btn-login{background:linear-gradient(135deg,#1E1913,#120F0C);color:var(--cream);border-color:rgba(212,165,55,.7)}
.btn-daftar{background:linear-gradient(135deg,#FFE27A,#D4A537);color:var(--ink);border-color:transparent}
.btn-order{background:linear-gradient(135deg,#2BC278,#1FA463);color:#fff;width:100%;font-size:17px;padding:17px;box-shadow:0 14px 28px rgba(31,164,99,.3)}
.btn.sm{padding:8px 16px;font-size:13px;box-shadow:none}
.btn-wa{background:linear-gradient(135deg,#34E07A,#25D366);color:#fff;border-color:transparent;padding:10px 18px;font-size:13px;margin-top:14px}
.btn-konf{background:linear-gradient(135deg,#34E07A,#25D366);color:#fff;border-color:transparent;padding:8px 15px;font-size:13px;box-shadow:none;white-space:nowrap}

/* tombol WA melayang */
.wa-float{position:fixed;right:20px;bottom:20px;z-index:50;display:inline-flex;align-items:center;gap:9px;background:linear-gradient(135deg,#34E07A,#25D366);color:#fff;text-decoration:none;font-weight:800;font-size:15px;padding:13px 20px 13px 15px;border-radius:999px;box-shadow:0 12px 28px rgba(37,211,102,.4);transition:transform .2s,box-shadow .25s}
.wa-float:hover{transform:translateY(-3px);box-shadow:0 16px 34px rgba(37,211,102,.5)}
.wa-float svg{flex:none}
@media(max-width:480px){.wa-float span{display:none}.wa-float{padding:15px;border-radius:50%}}

/* jaminan */
.jamin{display:grid;grid-template-columns:repeat(5,1fr);gap:14px}
@media(max-width:860px){.jamin{grid-template-columns:repeat(2,1fr)}}
.jam{background:rgba(255,255,255,.92);border-radius:22px;padding:22px 14px;text-align:center;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.16);transition:transform .25s,box-shadow .25s}
.jam:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg)}
.jam .ic{width:58px;height:58px;border-radius:18px;display:grid;place-items:center;margin:0 auto 14px}
.jam b{display:block;font-size:21px;font-weight:900;letter-spacing:-.01em}
.jam span{font-size:12.5px;color:var(--muted);line-height:1.45;display:block}

/* bukti freelance */
.fl-box{max-width:540px;margin:0 auto;background:linear-gradient(180deg,#fff,#FFFBF0);border-radius:26px;padding:38px 26px;text-align:center;box-shadow:var(--shadow-lg);border:1px solid rgba(212,165,55,.2)}
.fl-ic{width:80px;height:80px;border-radius:26px;background:linear-gradient(135deg,#E3F8EC,#C6F0D8);display:grid;place-items:center;margin:0 auto 16px;box-shadow:0 12px 24px rgba(31,164,99,.18)}
.fl-num{font-size:60px;font-weight:900;color:var(--ink);line-height:1;letter-spacing:-.03em}
.fl-label{font-size:20px;font-weight:900;margin-top:8px}
.fl-note{color:var(--muted);font-size:14.5px;margin-top:12px}

/* cara bayar */
.bayar{background:#fff;border-radius:26px;padding:30px;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.16)}
.bayar-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:4px}
@media(max-width:720px){.bayar-grid{grid-template-columns:1fr}}
.bayar-step{background:linear-gradient(180deg,#FFFBF0,#FFF5DA);border-radius:20px;padding:20px;border:1px solid rgba(212,165,55,.18)}
.bayar-no{width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#FFE27A,#D4A537);display:grid;place-items:center;font-size:20px;font-weight:900;margin-bottom:12px;box-shadow:0 8px 16px rgba(212,165,55,.35)}
.bayar-step p{margin:0;color:var(--ink);font-weight:600;font-size:15px}
.bayar-note{color:var(--muted);font-size:13.5px;margin:18px 0 0;text-align:center}

/* sosmed */
.sos-row{display:flex;flex-wrap:wrap;gap:12px;justify-content:center}
.sos{position:relative;display:inline-flex;align-items:center;gap:9px;padding:11px 18px;border-radius:999px;border:1.5px solid transparent;background:#fff;box-shadow:0 8px 18px rgba(18,15,12,.08);cursor:pointer;font-weight:800;font-size:14.5px;transition:transform .2s,box-shadow .2s}
.sos:hover{transform:translateY(-2px)}
.sos input{position:absolute;opacity:0;width:1px;height:1px;margin:0;pointer-events:none}
.sos.ig{background:#FFF0F6;border-color:#FFD0E4}
.sos.tiktok{background:#F3F4F6;border-color:#D5D9DF}
.sos.threads{background:#F3F4F6;border-color:#D5D9DF}
.sos.fb{background:#EEF4FF;border-color:#C6DAFB}
.sos.wa{background:#EAFAF0;border-color:#BDEBCD}
.sos:has(input:checked){border-color:var(--ink);box-shadow:0 12px 24px rgba(18,15,12,.2);transform:translateY(-1px)}

/* kartu & layanan */
.card{background:#fff;border-radius:24px;padding:26px;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.14);transition:transform .25s,box-shadow .25s}
.card:hover{box-shadow:var(--shadow-lg)}
.card h3{margin:0 0 9px;font-size:19px;font-weight:900;letter-spacing:-.01em}
.card p{color:var(--muted);margin:0;font-size:15px}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(235px,1fr))}
.svc-ic{width:80px;height:80px;border-radius:26px;display:grid;place-items:center;margin:0 0 18px;box-shadow:0 12px 24px rgba(18,15,12,.1)}
.step-no{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#FFE27A,#D4A537);display:grid;place-items:center;font-weight:900;margin-bottom:14px;box-shadow:0 8px 16px rgba(212,165,55,.35)}
.prose-card{background:linear-gradient(135deg,#fff 0%,#FFF6D8 100%);border-radius:24px;padding:30px;box-shadow:var(--shadow);border-left:6px solid var(--gold)}
.prose-card p{color:var(--muted);max-width:780px;font-size:15.5px}
.gar{display:grid;gap:14px}
.gar div{display:flex;align-items:center;gap:13px;font-weight:700}
.gar .gi{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;flex:none}

/* dikerjakan akun real */
.akun-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
.akun-no{width:50px;height:50px;border-radius:17px;background:linear-gradient(135deg,#1E1913,#120F0C);color:var(--gold2);display:grid;place-items:center;font-weight:900;margin-bottom:14px;box-shadow:0 10px 20px rgba(18,15,12,.22)}
.akun-card h3{margin:0 0 7px;font-size:17px}
.bukti-note{background:linear-gradient(135deg,#fff,#FFF6D8);border-radius:20px;padding:20px 24px;text-align:center;font-weight:800;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.2);margin-top:20px}

/* galeri bukti */
.gal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}
.gal-item{display:block;background:#fff;border-radius:18px;overflow:hidden;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.14);transition:transform .25s,box-shadow .25s}
.gal-item:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg)}
.gal-item img{width:100%;height:auto;display:block}
.gal-empty{grid-column:1/-1;background:#fff;border-radius:20px;padding:34px;text-align:center;color:var(--muted);box-shadow:var(--shadow);border:1px dashed rgba(212,165,55,.45)}

/* upload admin */
.upload{background:linear-gradient(180deg,#fff,#FFFBF0);border-radius:24px;padding:26px;box-shadow:var(--shadow-lg);border:1px solid rgba(212,165,55,.2)}
.up-row{display:flex;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap}
.up-ic{width:58px;height:58px;border-radius:18px;background:linear-gradient(135deg,#E3F8EC,#C6F0D8);display:grid;place-items:center;flex:none}
.up-row b{font-size:16px}
.up-row p{margin:4px 0 0}
.up-actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:6px}
.pick{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;border-radius:999px;background:#fff;border:1.5px solid var(--ink);font-weight:800;cursor:pointer;font-size:15px;box-shadow:0 6px 14px rgba(18,15,12,.08)}
.pick:hover{background:#FFF8E7}
.fname{color:var(--muted);font-size:14px;font-weight:600}
.fileinp{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}
.adm-gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-top:20px}
.adm-item{background:#fff;border-radius:18px;overflow:hidden;box-shadow:var(--shadow);border:1px solid rgba(212,165,55,.14);display:flex;flex-direction:column}
.adm-item img{width:100%;height:130px;object-fit:cover;display:block}
.adm-name{font-size:11.5px;color:var(--muted);padding:8px 10px 0;word-break:break-all}
.adm-item form{margin:0;padding:10px}
.btn-del{width:100%;background:linear-gradient(135deg,#FF6B6F,#E5484D);color:#fff;border:0;border-radius:999px;padding:9px 14px;font-weight:800;cursor:pointer;font-size:13px;font-family:inherit;box-shadow:0 6px 14px rgba(229,72,77,.3)}
.btn-del:hover{filter:brightness(1.05)}

/* form order */
.form .step{margin:0 0 22px}
.form .step>b{display:block;margin-bottom:11px;font-size:15px;font-weight:900;letter-spacing:-.005em}
.opts{display:flex;flex-wrap:wrap;gap:9px}
.opt{position:relative;border:1.5px solid var(--line);border-radius:15px;padding:10px 16px;background:#fff;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:700;box-shadow:0 4px 10px rgba(18,15,12,.04);transition:all .2s}
.opt:hover{border-color:var(--gold)}
.opt input{position:absolute;opacity:0;width:1px;height:1px;margin:0;pointer-events:none}
.opt:has(input:checked){background:linear-gradient(135deg,#1E1913,#120F0C);color:var(--cream);border-color:var(--ink);box-shadow:0 8px 18px rgba(18,15,12,.2)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:13px}
.tile{position:relative;display:flex;align-items:center;gap:13px;background:#fff;border:1.5px solid var(--line);border-radius:20px;padding:13px 15px;cursor:pointer;box-shadow:0 8px 18px rgba(18,15,12,.06);font-weight:800;transition:all .2s}
.tile:hover{border-color:var(--gold)}
.tile input{position:absolute;opacity:0;width:1px;height:1px;margin:0;pointer-events:none}
.tile:has(input:checked){border-color:var(--ink);background:linear-gradient(180deg,#FFFBEA,#FFF5D2);box-shadow:0 12px 24px rgba(18,15,12,.14)}
.tile .ti{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;flex:none}
.chips{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:12px}
.chip{border:1.5px solid var(--line);background:#fff;padding:9px 18px;border-radius:999px;cursor:pointer;font-size:14px;font-weight:800;font-family:inherit;transition:all .2s}
.chip:hover{border-color:var(--gold);background:#FFF6D8}
.info-box{background:linear-gradient(135deg,#FFF6D8,#FFF0BF);border-radius:16px;padding:14px 16px;font-size:14px;font-weight:600;line-height:1.75;border:1px solid rgba(212,165,55,.25)}
input,select,textarea{width:100%;padding:13px 14px;border-radius:14px;border:1.5px solid var(--line);background:#fff;color:var(--ink);margin:6px 0 14px;font-size:15px;font-family:inherit;transition:border-color .2s,box-shadow .2s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--gold);box-shadow:0 0 0 4px rgba(212,165,55,.15)}
input[type=number]{width:190px}
label{display:block}
.total-box{display:flex;justify-content:space-between;align-items:center;background:linear-gradient(120deg,#120F0C,#2A2116);color:var(--cream);border-radius:20px;padding:18px 22px;margin:10px 0 18px;box-shadow:0 16px 30px rgba(18,15,12,.22)}
.total-box strong{font-size:26px;color:var(--gold2);letter-spacing:-.01em}
.total-box span{font-weight:700;letter-spacing:.02em}
.price-table th{background:linear-gradient(90deg,#120F0C,#2A2116);color:var(--cream)}
.price-table th:first-child{border-radius:14px 0 0 0}
.price-table th:last-child{border-radius:0 14px 0 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:#FBF5E6;color:var(--ink);font-size:12.5px;letter-spacing:.06em;text-transform:uppercase;font-weight:800}
td,th{padding:12px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}
.note{color:var(--muted);font-size:14px;margin:14px 0 0}

/* umum */
.box{max-width:450px;margin:28px auto;background:#fff;border-radius:28px;padding:32px;box-shadow:var(--shadow-lg);border:1px solid rgba(212,165,55,.2)}
.alert{padding:13px 17px;border-radius:16px;margin:12px 0;font-weight:600;font-size:14.5px}
.alert.err{background:#FBE1DE;color:#7A1E17}
.alert.ok{background:#DDF3E4;color:#14572F}
.badge{background:linear-gradient(135deg,#FFE27A,#D4A537);color:var(--ink);padding:5px 13px;border-radius:999px;font-size:12px;font-weight:800;white-space:nowrap}
.card[style*="overflow-x"]{padding:8px 10px}
footer{text-align:center;color:var(--muted);padding:34px 20px;font-size:14px;border-top:1px solid rgba(212,165,55,.2);margin-top:20px}
footer::before{content:"TASKBOST";display:block;font-weight:900;letter-spacing:.22em;color:var(--ink);margin-bottom:6px}
"""


def nav(u):
    if not u:
        return ('<a class="btn sm btn-login" href="/login">Login</a>'
                '<a class="btn sm btn-daftar" href="/register">Daftar</a>')
    if u["role"] == "admin":
        link = '<a class="plain" href="/admin">Admin</a>'
    else:
        link = '<a class="plain" href="/toko">Toko</a>'
    return (link + '<form method="post" action="/logout" class="inline">'
            '<button class="link">Logout</button></form>')


def wa_float():
    return (f'<a class="wa-float" href="{h(WA_LINK)}" target="_blank" rel="noopener" '
            f'aria-label="Chat Admin via WhatsApp">{WA_WHITE}<span>Chat Admin</span></a>')


def page(title, body, u=None):
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · TASKBOST</title><style>{CSS}</style></head><body>
{SOS_DEFS}
<header class="top"><a class="logo" href="/"><span class="mark">T</span>TASKBOST</a><nav>{nav(u)}</nav></header>
<main>{body}</main><footer>© TASKBOST</footer>
{wa_float()}
</body></html>"""


def jaminan_section():
    items = []
    for v, l, ic, c, bg in JAMINAN:
        extra = ""
        if ic == "wa":
            extra = (f'<a class="btn btn-wa" href="{h(WA_LINK)}" target="_blank" rel="noopener">'
                     f'{WA_WHITE.replace("26", "16")} Chat Admin</a>')
        items.append(
            f'<div class="jam"><div class="ic" style="background:{bg}">{jam_ic(ic, c)}</div>'
            f'<b>{h(v)}</b><span>{h(l)}</span>{extra}</div>')
    return (f'<section><h2 class="center">Jaminan TASKBOST</h2>'
            f'<div class="jamin">{"".join(items)}</div></section>')


def freelance_section():
    return f"""<section>
<h2 class="center">Bekerja Sama Dengan Freelance Asli</h2>
<p class="lead">Puluhan ribu tugas sudah selesai dikerjakan freelance kami. Bukan bot, bukan akun palsu.
Setiap tugas dicek manual sebelum dinyatakan valid.</p>
<div class="fl-box">
  <div class="fl-ic">{ico("users", 40, "#1FA463", 2.2)}</div>
  <div class="fl-num">200.000+</div>
  <div class="fl-label">Tugas Selesai</div>
  <div class="fl-note">Dikerjakan freelance asli, dicek manual.</div>
</div>
</section>"""


def tentang_section():
    return """<section>
<h2>Tentang TASKBOST</h2>
<div class="prose-card">
  <p>Banyak orang ingin akunnya terlihat lebih hidup, tetapi tidak mau mengambil risiko dengan layanan
  yang dikerjakan bot. TASKBOST hadir untuk itu. Setiap interaksi dikerjakan oleh akun asli,
  sehingga hasilnya terlihat wajar di mata pengunjung dan platform.</p>
  <p>Harga dan detail layanan ada di dalam setelah kamu login, supaya kamu bisa melihat seluruh
  opsi dengan jelas sebelum memesan.</p>
</div>
</section>"""


def cara_pesan_section():
    langkah = [
        ("Daftar", "Buat akun TASKBOST dengan username, email, dan password."),
        ("Pilih sosmed", "Pilih Instagram, TikTok, Threads, Facebook, atau WhatsApp."),
        ("Atur pesanan", "Pilih layanan, jumlah, mode, gender, dan durasi. Total langsung terlihat."),
        ("Pantau status", "Pesanan diproses manual oleh admin. Status jadi selesai otomatis sesuai durasi."),
    ]
    items = "".join(
        f'<div class="card"><div class="step-no">{i}</div><h3>{h(t)}</h3><p>{h(d)}</p></div>'
        for i, (t, d) in enumerate(langkah, 1))
    return f'<section><h2 class="center">Cara Pesan</h2><div class="grid">{items}</div></section>'


def cara_bayar_section():
    steps = [
        "Buat pesanan, total langsung terlihat.",
        "Bayar via WA Admin, klik tombol Chat Admin / Konfirmasi Bayar.",
        "Kirim bukti bayar + ID order ke WA, admin cek, lalu pesanan diproses.",
    ]
    items = "".join(
        f'<div class="bayar-step"><div class="bayar-no">{i}</div><p>{h(t)}</p></div>'
        for i, t in enumerate(steps, 1))
    return f"""<section>
<h2 class="center">Cara Bayar</h2>
<div class="bayar">
  <div class="bayar-grid">{items}</div>
  <p class="bayar-note">Pembayaran manual via WA Admin saja. Belum ada gateway otomatis.</p>
</div>
</section>"""


def akun_real_section():
    steps = "".join(
        f'<div class="card akun-card"><div class="akun-no">{n}</div><h3>{h(t)}</h3><p>{h(d)}</p></div>'
        for n, t, d in AKUN_STEPS)
    return f"""<section>
<h2 class="center">Dikerjakan Akun Real, Bukan Bot</h2>
<p class="lead">Yang Follow, Like, Comment itu akun real, bukan bot. Dikerjakan member freelance asli, dicek manual, tanpa kecurangan.</p>
<div class="akun-grid">{steps}</div>
<div class="bukti-note">Bukti pengerjaan asli ada di Galeri Bukti di bawah. Bukan angka karangan, bukan testimoni karangan.</div>
</section>"""


def galeri_section():
    files = list_bukti()
    if files:
        items = "".join(
            f'<a class="gal-item" href="/bukti/{quote(f)}" target="_blank" rel="noopener">'
            f'<img src="/bukti/{quote(f)}" alt="Bukti pengerjaan" loading="lazy"></a>'
            for f in files)
    else:
        items = ('<div class="gal-empty">Foto bukti pengerjaan akan tampil di sini '
                 'setelah admin upload foto asli.</div>')
    return f"""<section>
<h2 class="center">Galeri Bukti</h2>
<div class="gal-grid">{items}</div>
</section>"""


def garansi_section():
    return """<section><h2>Garansi</h2><div class="card gar">
<div><span class="gi" style="background:#FFF1CC">""" + ico("wallet", 22, "#B7892E", 2.2) + """</span>
Jika order tidak valid, uang kembali 100%.</div>
<div><span class="gi" style="background:#FDE7E8">""" + ico("gift", 22, "#E5484D", 2.2) + """</span>
Jika drop, diganti gratis.</div>
</div></section>"""


def svc_card(key, title, desc):
    bg = LAY_ICON[key][2]
    return (f'<div class="card"><div class="svc-ic" style="background:{bg}">{lay_ic(key, 40)}</div>'
            f'<h3>{h(title)}</h3><p>{h(desc)}</p></div>')


# ---------------------------------------------------------------- landing
HERO_ART = """<svg viewBox="0 0 300 320" aria-hidden="true">
<circle cx="150" cy="160" r="140" fill="#FFFFFF" opacity=".35"/>
<ellipse cx="140" cy="290" rx="92" ry="10" fill="#120F0C" opacity=".25"/>
<rect x="112" y="226" width="22" height="58" rx="9" fill="#120F0C"/>
<rect x="142" y="226" width="22" height="58" rx="9" fill="#120F0C"/>
<ellipse cx="118" cy="286" rx="16" ry="7" fill="#120F0C"/>
<ellipse cx="156" cy="286" rx="16" ry="7" fill="#120F0C"/>
<path d="M100 138 Q140 122 180 138 L186 232 Q140 244 94 232 Z" fill="#FFFFFF" stroke="#120F0C" stroke-width="3" stroke-linejoin="round"/>
<path d="M124 130 L140 150 L156 130" fill="none" stroke="#120F0C" stroke-width="3" stroke-linejoin="round"/>
<path d="M102 146 Q84 176 88 214" fill="none" stroke="#FFFFFF" stroke-width="20" stroke-linecap="round"/>
<circle cx="88" cy="218" r="11" fill="#F2C9A0" stroke="#120F0C" stroke-width="2.5"/>
<path d="M178 146 Q196 168 180 186" fill="none" stroke="#FFFFFF" stroke-width="20" stroke-linecap="round"/>
<rect x="168" y="112" width="58" height="96" rx="11" fill="#120F0C"/>
<rect x="175" y="122" width="44" height="74" rx="5" fill="#FFF8E7"/>
<rect x="181" y="130" width="32" height="8" rx="4" fill="#FFC93C"/>
<path d="M197 158 c-2-3-7-3-7 1 0 3 7 7 7 7s7-4 7-7c0-4-5-4-7-1z" fill="#E5484D"/>
<circle cx="197" cy="180" r="8" fill="#1FA463"/>
<path d="M193 180l3 3 5-6" fill="none" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
<ellipse cx="176" cy="190" rx="10" ry="9" fill="#F2C9A0" stroke="#120F0C" stroke-width="2.5"/>
<ellipse cx="212" cy="196" rx="8" ry="7" fill="#F2C9A0" stroke="#120F0C" stroke-width="2"/>
<circle cx="140" cy="92" r="30" fill="#F2C9A0" stroke="#120F0C" stroke-width="2.5"/>
<path d="M108 88 Q108 56 140 56 Q172 56 172 88 Q162 72 140 72 Q118 72 108 88 Z" fill="#120F0C"/>
<circle cx="130" cy="96" r="2.8" fill="#120F0C"/>
<circle cx="150" cy="96" r="2.8" fill="#120F0C"/>
<path d="M130 108 Q140 116 150 108" fill="none" stroke="#120F0C" stroke-width="2.5" stroke-linecap="round"/>
<circle cx="48" cy="86" r="22" fill="#1FA463" stroke="#FFFFFF" stroke-width="3"/>
<path d="M40 86l6 6 11-12" stroke="#FFFFFF" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="256" cy="66" r="20" fill="#E5484D" stroke="#FFFFFF" stroke-width="3"/>
<path d="M256 75c-7-4.5-10-7.5-10-11a4.6 4.6 0 0 1 10-1.8 4.6 4.6 0 0 1 10 1.8c0 3.5-3 6.5-10 11z" fill="#FFFFFF"/>
<rect x="222" y="212" width="66" height="38" rx="14" fill="#FFFFFF" stroke="#120F0C" stroke-width="3"/>
<circle cx="238" cy="231" r="4" fill="#2F6FEB"/><circle cx="255" cy="231" r="4" fill="#2F6FEB"/><circle cx="272" cy="231" r="4" fill="#2F6FEB"/>
</svg>"""


def landing():
    sos_html = "".join(
        f'<span class="sos {k}">{sos_svg(k, 22)} {h(v[0])}</span>' for k, v in SOSMED.items())
    layanan = (svc_card("follow", "Follow", "Tambah pengikut dari akun asli untuk membangun kesan akun yang lebih kuat dan dipercaya.")
               + svc_card("like", "Like", "Tambah like pada postingan supaya konten terlihat lebih ramai dan menarik perhatian.")
               + svc_card("comment", "Comment", "Komentar yang relevan dengan postingan, bisa random atau ditulis sendiri."))
    return f"""
<section class="hero" style="margin-top:26px">
  <div>
    <span class="kicker">TASKBOST · TOKO SOSMED</span>
    <h1>Follow / Like / Comment <em>Asli, Orang Beneran, Bukan Bot</em></h1>
    <p>Kami fokus pada interaksi dari akun asli, pengerjaan yang rapi, dan layanan yang jelas
    dari awal sampai akhir. Daftar sekarang untuk mulai memesan.</p>
    <ul class="hero-points">
      <li>{ico("check", 20, "#FFD65C", 2.4)} Interaksi dari akun asli</li>
      <li>{ico("check", 20, "#FFD65C", 2.4)} Diproses manual oleh admin</li>
      <li>{ico("check", 20, "#FFD65C", 2.4)} Garansi jelas dan tertulis</li>
    </ul>
    <div class="hero-btns">
      <a class="btn btn-login" href="/login">Login</a>
      <a class="btn btn-daftar" href="/register">Daftar Sekarang</a>
    </div>
  </div>
  <div class="hero-art">{HERO_ART}</div>
</section>
{jaminan_section()}
{freelance_section()}
<section>
  <h2 class="center">Tersedia di Banyak Sosmed</h2>
  <p class="lead">Layanan kami bisa dipakai untuk akun dan postingan di beberapa platform populer.</p>
  <div class="sos-row">{sos_html}</div>
</section>
<section>
  <h2 class="center">Layanan TASKBOST</h2>
  <p class="lead">Pilih layanan sesuai kebutuhan akunmu. Jumlah dan mode diatur sendiri setelah login.</p>
  <div class="grid">{layanan}</div>
</section>
{tentang_section()}
{cara_pesan_section()}
{cara_bayar_section()}
{akun_real_section()}
{galeri_section()}
{garansi_section()}"""


# ---------------------------------------------------------------- auth pages
def auth_box(title, inner, qs):
    return (f'<section class="box" style="margin-top:34px"><h2>{title}</h2>'
            f'{alert(qs.get("err"), "err")}{alert(qs.get("ok"), "ok")}{inner}</section>')


def register_form(qs):
    return auth_box("Daftar", """
<form method="post" action="/register">
<label>Username<input name="username" required maxlength="20" placeholder="huruf, angka, _ (3-20)"></label>
<label>Email<input type="email" name="email" required></label>
<label>Password<input type="password" name="password" required minlength="6"></label>
<button class="btn btn-daftar" style="width:100%">Daftar</button></form>
<p>Sudah punya akun? <a href="/login"><b>Login</b></a></p>""", qs)


def login_form(qs):
    return auth_box("Login", """
<form method="post" action="/login">
<label>Email atau Username<input name="ident" required></label>
<label>Password<input type="password" name="password" required></label>
<button class="btn btn-login" style="width:100%">Login</button></form>
<p><a href="/lupa">Lupa password?</a> · <a href="/register"><b>Daftar</b></a></p>""", qs)


def forgot_form(qs):
    return auth_box("Lupa Password", """
<form method="post" action="/lupa">
<label>Email<input type="email" name="email" required></label>
<button class="btn btn-login" style="width:100%">Kirim Link</button></form>""", qs)


def reset_form(qs):
    tok = qs.get("token", "")
    if not tok:
        return auth_box("Password Baru", "<p>Link tidak valid.</p>", qs)
    return auth_box("Password Baru", f"""
<form method="post" action="/reset">
<input type="hidden" name="token" value="{h(tok)}">
<label>Password Baru<input type="password" name="password" required minlength="6"></label>
<button class="btn btn-login" style="width:100%">Simpan Password</button></form>""", qs)


# ---------------------------------------------------------------- toko (member, setelah login)
ORDER_JS = """
<script>
const PRICES = __PRICES__;
const PG = __PG__;
const HINT = __HINT__;
const NEED = __NEED__;
const NOGENDER = __NOGENDER__;
function val(n){const e=document.querySelector('input[name="'+n+'"]:checked');return e?e.value:""}
function rupiah(n){return "Rp "+String(n).replace(/\\B(?=(\\d{3})+(?!\\d))/g,".")}
function setQty(n){document.getElementById("qty").value=n;update()}
function update(){
  const s=val("sosmed");
  let firstVisible=null;
  document.querySelectorAll("[data-sosmed]").forEach(el=>{
    const show=el.dataset.sosmed===s;
    el.style.display=show?"":"none";
    if(show&&!firstVisible) firstVisible=el;
  });
  const cur=document.querySelector('input[name="layanan"]:checked');
  if(!cur||cur.closest("[data-sosmed]").style.display==="none"){
    const pick=firstVisible&&firstVisible.querySelector("input");
    if(pick) pick.checked=true;
  }
  const l=val("layanan");
  const needT=NEED.includes(l);
  document.getElementById("tipe-box").style.display=needT?"":"none";
  const t=val("tipe");
  document.getElementById("isi-box").style.display=(needT&&t==="CUSTOM")?"":"none";
  document.getElementById("gender-box").style.display=NOGENDER.includes(l)?"none":"";
  const hint=HINT[l]||HINT["default"];
  document.getElementById("tujuan-label").textContent=hint[0];
  document.getElementById("tujuan").placeholder=hint[1];
  const qty=parseInt(document.getElementById("qty").value,10)||0;
  const grp=PG[l];
  const per=grp?PRICES[grp][val("mode")]:0;
  document.getElementById("total").textContent=rupiah(qty*per);
}
document.addEventListener("change",update);
document.addEventListener("input",update);
update();
</script>
"""


def price_table():
    rows = "".join(
        f"<tr><td><b>{h(n)}</b></td><td>{rp(PRICES[k]['standard'])} / satuan</td>"
        f"<td>{rp(PRICES[k]['premium'])} / satuan</td></tr>"
        for k, n in [("follow", "Follow"), ("like", "Like"), ("comment", "Comment")])
    return f"""<section><h2>Daftar Harga</h2><div class="card" style="overflow-x:auto">
<table class="price-table"><tr><th>Layanan</th><th>Standard</th><th>Premium / FAST</th></tr>{rows}</table>
<p class="note">Total = jumlah × harga per satuan. Contoh: 150 Follow Premium = 150 × {rp(250)} = {rp(37500)}.</p>
</div></section>"""


def order_form():
    sos_html = "".join(
        f'<label class="sos {k}"><input type="radio" name="sosmed" value="{k}"'
        f'{" checked" if k == "ig" else ""}>{sos_svg(k, 22)}<span>{h(v[0])}</span></label>'
        for k, v in SOSMED.items())
    lay_html = "".join(
        f'<label class="tile" data-sosmed="{s}"><input type="radio" name="layanan" value="{k}"'
        f'{" checked" if s == "ig" and i == 0 else ""}>'
        f'<span class="ti" style="background:{LAY_ICON[k][2]}">{lay_ic(k, 24)}</span>'
        f'<span>{h(lbl)}</span></label>'
        for s, (_, items) in SOSMED.items() for i, (k, lbl) in enumerate(items))
    tipe_html = "".join(
        f'<label class="opt"><input type="radio" name="tipe" value="{k}"'
        f'{" checked" if k == "RANDOM" else ""}><span>{h(v)}</span></label>'
        for k, v in TIPES.items())
    mode_html = "".join(
        f'<label class="opt"><input type="radio" name="mode" value="{k}"'
        f'{" checked" if k == "standard" else ""}><span>{h(v)}</span></label>'
        for k, v in MODES.items())
    gender_html = "".join(
        f'<label class="opt"><input type="radio" name="gender" value="{k}"'
        f'{" checked" if k == "bebas" else ""}><span>{h(v)}</span></label>'
        for k, v in GENDERS.items())
    durasi_html = "".join(
        f'<label class="opt"><input type="radio" name="durasi" value="{d}"'
        f'{" checked" if d == 6 else ""}><span>{d} hari</span></label>'
        for d in DURASI)
    chips = "".join(f'<button type="button" class="chip" onclick="setQty({n})">{n}</button>'
                    for n in QUICK_QTY)
    form = f"""
<section><h2>Buat Pesanan</h2>
<form method="post" action="/order" class="card form">
<div class="step"><b>1. Pilih sosmed</b><div class="sos-row" style="justify-content:flex-start">{sos_html}</div></div>
<div class="step"><b>2. Pilih layanan</b><div class="tiles">{lay_html}</div></div>
<div class="step" id="tipe-box"><b>3. Tipe komentar</b><div class="opts">{tipe_html}</div></div>
<div class="step"><b>4. Jumlah (100 - 1000)</b>
  <div class="chips">{chips}</div>
  <input type="number" id="qty" name="qty" min="100" max="1000" value="100" required></div>
<div class="step"><b>5. Mode</b><div class="opts">{mode_html}</div></div>
<div class="step" id="gender-box"><b>6. Gender</b><div class="opts">{gender_html}</div></div>
<div class="step"><b>7. Durasi Order</b>
  <div class="opts">{durasi_html}</div>
  <div class="info-box" style="margin-top:12px">
    Garansi: 6 hari / 14 hari / 30 hari.<br>
    Pengerjaan: 3-12 jam (Premium / FAST) / 1-2 hari (Standard).
  </div></div>
<div class="step"><b>8. Tujuan: <span id="tujuan-label">Username atau link</span></b>
  <input id="tujuan" name="tujuan" maxlength="300" required></div>
<div class="step" id="isi-box"><b>Isi komentar / chat</b>
  <textarea id="isi" name="isi" rows="3" maxlength="500"
  placeholder="Tulis isi komentar atau chat"></textarea></div>
<div class="total-box"><span>Total</span><strong id="total">Rp 0</strong></div>
<button class="btn btn-order">Order Sekarang</button>
</form></section>"""
    script = (ORDER_JS
              .replace("__PRICES__", json.dumps(PRICES))
              .replace("__PG__", json.dumps(PRICE_GROUP))
              .replace("__HINT__", json.dumps(HINT))
              .replace("__NEED__", json.dumps(NEED_TIPE))
              .replace("__NOGENDER__", json.dumps(NOGENDER)))
    return form + script


def toko_page(u, qs):
    orders = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (u["id"],))
    rows = ""
    for o in orders:
        aksi = ""
        if o["status"] == "menunggu":
            aksi = (f'<a class="btn btn-konf" href="{h(konfirmasi_link(o))}" '
                    f'target="_blank" rel="noopener">Konfirmasi Bayar via WA</a>')
        rows += (f"<tr><td>#{o['id']}</td><td>{h(o['paket'])}</td><td>{rp(o['harga'])}</td>"
                 f"<td><span class='badge'>{h(o['status'])}</span></td>"
                 f"<td>{h(o['created_at'])}</td><td>{h(o['catatan'])}</td>"
                 f"<td>{aksi}</td></tr>")
    if not rows:
        rows = '<tr><td colspan="7">Belum ada pesanan.</td></tr>'
    return f"""
<section><h2>Halo, {h(u['username'])} 👋</h2>
{alert(qs.get('err'), 'err')}{alert(qs.get('ok'), 'ok')}</section>
{price_table()}
{cara_bayar_section()}
{order_form()}
<section><h2>Pesanan Saya</h2>
<div class="card" style="overflow-x:auto"><table>
<tr><th>ID</th><th>Paket</th><th>Total</th><th>Status</th><th>Tanggal</th><th>Detail</th><th>Aksi</th></tr>
{rows}</table></div>
<p class="note">Status jadi selesai otomatis setelah durasi order habis.</p>
</section>
{jaminan_section()}
{garansi_section()}"""


# ---------------------------------------------------------------- admin (hanya /admin)
def upload_box():
    return """<section>
<h2>Upload Foto Bukti</h2>
<form method="post" action="/admin/upload" enctype="multipart/form-data" class="upload">
  <div class="up-row">
    <div class="up-ic">""" + ico("users", 28, "#1FA463", 2.2) + """</div>
    <div>
      <b>Foto bukti pengerjaan asli</b>
      <p class="note">JPG, PNG, atau WEBP. Maks 8 MB per foto. Foto langsung tampil di Galeri Bukti halaman utama.</p>
    </div>
  </div>
  <div class="up-actions">
    <label class="pick" for="foto-input">Pilih Foto</label>
    <input class="fileinp" id="foto-input" type="file" name="foto"
           accept="image/jpeg,image/png,image/webp" required>
    <span class="fname" id="fname">Belum ada foto dipilih</span>
    <button class="btn btn-login sm">Upload Foto</button>
  </div>
</form>
<script>
document.getElementById("foto-input").addEventListener("change",function(){
  const f=this.files&&this.files[0];
  document.getElementById("fname").textContent=f?f.name:"Belum ada foto dipilih";
});
</script>
""" + gallery_admin() + "</section>"


def gallery_admin():
    files = list_bukti()
    if not files:
        return '<p class="note">Belum ada foto di folder bukti/.</p>'
    items = "".join(
        f"""<div class="adm-item"><img src="/bukti/{quote(f)}" alt="Bukti">
<div class="adm-name">{h(f)}</div>
<form method="post" action="/admin/bukti/hapus" onsubmit="return confirm('Hapus foto ini dari Galeri?')">
<input type="hidden" name="name" value="{h(f)}">
<button class="btn-del">Hapus</button></form></div>"""
        for f in files)
    return f'<div class="adm-gal">{items}</div>'


def admin_page(qs):
    orders = q("SELECT o.*, u.username FROM orders o JOIN users u ON u.id=o.user_id "
               "ORDER BY o.id DESC LIMIT 300")
    rows = ""
    for o in orders:
        if o["status"] in ADMIN_STATUSES:
            opts = "".join(f'<option value="{s}" {"selected" if s == o["status"] else ""}>{s}</option>'
                           for s in ADMIN_STATUSES)
            aksi = (f'<form method="post" action="/admin/status">'
                    f'<input type="hidden" name="id" value="{o["id"]}">'
                    f'<select name="status">{opts}</select>'
                    f'<button class="btn btn-login sm">Simpan</button></form>')
        else:
            aksi = f"<span class='badge'>{h(o['status'])}</span>"
        rows += (f"<tr><td>#{o['id']}</td><td>{h(o['created_at'])}</td>"
                 f"<td>{h(o['username'])}</td><td>{h(o['paket'])}</td>"
                 f"<td>{rp(o['harga'])}</td><td>{h(o['catatan'])}</td>"
                 f"<td>{aksi}</td></tr>")
    return f"""
<section><h2>Admin · Semua Order</h2>
{alert(qs.get('err'), 'err')}{alert(qs.get('ok'), 'ok')}</section>
{upload_box()}
<section><h2>Semua Order</h2>
<div class="card" style="overflow-x:auto"><table>
<tr><th>ID</th><th>Waktu</th><th>User</th><th>Paket</th><th>Total</th><th>Detail</th><th>Status</th></tr>
{rows or '<tr><td colspan="7">Belum ada order.</td></tr>'}</table></div></section>"""


# ---------------------------------------------------------------- actions
def do_register(r, f):
    user = f.get("username", "")
    email = f.get("email", "").lower()
    pw = f.get("password", "")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", user):
        return go(r, "/register", err="Username 3-20 karakter: huruf, angka, atau _")
    if user.lower() == ADMIN_USER:
        return go(r, "/register", err="Username itu tidak tersedia")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return go(r, "/register", err="Email tidak valid")
    if len(pw) < 6:
        return go(r, "/register", err="Password minimal 6 karakter")
    try:
        q("INSERT INTO users (username,email,password_hash,role) VALUES (?,?,?,?)",
          (user, email, hash_pw(pw), "customer"))
    except sqlite3.IntegrityError:
        return go(r, "/register", err="Username atau email sudah dipakai")
    uid = q("SELECT id FROM users WHERE username=?", (user,), one=True)["id"]
    r.redirect("/toko", login_cookie(uid))


def do_login(r, f):
    ident = f.get("ident", "")
    u = q("SELECT * FROM users WHERE email=? OR username=?", (ident.lower(), ident), one=True)
    if not u or not check_pw(f.get("password", ""), u["password_hash"]):
        return go(r, "/login", err="Email/username atau password salah")
    r.redirect("/admin" if u["role"] == "admin" else "/toko", login_cookie(u["id"]))


def do_logout(r, cookie_header):
    ck = SimpleCookie(cookie_header or "")
    if "sid" in ck:
        SESSIONS.pop(ck["sid"].value, None)
    r.redirect("/", "sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")


def do_forgot(r, f):
    email = f.get("email", "").lower()
    u = q("SELECT id,email FROM users WHERE email=?", (email,), one=True)
    if u:
        tok = secrets.token_urlsafe(32)
        RESET_TOKENS[tok] = (u["id"], time.time() + 1800)
        link = f"{BASE_URL}/reset?token={tok}"
        send_mail(email, "Reset password TASKBOST",
                  f"Klik link ini untuk membuat password baru (berlaku 30 menit):\n{link}")
    go(r, "/lupa", ok="Jika email terdaftar, link reset sudah dikirim.")


def do_reset(r, f):
    tok = f.get("token", "")
    pw = f.get("password", "")
    entry = RESET_TOKENS.get(tok)
    if not entry or entry[1] < time.time():
        RESET_TOKENS.pop(tok, None)
        return go(r, "/lupa", err="Link reset tidak valid atau sudah kedaluwarsa")
    if len(pw) < 6:
        return go(r, "/reset", token=tok, err="Password minimal 6 karakter")
    q("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(pw), entry[0]))
    RESET_TOKENS.pop(tok, None)
    go(r, "/login", ok="Password berhasil diubah, silakan login.")


def do_order(r, u, f):
    sos = f.get("sosmed", "")
    lay = f.get("layanan", "")
    mode = f.get("mode", "")
    tipe = f.get("tipe", "").upper()
    isi = f.get("isi", "").strip()
    gender_key = f.get("gender", "").lower()
    durasi_raw = f.get("durasi", "")

    if sos not in SOSMED:
        return go(r, "/toko", err="Sosmed tidak valid")
    layanan_keys = [k for k, _ in SOSMED[sos][1]]
    if lay not in layanan_keys:
        return go(r, "/toko", err="Layanan tidak valid untuk sosmed ini")
    if mode not in MODES:
        return go(r, "/toko", err="Mode tidak valid")
    try:
        qty = int(f.get("qty", ""))
    except ValueError:
        return go(r, "/toko", err="Jumlah harus angka")
    if not QTY_MIN <= qty <= QTY_MAX:
        return go(r, "/toko", err=f"Jumlah harus {QTY_MIN}-{QTY_MAX}")

    gender = ""
    if lay not in NOGENDER:
        if gender_key not in GENDERS:
            return go(r, "/toko", err="Pilih gender")
        gender = GENDERS[gender_key]

    try:
        durasi = int(durasi_raw)
    except ValueError:
        return go(r, "/toko", err="Pilih durasi order")
    if durasi not in DURASI:
        return go(r, "/toko", err="Durasi order tidak valid")

    if lay in NEED_TIPE:
        if tipe not in TIPES:
            return go(r, "/toko", err="Pilih tipe komentar")
        if tipe == "CUSTOM" and (not isi or len(isi) > 500):
            return go(r, "/toko", err="Isi komentar/chat wajib diisi (maks 500 karakter)")
        if tipe == "RANDOM":
            isi = ""
    else:
        tipe = ""
        isi = ""

    tujuan = clean_target(lay, f.get("tujuan", ""))
    if not tujuan:
        return go(r, "/toko", err="Tujuan tidak valid. Cek lagi username, link, atau nomor WA.")

    harga = hitung_harga(lay, mode, qty)
    nama_sos = SOSMED[sos][0]
    nama_lay = dict(SOSMED[sos][1])[lay]
    paket = f"{nama_sos} · {nama_lay} · {MODES[mode]}"
    catatan = (f"Qty: {qty} | Tipe: {tipe or '-'} | Gender: {gender or '-'} | "
               f"Durasi: {durasi} hari | Tujuan: {tujuan} | Isi: {isi or '-'}")

    c = db()
    try:
        cur = c.execute(
            "INSERT INTO orders (user_id,paket,harga,status,catatan) VALUES (?,?,?,?,?)",
            (u["id"], paket, harga, "menunggu", catatan))
        c.commit()
        oid = cur.lastrowid
    finally:
        c.close()

    go(r, "/toko", ok=(f"Order #{oid} masuk. Total {rp(harga)}. Status: menunggu. "
                       f"Silakan bayar via WA Admin, kirim bukti + ID order."))


def do_admin_status(r, f):
    try:
        oid = int(f.get("id", ""))
    except ValueError:
        return go(r, "/admin", err="ID tidak valid")
    if f.get("status") not in ADMIN_STATUSES:
        return go(r, "/admin", err="Status tidak valid")
    q("UPDATE orders SET status=? WHERE id=? AND status IN ('menunggu','diproses')",
      (f["status"], oid))
    go(r, "/admin", ok=f"Order #{oid} diperbarui.")


# ---------------------------------------------------------------- routing
def serve_bukti(r, path):
    name = os.path.basename(unquote(path[len("/bukti/"):]))
    ext = os.path.splitext(name)[1].lower()
    fp = os.path.join(BUKTI_DIR, name)
    if ext in BUKTI_EXT and os.path.isfile(fp):
        return r.file(fp, BUKTI_EXT[ext])
    r.html(page("Tidak ditemukan", "<p>Berkas tidak ditemukan.</p>"), 404)


def route(r, m, path, qs, f):
    if m == "GET" and path.startswith("/bukti/"):
        return serve_bukti(r, path)

    u = r.current_user()
    is_admin = bool(u and u["role"] == "admin")

    if m == "GET" and path == "/":
        return r.html(page("Beranda", landing(), u))
    if m == "GET" and path == "/register":
        return r.html(page("Daftar", register_form(qs), u))
    if m == "POST" and path == "/register":
        return do_register(r, f)
    if m == "GET" and path == "/login":
        return r.html(page("Login", login_form(qs), u))
    if m == "POST" and path == "/login":
        return do_login(r, f)
    if m == "POST" and path == "/logout":
        return do_logout(r, r.headers.get("Cookie"))
    if path == "/lupa":
        if m == "POST":
            return do_forgot(r, f)
        return r.html(page("Lupa Password", forgot_form(qs), u))
    if path == "/reset":
        if m == "POST":
            return do_reset(r, f)
        return r.html(page("Password Baru", reset_form(qs), u))

    # Member: /toko
    if path == "/toko":
        if not u:
            return r.redirect("/login")
        if is_admin:
            return r.redirect("/admin")
        auto_selesai()
        return r.html(page("Toko", toko_page(u, qs), u))
    if m == "POST" and path == "/order":
        if not u:
            return r.redirect("/login")
        if is_admin:
            return r.redirect("/admin")
        return do_order(r, u, f)

    # Admin only: /admin
    if path == "/admin":
        if not u:
            return r.redirect("/login")
        if not is_admin:
            return r.redirect("/toko")
        auto_selesai()
        return r.html(page("Admin", admin_page(qs), u))
    if m == "POST" and path == "/admin/status":
        if not is_admin:
            return r.redirect("/login")
        return do_admin_status(r, f)
    if m == "POST" and path == "/admin/upload":
        if not is_admin:
            return r.redirect("/login")
        return do_upload(r)
    if m == "POST" and path == "/admin/bukti/hapus":
        if not is_admin:
            return r.redirect("/login")
        return do_bukti_hapus(r, f)

    r.html(page("Tidak ditemukan", "<p>Halaman tidak ditemukan.</p>", u), 404)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def current_user(self):
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        sid = ck["sid"].value if "sid" in ck else ""
        uid = SESSIONS.get(sid)
        return q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None

    def html(self, body, status=200, cookie=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def file(self, fp, ctype):
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc, cookie=None):
        self.send_response(303)
        self.send_header("Location", loc)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def dispatch(self, method):
        p = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(p.query).items()}
        f, self.files = {}, {}
        try:
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_UPLOAD + 65536:
                    return self.html(page("Terlalu besar",
                                          '<p class="alert err">Ukuran data terlalu besar. Maks 8 MB per foto.</p>'), 413)
                body = self.rfile.read(n)
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/form-data"):
                    f, self.files = parse_multipart(body, ctype)
                else:
                    f = {k: v[0].strip() for k, v in
                         parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            route(self, method, p.path, qs, f)
        except Exception:
            traceback.print_exc()
            self.html(page("Error", '<p class="alert err">Terjadi kesalahan. Coba lagi.</p>'), 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


if __name__ == "__main__":
    init_db()
    seed_admin()
    os.makedirs(BUKTI_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"TASKBOST berjalan di http://localhost:{PORT}  (Ctrl+C untuk berhenti)")
    print(f"Foto galeri bukti diambil dari folder: {BUKTI_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
