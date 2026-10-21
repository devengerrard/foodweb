# Catering Oma — Netlify migration

Migrasi dari Flask + Jinja2 + SQLite menjadi frontend statis + Netlify Functions (Node.js) + Supabase PostgreSQL. Fitur yang dipertahankan: registrasi/login dengan username, preferensi rasa/diet/alergi, katalog dan filter menu, keranjang, checkout, riwayat/detail pesanan, konfirmasi pembayaran simulasi, serta panel admin untuk verifikasi pembayaran.

> **Penting:** ini paket migrasi baru; tidak mengubah repository GitHub Anda secara otomatis. Jangan upload `instance/foodweb.db` atau `instance/secret_key`. QRIS masih gambar demo, bukan pembayaran nyata.

## Struktur

- `public/` — frontend HTML/CSS/JavaScript
- `netlify/functions/api.mjs` — API serverless, autentikasi cookie HttpOnly, CSRF double-submit, validasi role, rate limit login
- `supabase/migrations/001_initial_schema.sql` — schema PostgreSQL, seed menu, dan fungsi transaksi pembuatan order
- `netlify.toml` — konfigurasi Netlify

## 1. Buat database Supabase

1. Buat project baru di https://supabase.com/.
2. Buka **SQL Editor**.
3. Jalankan seluruh isi `supabase/migrations/001_initial_schema.sql`.
4. Buka **Project Settings → API**. Catat Project URL dan `service_role` key. `service_role` adalah rahasia server, jangan taruh di frontend, GitHub, atau chat.

## 2. Deploy ke Netlify

1. Upload folder hasil migrasi ini ke repository GitHub baru atau branch terpisah, misalnya `netlify-migration`. Jangan menimpa branch `main` sebelum pengujian.
2. Login ke https://app.netlify.com/ lalu pilih **Add new site / Import an existing project** dan hubungkan repository.
3. Build settings: **Base directory** kosong, **Build command** kosong, **Publish directory** `public`, **Functions directory** `netlify/functions`.
4. Di **Site configuration → Environment variables**, tambahkan:
   - `SUPABASE_URL` = URL project Supabase
   - `SUPABASE_SERVICE_ROLE_KEY` = service role key Supabase
   - `FOODWEB_SECRET` = string acak rahasia minimal 32 karakter (gunakan password manager/generator)
5. Deploy. Setelah variabel lingkungan diubah, trigger deploy ulang jika diperlukan.
6. Tes `https://NAMA-SITE.netlify.app/api/health`; hasil yang diharapkan `{"ok":true,"service":"foodweb-api"}`.

## 3. Jadikan akun admin

1. Daftar akun normal melalui website yang sudah dideploy.
2. Buka Supabase SQL Editor dan ubah `username` ke username Anda yang sebenarnya:

```sql
update public.users set role = 'admin' where username = 'USERNAME_ADMIN';
```

3. Keluar lalu masuk lagi agar UI membaca role terbaru. Jangan pernah menerima role admin dari form registrasi.

## 4. Uji alur

- Daftar akun pelanggan, logout, lalu login.
- Simpan preferensi rasa/diet/alergi dan coba filter menu.
- Tambahkan menu ke keranjang; kuantitas tiap menu harus 5–500.
- Checkout dengan tanggal acara besok hingga 90 hari ke depan.
- Buat pesanan, buka halaman pembayaran simulasi, dan kirim konfirmasi.
- Login sebagai admin, buka pesanan, setujui/tolak status menunggu verifikasi.
- Pastikan pelanggan tidak bisa melihat pesanan pengguna lain dengan mengubah referensi URL.

## Catatan teknis dan batasan

- API memakai cookie sesi `HttpOnly`, `SameSite=Lax`, `Secure`, dan CSRF token untuk operasi yang mengubah data. `FOODWEB_SECRET` wajib panjang minimal 32 karakter.
- Login rate-limit disimpan di PostgreSQL; bersihkan tabel `login_attempts` berkala jika volume demo meningkat.
- Pembuatan order menggunakan PostgreSQL function agar harga, total, validasi jumlah, dan insert order/item/payment terjadi dalam satu transaksi server-side.
- Backend menggunakan Supabase service-role key. Akses tabel dari browser tidak dilakukan; jangan menambahkan key ini ke `public/`.
- Database lama SQLite tidak dimigrasikan otomatis. Schema ini dimulai dengan menu demo dan pengguna baru; jika perlu migrasi akun/order lama, buat skrip konversi terpisah dan jangan pindahkan password hash Werkzeug langsung ke sistem hash baru tanpa penyesuaian.
- Belum ada payment gateway nyata, email/notifikasi, reset password, dan 2FA. QRIS adalah simulasi akademik.
- Free tier dan kebijakan verifikasi provider dapat berubah. Periksa ketentuan terbaru Supabase dan Netlify sebelum memilih paket.

## Pengembangan lokal

Node.js 20+ diperlukan. Jalankan `npm install`, lalu gunakan Netlify CLI (`npx netlify dev`) setelah menyiapkan environment variables. `netlify dev` menyediakan route `/api/*` ke Functions.
