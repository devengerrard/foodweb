"""Smoke test keamanan & alur Food Web. Jalankan: python -m unittest discover -s tests -v"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

_tmp = tempfile.mkdtemp()
os.environ["FOODWEB_DB"] = os.path.join(_tmp, "test.db")
os.environ["FOODWEB_SECRET"] = "test-secret"
os.environ["ADMIN_PASSWORD"] = "AdminPass123"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as foodweb  # noqa: E402

TOKEN_RE = re.compile(r'name="_csrf" value="([^"]+)"')
TOMORROW = (date.today() + timedelta(days=3)).isoformat()


def token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    m = TOKEN_RE.search(html)
    assert m, f"CSRF token tidak ditemukan di {path}"
    return m.group(1)


def post(client, path, data, page=None):
    data = dict(data)
    data["_csrf"] = token(client, page or path)
    return client.post(path, data=data)


def new_client():
    return foodweb.app.test_client()


def register(client, username, password="Passw0rd123"):
    r = post(client, "/register", {"username": username, "full_name": "Tester " + username, "phone": "081234567890",
                                   "password": password, "password2": password})
    assert r.status_code == 302, r.get_data(as_text=True)[:300]


def login(client, username, password):
    return post(client, "/login", {"username": username, "password": password})


def place_order(client, special="Tanpa bawang", allergy="", qty=10, menu_id=1):
    post(client, "/cart/add", {"menu_id": menu_id, "qty": qty}, page="/menu")
    r = post(client, "/checkout", {f"qty_{menu_id}": qty, "name": "Budi", "phone": "08123456789", "date": TOMORROW,
                                   "special": special, "allergy": allergy}, page="/checkout")
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    r = post(client, "/checkout/review", {}, page="/checkout/review")
    assert r.status_code == 302
    return re.search(r"/orders/(FW-[A-Z0-9]{6})/pay", r.headers["Location"]).group(1)


def db_one(sql, args=()):
    con = sqlite3.connect(os.environ["FOODWEB_DB"])
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


class SecurityTests(unittest.TestCase):
    # ---- login wajib
    def test_public_vs_protected_pages(self):
        c = new_client()
        for p in ("/", "/about", "/more", "/login", "/register"):
            self.assertEqual(c.get(p).status_code, 200, p)
        for p in ("/menu", "/preferences", "/checkout", "/orders", "/admin/orders"):
            r = c.get(p)
            self.assertEqual(r.status_code, 302, p)
            self.assertIn("/login", r.headers["Location"], p)

    # ---- SQL injection
    def test_sql_injection(self):
        c = new_client()
        r = login(c, "' OR '1'='1' --", "x")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Username atau password salah", r.get_data(as_text=True))
        register(c, "sqli_user")
        payload = "kacang'; DROP TABLE users; --"
        post(c, "/preferences", {"has_allergy": "1", "allergy_text": payload, "tastes": "manis"})
        self.assertIsNotNone(db_one("SELECT 1 FROM users LIMIT 1"))           # tabel masih ada
        self.assertEqual(db_one("SELECT allergy_text FROM preferences WHERE user_id=(SELECT id FROM users WHERE username='sqli_user')")[0], payload)
        self.assertEqual(c.get("/menu?filter=1&x=' OR 1=1--").status_code, 200)

    # ---- XSS
    def test_stored_xss_is_escaped_for_admin(self):
        c = new_client()
        register(c, "xss_user")
        ref = place_order(c, special="<script>alert(1)</script>", allergy='"><img src=x onerror=alert(2)>')
        a = new_client()
        login(a, "admin", "AdminPass123")
        html = a.get(f"/admin/orders/{ref}").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        r = a.get("/admin/orders")
        self.assertNotIn("<img src=x", r.get_data(as_text=True))

    def test_security_headers_and_cookie(self):
        c = new_client()
        r = c.get("/")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        r = login(c, "admin", "AdminPass123")
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    # ---- CSRF
    def test_csrf_required(self):
        c = new_client()
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "AdminPass123"}).status_code, 400)
        register(c, "csrf_user")
        self.assertEqual(c.post("/logout", data={}).status_code, 400)
        self.assertEqual(c.post("/cart/add", data={"menu_id": 1, "qty": 10, "_csrf": "salah"}).status_code, 400)

    # ---- IDOR / access control
    def test_idor_and_admin_access(self):
        a, b = new_client(), new_client()
        register(a, "owner_a")
        register(b, "other_b")
        ref = place_order(a)
        for path in (f"/orders/{ref}", f"/orders/{ref}/pay"):
            self.assertEqual(b.get(path).status_code, 404, path)
        self.assertEqual(post(b, f"/orders/{ref}/confirm", {}, page="/menu").status_code, 404)
        self.assertEqual(a.get(f"/orders/{ref}").status_code, 200)
        # customer tidak bisa ke area admin
        self.assertEqual(b.get("/admin/orders").status_code, 404)
        self.assertEqual(b.get(f"/admin/orders/{ref}").status_code, 404)
        self.assertEqual(post(b, f"/admin/orders/{ref}/verify", {"action": "approve"}, page="/menu").status_code, 404)
        self.assertEqual(db_one("SELECT status FROM payments p JOIN orders o ON o.id=p.order_id WHERE o.ref=?", (ref,))[0], "awaiting_payment")
        # ref tidak valid
        self.assertEqual(a.get("/orders/1' OR '1'='1").status_code, 404)

    def test_register_cannot_set_role(self):
        c = new_client()
        post(c, "/register", {"username": "sneaky", "full_name": "Sneaky", "phone": "081234567890",
                              "password": "Passw0rd123", "password2": "Passw0rd123", "role": "admin"})
        self.assertEqual(db_one("SELECT role FROM users WHERE username='sneaky'")[0], "customer")

    # ---- price tampering & validation
    def test_price_tampering_and_qty_validation(self):
        c = new_client()
        register(c, "price_user")
        post(c, "/cart/add", {"menu_id": 1, "qty": 10, "price": 1, "total": 1}, page="/menu")
        r = post(c, "/checkout", {"qty_1": 10, "name": "Budi", "phone": "08123456789", "date": TOMORROW,
                                  "special": "", "allergy": "", "price": 1, "total": 1}, page="/checkout")
        self.assertEqual(r.status_code, 302)
        r = post(c, "/checkout/review", {"total": 1}, page="/checkout/review")
        ref = re.search(r"(FW-[A-Z0-9]{6})", r.headers["Location"]).group(1)
        self.assertEqual(db_one("SELECT total FROM orders WHERE ref=?", (ref,))[0], 35000 * 10)
        for bad in ("0", "-5", "4", "501", "abc", "1.5", ""):
            c.get("/menu")
            post(c, "/cart/add", {"menu_id": 2, "qty": bad}, page="/menu")
        self.assertIsNone(db_one("SELECT 1 FROM order_items WHERE menu_id=2"))
        r = post(c, "/cart/add", {"menu_id": 999, "qty": 10}, page="/menu")
        self.assertEqual(r.status_code, 404)
        # tanggal tidak valid
        post(c, "/cart/add", {"menu_id": 1, "qty": 10}, page="/menu")
        past = (date.today() - timedelta(days=1)).isoformat()
        r = post(c, "/checkout", {"qty_1": 10, "name": "B", "phone": "08123456789", "date": past}, page="/checkout")
        self.assertEqual(r.status_code, 200)
        self.assertIn("minimal besok", r.get_data(as_text=True))

    # ---- payment state machine
    def test_payment_state_machine(self):
        c = new_client()
        register(c, "pay_user")
        ref = place_order(c, allergy="udang")
        status = lambda: db_one("SELECT p.status FROM payments p JOIN orders o ON o.id=p.order_id WHERE o.ref=?", (ref,))[0]
        self.assertEqual(status(), "awaiting_payment")
        post(c, f"/orders/{ref}/confirm", {"payer_note": "a/n Budi"}, page=f"/orders/{ref}/pay")
        self.assertEqual(status(), "awaiting_verification")        # bukan lunas
        post(c, f"/orders/{ref}/confirm", {}, page=f"/orders/{ref}")  # klik ganda
        self.assertEqual(status(), "awaiting_verification")
        # pelanggan tidak bisa memaksa status lewat parameter
        post(c, f"/orders/{ref}/confirm", {"status": "paid"}, page=f"/orders/{ref}")
        self.assertEqual(status(), "awaiting_verification")
        a = new_client()
        login(a, "admin", "AdminPass123")
        post(a, f"/admin/orders/{ref}/verify", {"action": "approve"}, page=f"/admin/orders/{ref}")
        self.assertEqual(status(), "paid")
        post(a, f"/admin/orders/{ref}/verify", {"action": "reject"}, page=f"/admin/orders/{ref}")
        self.assertEqual(status(), "paid")                          # tidak bisa dibalik
        self.assertEqual(db_one("SELECT COUNT(*) FROM audit_log WHERE detail LIKE ?", (f"{ref}%",))[0], 1)

    def test_admin_can_reject(self):
        c = new_client()
        register(c, "reject_user")
        ref = place_order(c)
        post(c, f"/orders/{ref}/confirm", {}, page=f"/orders/{ref}/pay")
        a = new_client()
        login(a, "admin", "AdminPass123")
        post(a, f"/admin/orders/{ref}/verify", {"action": "reject", "admin_note": "dana belum masuk"}, page=f"/admin/orders/{ref}")
        self.assertIn("ditolak", c.get(f"/orders/{ref}").get_data(as_text=True))

    # ---- brute force, open redirect, password storage
    def test_bruteforce_lockout(self):
        c = new_client()
        register(c, "brute_user")
        c.post("/logout", data={"_csrf": token(c, "/menu")})
        for _ in range(5):
            login(c, "brute_user", "salah-salah")
        r = login(c, "brute_user", "Passw0rd123")      # password benar tetap diblokir sementara
        self.assertEqual(r.status_code, 429)
        self.assertIn("Terlalu banyak", r.get_data(as_text=True))

    def test_open_redirect_blocked(self):
        c = new_client()
        register(c, "redir_user")
        c.post("/logout", data={"_csrf": token(c, "/menu")})
        for evil in ("//evil.com", "https://evil.com", "/\\evil.com"):
            r = post(c, "/login", {"username": "redir_user", "password": "Passw0rd123", "next": evil}, page="/login")
            self.assertEqual(r.status_code, 302)
            self.assertNotIn("evil.com", r.headers["Location"])
            c.post("/logout", data={"_csrf": token(c, "/menu")})

    def test_password_hashed_and_no_user_enumeration(self):
        register(new_client(), "hash_user", "Rahasia12345")
        h = db_one("SELECT password_hash FROM users WHERE username='hash_user'")[0]
        self.assertNotIn("Rahasia12345", h)
        self.assertTrue(h.startswith(("scrypt:", "pbkdf2:")))
        c = new_client()
        m1 = login(c, "tidak_ada", "x").get_data(as_text=True)
        m2 = login(c, "hash_user", "salah").get_data(as_text=True)
        pick = lambda s: re.search(r'class="flash err">([^<]+)<', s).group(1)
        self.assertEqual(pick(m1), pick(m2))

    def test_weak_passwords_rejected(self):
        c = new_client()
        for pw in ("short1", "alllettersonly", "12345678"):
            r = post(c, "/register", {"username": "weak_" + str(len(pw)), "full_name": "Weak", "phone": "081234567890",
                                      "password": pw, "password2": pw})
            self.assertEqual(r.status_code, 200, pw)

    # ---- filter & alergi
    def test_allergy_filter(self):
        c = new_client()
        register(c, "allergy_user")
        post(c, "/preferences", {"has_allergy": "1", "allergy_text": "kacang"}, page="/preferences")
        html = c.get("/menu?filter=1").get_data(as_text=True)
        self.assertNotIn("Sate Ayam Bumbu Kacang", html)
        self.assertNotIn("Pecel Sayur Lontong", html)
        self.assertIn("bukan jaminan", html)
        post(c, "/preferences", {"has_allergy": "0", "tastes": "pedas", "diets": ["rendah_kalori", "rendah_lemak"]}, page="/preferences")
        html = c.get("/menu?filter=1").get_data(as_text=True)
        self.assertIn("Pepes Ikan Kembung", html)
        self.assertNotIn("Nasi Kuning Komplit", html)
        # kombinasi tanpa menu cocok (manis + rendah kalori) -> pesan hasil kosong
        post(c, "/preferences", {"has_allergy": "0", "tastes": "manis", "diets": "rendah_kalori"}, page="/preferences")
        self.assertIn("Tidak ada menu yang cocok", c.get("/menu?filter=1").get_data(as_text=True))
        # menu dengan data alergen tidak lengkap diberi peringatan
        self.assertIn("Data alergen belum lengkap", c.get("/menu").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
