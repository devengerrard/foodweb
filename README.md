# Food Web v0.4 (prototipe akademik)

Website pemesanan catering. **Login wajib** untuk menu, preferensi, checkout, dan status pesanan.
Publik hanya: Home, About, More, Login, Register.

**Stack:** Python · Flask · SQLite · Jinja2 (tanpa framework JS, tanpa build step)

## Menjalankan

```bash
cd foodweb
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
ADMIN_PASSWORD='GantiPasswordIni1' python app.py     # Windows (PowerShell): $env:ADMIN_PASSWORD='...'; python app.py
```

Buka http://127.0.0.1:5000. Akun admin: username `admin`, password dari `ADMIN_PASSWORD`
(jika tidak diisi, password acak dicetak sekali di terminal saat database pertama dibuat).

Variabel opsional: `FOODWEB_SECRET` (kunci sesi), `FOODWEB_HTTPS=1` (cookie Secure + HSTS saat memakai HTTPS), `FOODWEB_DB` (path database).

## Alur

Daftar/Masuk → Preferensi (alergi, rasa, diet) → Menu (bisa disaring) → Pesanan/Checkout → Ringkasan →
Pembayaran QRIS (simulasi) → "Sudah Bayar" (status: *menunggu verifikasi*) → Admin memverifikasi → *berhasil / ditolak*.

## Pemetaan keamanan

| Ancaman | Mitigasi di kode |
|---|---|
| SQL Injection | Semua query memakai parameter `?`, tidak ada string concatenation |
| XSS | Autoescape Jinja, tanpa `\|safe`/inline script, CSP `script-src 'self'`, cookie HttpOnly |
| CSRF | Token per sesi di semua POST + cookie SameSite=Lax |
| IDOR / akses admin | Cek kepemilikan di server, 404 untuk pesanan orang lain, `admin_required` |
| Privilege escalation | Role diset server (`customer`), perubahan status hanya via endpoint admin |
| Brute force | Batas 5 gagal/5 menit per user+IP (20 per IP), pesan error seragam |
| Password | Hash scrypt/PBKDF2 (Werkzeug), min. 8 karakter huruf+angka |
| Session | `session.clear()` saat login, masa berlaku 2 jam, cookie HttpOnly/SameSite |
| Price tampering | Harga & total selalu dihitung ulang dari DB, porsi divalidasi 5-500 |
| Duplikasi konfirmasi | Transisi status atomik (`UPDATE ... WHERE status=...`) |
| Open redirect | Parameter `next` hanya path internal |
| Clickjacking & header | CSP, X-Frame-Options, nosniff, Referrer-Policy, no-store |
| Upload berbahaya | Tidak ada fitur upload (konfirmasi berupa catatan teks) |

## Tes

```bash
python -m unittest discover -s tests -v
```

## Batasan prototipe

- QRIS adalah gambar **simulasi** (tidak bisa dipindai). Untuk transaksi nyata dibutuhkan QRIS merchant yang sah.
- Rate limiting disimpan di memori (satu proses). Untuk produksi gunakan Redis/WSGI server + HTTPS.
- Penyaringan alergi hanya berdasarkan teks alergen menu, **bukan jaminan aman**.
- Belum ada reset password, 2FA, atau notifikasi (di luar scope PRD).
