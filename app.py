"""Food Web v0.4 - prototipe akademik website pemesanan catering.

Stack: Flask + SQLite + Jinja2 (tanpa JavaScript framework).
Login WAJIB untuk semua halaman kecuali Home, About, More, Login, Register.
"""
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
DB_PATH = os.environ.get("FOODWEB_DB") or os.path.join(INSTANCE_DIR, "foodweb.db")

MIN_QTY, MAX_QTY = 5, 500          # batas porsi per menu (D-06: satu pesanan boleh banyak menu)
MAX_DAYS_AHEAD = 90

TASTES = {"asin": "Asin", "manis": "Manis", "pedas": "Pedas"}
DIETS = {
    "rendah_kalori": "Rendah kalori",
    "rendah_gula": "Rendah gula",
    "tinggi_protein": "Tinggi protein",
    "rendah_lemak": "Rendah lemak",
}
PAY_STATUS = {
    "awaiting_payment": "Menunggu pembayaran",
    "awaiting_verification": "Menunggu verifikasi",
    "paid": "Pembayaran berhasil",
    "rejected": "Pembayaran ditolak",
}


# --------------------------------------------------------------------------- app
def _load_secret_key():
    env = os.environ.get("FOODWEB_SECRET")
    if env:
        return env
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FOODWEB_HTTPS") == "1",  # set 1 saat memakai HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    MAX_CONTENT_LENGTH=64 * 1024,                                   # tolak body besar
)

# --------------------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  full_name TEXT NOT NULL,
  phone TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'customer' CHECK(role IN ('customer','admin')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS preferences(
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  has_allergy INTEGER NOT NULL DEFAULT 0,
  allergy_text TEXT NOT NULL DEFAULT '',
  tastes TEXT NOT NULL DEFAULT '',
  diets TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS menus(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  price INTEGER NOT NULL CHECK(price > 0),
  taste TEXT NOT NULL,
  diets TEXT NOT NULL DEFAULT '',
  allergens TEXT NOT NULL DEFAULT '',
  allergen_known INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  contact_name TEXT NOT NULL,
  contact_phone TEXT NOT NULL,
  event_date TEXT NOT NULL,
  special_request TEXT NOT NULL DEFAULT '',
  allergy_text TEXT NOT NULL DEFAULT '',
  total INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  menu_id INTEGER NOT NULL REFERENCES menus(id),
  menu_name TEXT NOT NULL,
  unit_price INTEGER NOT NULL,
  qty INTEGER NOT NULL CHECK(qty > 0)
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  method TEXT NOT NULL DEFAULT 'QRIS_STATIS',
  status TEXT NOT NULL DEFAULT 'awaiting_payment'
    CHECK(status IN ('awaiting_payment','awaiting_verification','paid','rejected')),
  payer_note TEXT NOT NULL DEFAULT '',
  confirmed_at TEXT,
  verified_by INTEGER REFERENCES users(id),
  verified_at TEXT,
  admin_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# (name, description, price, taste, diets, allergens, allergen_known)
SEED_MENUS = [
    ("Nasi Box Ayam Bakar Madu", "Ayam bakar bumbu madu, nasi putih, lalapan, sambal terpisah.", 35000,
     "manis", "tinggi_protein", "kedelai (kecap)", 1),
    ("Nasi Box Rendang Sapi", "Rendang sapi empuk, nasi, daun singkong, telur balado.", 42000,
     "pedas", "tinggi_protein", "santan, kelapa, telur", 1),
    ("Paket Sehat Dada Ayam Panggang", "Dada ayam panggang, brokoli kukus, nasi merah.", 38000,
     "asin", "rendah_kalori,rendah_gula,tinggi_protein,rendah_lemak", "", 1),
    ("Nasi Kuning Komplit", "Nasi kuning, ayam suwir, telur dadar, kering tempe.", 32000,
     "asin", "", "telur, santan, kedelai (tempe)", 1),
    ("Pecel Sayur Lontong", "Aneka sayur rebus, lontong, bumbu pecel.", 28000,
     "pedas", "rendah_kalori,rendah_lemak,rendah_gula", "kacang", 1),
    ("Sate Ayam Bumbu Kacang", "Sate ayam 10 tusuk, lontong, bumbu kacang.", 36000,
     "manis", "tinggi_protein", "kacang, kedelai (kecap)", 1),
    ("Salad Buah Yoghurt", "Buah segar potong dengan dressing yoghurt.", 25000,
     "manis", "rendah_lemak", "susu", 1),
    ("Pepes Ikan Kembung", "Pepes ikan kembung bumbu kuning, nasi liwet, lalap.", 34000,
     "pedas", "rendah_kalori,rendah_lemak,tinggi_protein,rendah_gula", "ikan", 1),
    ("Ayam Goreng Kremes Paket", "Ayam goreng kremes, nasi, sambal terasi, lalapan.", 33000,
     "asin", "tinggi_protein", "gluten (tepung terigu), telur, udang (terasi)", 1),
    ("Capcay Tahu Sayur", "Capcay sayur dengan tahu goreng dan nasi.", 27000,
     "asin", "rendah_kalori,rendah_gula,rendah_lemak", "kedelai (tahu), udang", 1),
    ("Snack Box Kue Basah", "Aneka kue basah tradisional (isi bervariasi).", 30000,
     "manis", "", "", 0),  # data alergen belum lengkap -> memicu peringatan
]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM menus").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO menus(name,description,price,taste,diets,allergens,allergen_known)"
            " VALUES (?,?,?,?,?,?,?)", SEED_MENUS)
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        env_pw = os.environ.get("ADMIN_PASSWORD")
        pw = env_pw or secrets.token_urlsafe(10)
        if len(pw) < 8:
            raise SystemExit("ADMIN_PASSWORD minimal 8 karakter.")
        con.execute(
            "INSERT INTO users(username,password_hash,full_name,phone,role) VALUES (?,?,?,?,'admin')",
            ("admin", generate_password_hash(pw), "Pengelola Catering", "081200000000"))
        if not env_pw:
            print("=" * 60)
            print("Akun admin dibuat -> username: admin | password:", pw)
            print("Simpan password ini; tidak akan ditampilkan lagi.")
            print("=" * 60)
    con.commit()
    con.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------- helpers
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")
REF_RE = re.compile(r"^FW-[A-HJ-NP-Z2-9]{6}$")
REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


def clean(value, maxlen):
    """Buang karakter kontrol, trim, dan batasi panjang."""
    return _CTRL.sub("", (value or "")).strip()[:maxlen]


def normalize_phone(value):
    return re.sub(r"[\s\-().]", "", value or "")


def to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def split_csv(value):
    return [x for x in (value or "").split(",") if x]


def safe_next(target):
    """Cegah open redirect: hanya path relatif internal."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


@app.template_filter("rupiah")
def rupiah(n):
    return "Rp " + f"{int(n):,}".replace(",", ".")


@app.template_filter("wib")
def wib(s):
    try:
        return (datetime.strptime(s, "%Y-%m-%d %H:%M:%S") + timedelta(hours=7)).strftime("%d-%m-%Y %H:%M WIB")
    except (TypeError, ValueError):
        return s or "-"


@app.template_filter("tgl")
def tgl(s):
    try:
        return date.fromisoformat(s).strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return s or "-"


@app.template_filter("status_label")
def status_label(s):
    return PAY_STATUS.get(s, s)


# --------------------------------------------------------------------------- security layer
@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        row = get_db().execute(
            "SELECT id, username, full_name, phone, role FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            g.user = row
        else:
            session.clear()


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf", "")
        expected = session.get("_csrf", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400)


@app.context_processor
def inject_globals():
    cart_count = len(get_cart()) if g.get("user") else 0
    return {"current_user": g.get("user"), "cart_count": cart_count,
            "TASTES": TASTES, "DIETS": DIETS, "MIN_QTY": MIN_QTY, "MAX_QTY": MAX_QTY}


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if app.config["SESSION_COOKIE_SECURE"]:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.user:
            nxt = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("login", next=nxt) if nxt else url_for("login"))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*a, **kw):
        if g.user["role"] != "admin":
            abort(404)  # sembunyikan keberadaan halaman admin
        return view(*a, **kw)
    return wrapper


# rate limiting login (in-memory; cukup untuk prototipe satu proses)
_FAILS = {}
WINDOW, MAX_PER_USER, MAX_PER_IP = 300, 5, 20


def _recent(key):
    now = time.time()
    lst = [t for t in _FAILS.get(key, []) if now - t < WINDOW]
    _FAILS[key] = lst
    return lst


def login_limited(ip, username):
    return len(_recent((ip, username.lower()))) >= MAX_PER_USER or len(_recent((ip, "*"))) >= MAX_PER_IP


def record_fail(ip, username):
    now = time.time()
    _recent((ip, username.lower())).append(now)
    _recent((ip, "*")).append(now)


# --------------------------------------------------------------------------- error pages
ERR_MSG = {
    400: "Permintaan tidak valid atau sesi kedaluwarsa. Muat ulang halaman lalu coba lagi.",
    403: "Akses ditolak.",
    404: "Halaman tidak ditemukan.",
    405: "Metode tidak diizinkan.",
    413: "Data yang dikirim terlalu besar.",
    429: "Terlalu banyak percobaan. Coba lagi nanti.",
}


def http_error(e):
    code = getattr(e, "code", 500) or 500
    return render_template("error.html", code=code, message=ERR_MSG.get(code, "Terjadi kesalahan pada server.")), code


for _c in (400, 403, 404, 405, 413, 429, 500):
    app.register_error_handler(_c, http_error)


# --------------------------------------------------------------------------- public pages
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/more")
def more():
    return render_template("more.html")


# --------------------------------------------------------------------------- auth
@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("menu"))
    errors, form = [], {}
    if request.method == "POST":
        f = request.form
        username = clean(f.get("username"), 30)
        full_name = clean(f.get("full_name"), 60)
        phone = normalize_phone(clean(f.get("phone"), 25))
        pw, pw2 = f.get("password", "")[:128], f.get("password2", "")[:128]
        form = {"username": username, "full_name": full_name, "phone": phone}
        if not USERNAME_RE.match(username):
            errors.append("Username 3-30 karakter: huruf, angka, atau underscore.")
        if len(full_name) < 2:
            errors.append("Nama lengkap wajib diisi (minimal 2 karakter).")
        if not PHONE_RE.match(phone):
            errors.append("Nomor telepon tidak valid (8-15 digit).")
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
            errors.append("Password minimal 8 karakter dan mengandung huruf serta angka.")
        elif pw.lower() == username.lower():
            errors.append("Password tidak boleh sama dengan username.")
        if pw != pw2:
            errors.append("Konfirmasi password tidak cocok.")
        if not errors:
            db = get_db()
            try:
                # role SELALU 'customer' dari server; input role dari client diabaikan
                cur = db.execute(
                    "INSERT INTO users(username,password_hash,full_name,phone,role) VALUES (?,?,?,?, 'customer')",
                    (username, generate_password_hash(pw), full_name, phone))
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("Username tidak tersedia. Gunakan username lain.")
            else:
                session.clear()
                session["uid"] = cur.lastrowid
                session.permanent = True
                flash("Akun berhasil dibuat. Lengkapi preferensi makanan Anda.", "ok")
                return redirect(url_for("preferences"))
    return render_template("register.html", errors=errors, form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if g.user:
        return redirect(nxt or url_for("menu"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:30]
        pw = (request.form.get("password") or "")[:128]
        ip = request.remote_addr or "?"
        if login_limited(ip, username):
            return render_template("login.html", error="Terlalu banyak percobaan gagal. Coba lagi dalam beberapa menit.",
                                   next=nxt), 429
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # selalu hitung hash agar waktu respons tidak membocorkan keberadaan username
        valid = check_password_hash(user["password_hash"] if user else DUMMY_HASH, pw) and user is not None
        if valid:
            _FAILS.pop((ip, username.lower()), None)
            session.clear()                       # cegah session fixation
            session["uid"] = user["id"]
            session.permanent = True
            default = url_for("admin_orders") if user["role"] == "admin" else url_for("menu")
            return redirect(nxt or default)
        record_fail(ip, username)
        error = "Username atau password salah."   # pesan seragam
    return render_template("login.html", error=error, next=nxt)


@app.post("/logout")
def logout():
    session.clear()
    flash("Anda telah keluar.", "ok")
    return redirect(url_for("home"))


# --------------------------------------------------------------------------- preferences
def get_prefs(uid):
    row = get_db().execute("SELECT * FROM preferences WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {"has_allergy": 0, "allergy_text": "", "tastes": [], "diets": []}
    return {"has_allergy": row["has_allergy"], "allergy_text": row["allergy_text"],
            "tastes": split_csv(row["tastes"]), "diets": split_csv(row["diets"])}


@app.route("/preferences", methods=["GET", "POST"])
@login_required
def preferences():
    errors = []
    prefs = get_prefs(g.user["id"])
    if request.method == "POST":
        f = request.form
        has_allergy = 1 if f.get("has_allergy") == "1" else 0
        allergy_text = clean(f.get("allergy_text"), 200) if has_allergy else ""
        tastes = [t for t in f.getlist("tastes") if t in TASTES]
        diets = [d for d in f.getlist("diets") if d in DIETS]
        if has_allergy and len(allergy_text) < 2:
            errors.append("Sebutkan alergi Anda (minimal 2 karakter).")
        prefs = {"has_allergy": has_allergy, "allergy_text": allergy_text, "tastes": tastes, "diets": diets}
        if not errors:
            db = get_db()
            db.execute(
                "INSERT INTO preferences(user_id,has_allergy,allergy_text,tastes,diets) VALUES (?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET has_allergy=excluded.has_allergy,"
                " allergy_text=excluded.allergy_text, tastes=excluded.tastes, diets=excluded.diets",
                (g.user["id"], has_allergy, allergy_text, ",".join(tastes), ",".join(diets)))
            db.commit()
            flash("Preferensi disimpan.", "ok")
            return redirect(url_for("menu", filter=1))
    return render_template("preferences.html", prefs=prefs, errors=errors)


# --------------------------------------------------------------------------- catalog
def allergy_tokens(text):
    return [t.strip() for t in re.split(r"[,;/\n]+", (text or "").lower()) if len(t.strip()) >= 3]


def has_allergen_conflict(menu, tokens):
    allergens = (menu["allergens"] or "").lower()
    menu_toks = allergy_tokens(allergens)
    for tok in tokens:
        if tok in allergens or any(m in tok for m in menu_toks):
            return True
    return False


def filter_menus(menus, prefs):
    tokens = allergy_tokens(prefs["allergy_text"]) if prefs["has_allergy"] else []
    result = []
    for m in menus:
        if prefs["tastes"] and m["taste"] not in prefs["tastes"]:
            continue
        if prefs["diets"] and not set(prefs["diets"]).issubset(set(m["diet_list"])):
            continue
        if tokens and has_allergen_conflict(m, tokens):
            continue
        result.append(m)
    return result


@app.route("/menu")
@login_required
def menu():
    prefs = get_prefs(g.user["id"])
    rows = [dict(r) for r in get_db().execute("SELECT * FROM menus ORDER BY id")]
    for r in rows:
        r["diet_list"] = split_csv(r["diets"])
    filtering = request.args.get("filter") == "1"
    if filtering:
        rows = filter_menus(rows, prefs)
    return render_template("menu.html", menus=rows, prefs=prefs, filtering=filtering)


# --------------------------------------------------------------------------- cart & checkout
def get_cart():
    raw = session.get("cart")
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            k, v = to_int(k), to_int(v)
            if k is not None and v is not None and MIN_QTY <= v <= MAX_QTY:
                out[str(k)] = v
    return out


def cart_rows(cart):
    """Bangun rincian dari DB. Harga SELALU dari server, bukan dari client."""
    rows, total = [], 0
    for mid, qty in cart.items():
        m = get_db().execute("SELECT * FROM menus WHERE id=?", (int(mid),)).fetchone()
        if not m:
            continue
        sub = m["price"] * qty
        total += sub
        rows.append({"menu": m, "qty": qty, "subtotal": sub})
    return rows, total


@app.post("/cart/add")
@login_required
def cart_add():
    mid, qty = to_int(request.form.get("menu_id")), to_int(request.form.get("qty"))
    if mid is None or not get_db().execute("SELECT 1 FROM menus WHERE id=?", (mid,)).fetchone():
        abort(404)
    if qty is None or not (MIN_QTY <= qty <= MAX_QTY):
        flash(f"Jumlah porsi harus bilangan bulat {MIN_QTY}-{MAX_QTY}.", "err")
        return redirect(url_for("menu"))
    cart = get_cart()
    cart[str(mid)] = qty
    session["cart"] = cart
    flash("Menu ditambahkan ke pesanan.", "ok")
    return redirect(url_for("menu"))


@app.post("/cart/remove")
@login_required
def cart_remove():
    cart = get_cart()
    cart.pop(str(to_int(request.form.get("menu_id"))), None)
    session["cart"] = cart
    return redirect(url_for("checkout"))


def validate_event_date(value):
    try:
        d = date.fromisoformat(value or "")
    except ValueError:
        return None, "Tanggal pesanan tidak valid."
    today = date.today()
    if d <= today:
        return None, "Tanggal pesanan minimal besok (H+1)."
    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        return None, f"Tanggal pesanan maksimal {MAX_DAYS_AHEAD} hari ke depan."
    return d.isoformat(), None


@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    cart = get_cart()
    if not cart:
        flash("Pesanan masih kosong. Pilih menu terlebih dahulu.", "err")
        return redirect(url_for("menu"))
    prefs = get_prefs(g.user["id"])
    draft = session.get("draft") or {}
    form = {
        "name": draft.get("name", g.user["full_name"]),
        "phone": draft.get("phone", g.user["phone"]),
        "date": draft.get("date", ""),
        "special": draft.get("special", ""),
        "allergy": draft.get("allergy", prefs["allergy_text"] if prefs["has_allergy"] else ""),
    }
    errors = []
    if request.method == "POST":
        f = request.form
        new_cart = {}
        for mid in cart:
            q = to_int(f.get(f"qty_{mid}"))
            if q is None or not (MIN_QTY <= q <= MAX_QTY):
                errors.append(f"Jumlah porsi harus bilangan bulat {MIN_QTY}-{MAX_QTY}.")
                new_cart[mid] = cart[mid]
            else:
                new_cart[mid] = q
        form = {
            "name": clean(f.get("name"), 60),
            "phone": normalize_phone(clean(f.get("phone"), 25)),
            "date": clean(f.get("date"), 10),
            "special": clean(f.get("special"), 500),
            "allergy": clean(f.get("allergy"), 200),
        }
        if len(form["name"]) < 2:
            errors.append("Nama pemesan wajib diisi.")
        if not PHONE_RE.match(form["phone"]):
            errors.append("Nomor telepon tidak valid (8-15 digit).")
        iso, err = validate_event_date(form["date"])
        if err:
            errors.append(err)
        if not errors:
            session["cart"] = new_cart
            session["draft"] = {**form, "date": iso}
            return redirect(url_for("checkout_review"))
        cart = new_cart
    rows, total = cart_rows(cart)
    min_date = (date.today() + timedelta(days=1)).isoformat()
    max_date = (date.today() + timedelta(days=MAX_DAYS_AHEAD)).isoformat()
    return render_template("checkout.html", rows=rows, total=total, form=form, errors=errors,
                           min_date=min_date, max_date=max_date)


def new_ref(db):
    for _ in range(10):
        ref = "FW-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM orders WHERE ref=?", (ref,)).fetchone():
            return ref
    raise RuntimeError("gagal membuat nomor referensi")


@app.route("/checkout/review", methods=["GET", "POST"])
@login_required
def checkout_review():
    cart, draft = get_cart(), session.get("draft")
    if not cart or not draft:
        return redirect(url_for("checkout"))
    iso, err = validate_event_date(draft.get("date"))   # validasi ulang di server
    if err:
        flash(err, "err")
        return redirect(url_for("checkout"))
    rows, total = cart_rows(cart)
    if not rows:
        return redirect(url_for("menu"))
    if request.method == "POST":
        db = get_db()
        try:
            ref = new_ref(db)
            cur = db.execute(
                "INSERT INTO orders(ref,user_id,contact_name,contact_phone,event_date,special_request,allergy_text,total)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ref, g.user["id"], draft["name"], draft["phone"], iso, draft["special"], draft["allergy"], total))
            oid = cur.lastrowid
            db.executemany(
                "INSERT INTO order_items(order_id,menu_id,menu_name,unit_price,qty) VALUES (?,?,?,?,?)",
                [(oid, r["menu"]["id"], r["menu"]["name"], r["menu"]["price"], r["qty"]) for r in rows])
            db.execute("INSERT INTO payments(order_id) VALUES (?)", (oid,))
            db.commit()
        except Exception:
            db.rollback()
            flash("Pesanan gagal disimpan. Silakan coba lagi.", "err")   # NFR-06
            return redirect(url_for("checkout_review"))
        session.pop("cart", None)
        session.pop("draft", None)
        flash("Pesanan dibuat. Lanjutkan ke pembayaran.", "ok")
        return redirect(url_for("order_pay", ref=ref))
    return render_template("review.html", rows=rows, total=total, draft=draft)


# --------------------------------------------------------------------------- orders (customer)
ORDER_SQL = ("SELECT o.*, u.username, p.status AS pay_status, p.payer_note, p.confirmed_at, p.verified_at, p.admin_note"
             " FROM orders o JOIN payments p ON p.order_id=o.id JOIN users u ON u.id=o.user_id WHERE o.ref=?")


def get_order(ref, owner_only=True):
    if not REF_RE.match(ref):
        abort(404)
    o = get_db().execute(ORDER_SQL, (ref,)).fetchone()
    # 404 (bukan 403) agar keberadaan pesanan orang lain tidak bocor (anti-IDOR/enumerasi)
    if not o or (owner_only and o["user_id"] != g.user["id"]):
        abort(404)
    items = get_db().execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (o["id"],)).fetchall()
    return o, items


@app.route("/orders")
@login_required
def orders():
    rows = get_db().execute(
        "SELECT o.ref, o.event_date, o.total, o.created_at, p.status AS pay_status FROM orders o"
        " JOIN payments p ON p.order_id=o.id WHERE o.user_id=? ORDER BY o.id DESC", (g.user["id"],)).fetchall()
    return render_template("orders.html", orders=rows)


@app.route("/orders/<ref>")
@login_required
def order_detail(ref):
    o, items = get_order(ref)
    return render_template("order.html", o=o, items=items)


@app.route("/orders/<ref>/pay")
@login_required
def order_pay(ref):
    o, items = get_order(ref)
    return render_template("pay.html", o=o, items=items)


@app.post("/orders/<ref>/confirm")
@login_required
def order_confirm(ref):
    o, _ = get_order(ref)
    note = clean(request.form.get("payer_note"), 200)
    db = get_db()
    # transisi atomik: hanya dari 'awaiting_payment'. Klik ganda tidak menggandakan apa pun.
    cur = db.execute(
        "UPDATE payments SET status='awaiting_verification', payer_note=?, confirmed_at=datetime('now')"
        " WHERE order_id=? AND status='awaiting_payment'", (note, o["id"]))
    db.commit()
    if cur.rowcount == 1:
        flash("Konfirmasi terkirim. Pembayaran menunggu verifikasi pengelola.", "ok")
    else:
        flash("Konfirmasi tidak dapat diulang untuk pesanan ini.", "err")
    return redirect(url_for("order_detail", ref=ref))


# --------------------------------------------------------------------------- admin
@app.route("/admin/orders")
@admin_required
def admin_orders():
    status = request.args.get("status", "")
    sql = ("SELECT o.ref, o.contact_name, o.event_date, o.total, o.created_at, o.allergy_text, u.username,"
           " p.status AS pay_status FROM orders o JOIN payments p ON p.order_id=o.id"
           " JOIN users u ON u.id=o.user_id")
    args = ()
    if status in PAY_STATUS:
        sql += " WHERE p.status=?"
        args = (status,)
    rows = get_db().execute(sql + " ORDER BY o.id DESC", args).fetchall()
    return render_template("admin_orders.html", orders=rows, status=status, PAY_STATUS=PAY_STATUS)


@app.route("/admin/orders/<ref>")
@admin_required
def admin_order(ref):
    o, items = get_order(ref, owner_only=False)
    return render_template("admin_order.html", o=o, items=items)


@app.post("/admin/orders/<ref>/verify")
@admin_required
def admin_verify(ref):
    o, _ = get_order(ref, owner_only=False)
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        abort(400)
    new_status = "paid" if action == "approve" else "rejected"
    note = clean(request.form.get("admin_note"), 200)
    db = get_db()
    cur = db.execute(
        "UPDATE payments SET status=?, verified_by=?, verified_at=datetime('now'), admin_note=?"
        " WHERE order_id=? AND status='awaiting_verification'", (new_status, g.user["id"], note, o["id"]))
    if cur.rowcount == 1:
        db.execute("INSERT INTO audit_log(actor_id,action,detail) VALUES (?,?,?)",
                   (g.user["id"], "verify_payment", f"{ref} -> {new_status}"))
        flash(f"Status pembayaran {ref} diperbarui: {PAY_STATUS[new_status]}.", "ok")
    else:
        flash("Status hanya dapat diubah saat pembayaran 'Menunggu verifikasi'.", "err")
    db.commit()
    return redirect(url_for("admin_order", ref=ref))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
