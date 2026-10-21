import { createClient } from '@supabase/supabase-js';
import { SignJWT, jwtVerify } from 'jose';
import { randomBytes, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';

const scrypt = promisify(scryptCallback);
const supabaseUrl = process.env.SUPABASE_URL;
const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
const signingSecret = process.env.FOODWEB_SECRET || process.env.JWT_SECRET;
const db = supabaseUrl && serviceKey ? createClient(supabaseUrl, serviceKey, {
  auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false }
}) : null;
const secretKey = signingSecret ? new TextEncoder().encode(signingSecret) : null;
const COOKIE_SESSION = 'foodweb_session';
const COOKIE_CSRF = 'foodweb_csrf';
const PAY_STATUS = {
  awaiting_payment: 'Menunggu pembayaran',
  awaiting_verification: 'Menunggu verifikasi',
  paid: 'Pembayaran berhasil',
  rejected: 'Pembayaran ditolak'
};
const TASTES = new Set(['asin', 'manis', 'pedas']);
const DIETS = new Set(['rendah_kalori', 'rendah_gula', 'tinggi_protein', 'rendah_lemak']);

function json(statusCode, data, extra = {}) {
  return { statusCode, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', ...extra.headers }, body: JSON.stringify(data), ...(extra.cookies ? { cookies: extra.cookies } : {}) };
}
function fail(status, message) { return json(status, { error: message }); }
function cookieValue(headers, name) {
  const raw = headers.cookie || headers.Cookie || '';
  for (const part of raw.split(';')) {
    const i = part.indexOf('=');
    if (i > -1 && part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
  }
  return '';
}
function cookie(name, value, maxAge, httpOnly = true) {
  return `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${maxAge}; SameSite=Lax; Secure${httpOnly ? '; HttpOnly' : ''}`;
}
function clearCookies() {
  return [cookie(COOKIE_SESSION, '', 0, true), cookie(COOKIE_CSRF, '', 0, false)];
}
function clean(value, max = 200) { return String(value ?? '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, max); }
function phone(value) { return String(value ?? '').replace(/\D/g, '').slice(0, 15); }
function validPhone(value) { return /^\d{8,15}$/.test(value); }
function normalizePath(event) {
  const raw = event.rawUrl || `https://localhost${event.path || '/'}`;
  let path = new URL(raw).pathname;
  path = path.replace(/^\/.netlify\/functions\/api/, '');
  if (path === '/api') path = '/';
  else if (path.startsWith('/api/')) path = path.slice(4);
  return path || '/';
}
function bodyOf(event) {
  if (!event.body) return {};
  if (String(event.body).length > 64 * 1024) throw Object.assign(new Error('Permintaan terlalu besar.'), { status: 413 });
  try { return JSON.parse(event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body); }
  catch { throw Object.assign(new Error('Format JSON tidak valid.'), { status: 400 }); }
}
async function passwordHash(password) {
  const salt = randomBytes(16).toString('hex');
  const derived = await scrypt(password, salt, 64);
  return `scrypt$${salt}$${Buffer.from(derived).toString('hex')}`;
}
async function verifyPassword(password, encoded) {
  try {
    const [scheme, salt, hex] = String(encoded).split('$');
    if (scheme !== 'scrypt' || !salt || !hex) return false;
    const actual = Buffer.from(await scrypt(password, salt, 64));
    const expected = Buffer.from(hex, 'hex');
    return actual.length === expected.length && timingSafeEqual(actual, expected);
  } catch { return false; }
}
async function newSession(user) {
  if (!secretKey || signingSecret.length < 32) throw new Error('FOODWEB_SECRET harus diisi dengan minimal 32 karakter.');
  const csrf = randomBytes(24).toString('hex');
  const token = await new SignJWT({ uid: user.id, csrf })
    .setProtectedHeader({ alg: 'HS256' }).setIssuedAt().setExpirationTime('2h').sign(secretKey);
  return { token, csrf };
}
async function requireUser(event, { csrf = false, admin = false } = {}) {
  if (!secretKey || !db) throw Object.assign(new Error('Konfigurasi server belum lengkap.'), { status: 503 });
  const token = cookieValue(event.headers || {}, COOKIE_SESSION);
  if (!token) throw Object.assign(new Error('Silakan masuk terlebih dahulu.'), { status: 401 });
  let claims;
  try { ({ payload: claims } = await jwtVerify(token, secretKey)); }
  catch { throw Object.assign(new Error('Sesi tidak valid atau sudah berakhir. Silakan masuk lagi.'), { status: 401 }); }
  if (csrf) {
    const cookieToken = cookieValue(event.headers || {}, COOKIE_CSRF);
    const headerToken = (event.headers || {})['x-csrf-token'] || (event.headers || {})['X-CSRF-Token'] || '';
    if (!claims.csrf || !cookieToken || !headerToken || claims.csrf !== cookieToken || claims.csrf !== headerToken) {
      throw Object.assign(new Error('Token keamanan tidak valid. Muat ulang halaman dan coba lagi.'), { status: 403 });
    }
  }
  const { data: user, error } = await db.from('users').select('id,username,full_name,phone,role,created_at').eq('id', claims.uid).maybeSingle();
  if (error) throw new Error('Gagal memeriksa sesi pengguna.');
  if (!user) throw Object.assign(new Error('Akun tidak ditemukan.'), { status: 401 });
  if (admin && user.role !== 'admin') throw Object.assign(new Error('Akses khusus pengelola.'), { status: 403 });
  return user;
}
function checkOrigin(event) {
  const origin = event.headers?.origin || event.headers?.Origin;
  if (!origin) return;
  const raw = event.rawUrl || '';
  const host = event.headers?.['x-forwarded-host'] || event.headers?.host || event.headers?.Host;
  let expected;
  try { expected = raw ? new URL(raw).origin : (host ? `https://${host}` : null); } catch { expected = null; }
  if (expected && origin !== expected) throw Object.assign(new Error('Origin tidak diizinkan.'), { status: 403 });
}
async function getPreferences(userId) {
  const { data, error } = await db.from('preferences').select('*').eq('user_id', userId).maybeSingle();
  if (error) throw new Error('Gagal membaca preferensi.');
  return data || { user_id: userId, has_allergy: false, allergy_text: '', tastes: [], diets: [] };
}
function allergyTokens(text) { return String(text || '').toLowerCase().split(/[,;/\n]+/).map(x => x.trim()).filter(x => x.length >= 3); }
function allergenConflict(menu, tokens) {
  const allergens = String(menu.allergens || '').toLowerCase();
  const menuTokens = allergyTokens(allergens);
  return tokens.some(t => allergens.includes(t) || menuTokens.some(m => t.includes(m)));
}
function filterMenus(menus, prefs) {
  const tokens = prefs.has_allergy ? allergyTokens(prefs.allergy_text) : [];
  return menus.filter(m => {
    if (prefs.tastes?.length && !prefs.tastes.includes(m.taste)) return false;
    if (prefs.diets?.length && !prefs.diets.every(d => (m.diets || []).includes(d))) return false;
    if (tokens.length && allergenConflict(m, tokens)) return false;
    return true;
  });
}
async function getOrder(ref, user, ownerOnly = true) {
  if (!/^FW-[A-Z0-9]{6}$/.test(ref)) throw Object.assign(new Error('Pesanan tidak ditemukan.'), { status: 404 });
  const { data: order, error } = await db.from('orders').select('*').eq('ref', ref).maybeSingle();
  if (error) throw new Error('Gagal membaca pesanan.');
  if (!order || (ownerOnly && order.user_id !== user.id)) throw Object.assign(new Error('Pesanan tidak ditemukan.'), { status: 404 });
  const [itemsRes, paymentRes, userRes] = await Promise.all([
    db.from('order_items').select('*').eq('order_id', order.id).order('id'),
    db.from('payments').select('*').eq('order_id', order.id).maybeSingle(),
    db.from('users').select('username').eq('id', order.user_id).maybeSingle()
  ]);
  if (itemsRes.error || paymentRes.error || userRes.error) throw new Error('Gagal membaca rincian pesanan.');
  return { ...order, username: userRes.data?.username || '', payment: paymentRes.data, pay_status: paymentRes.data?.status, items: itemsRes.data || [] };
}
async function listOrders(userId = null, status = '') {
  let query = db.from('orders').select('*').order('created_at', { ascending: false });
  if (userId) query = query.eq('user_id', userId);
  const { data: orders, error } = await query;
  if (error) throw new Error('Gagal membaca daftar pesanan.');
  const ids = (orders || []).map(o => o.id);
  if (!ids.length) return [];
  const [paymentsRes, usersRes] = await Promise.all([
    db.from('payments').select('*').in('order_id', ids),
    db.from('users').select('id,username').in('id', [...new Set(orders.map(o => o.user_id))])
  ]);
  if (paymentsRes.error || usersRes.error) throw new Error('Gagal membaca status pesanan.');
  const payMap = new Map((paymentsRes.data || []).map(p => [p.order_id, p]));
  const userMap = new Map((usersRes.data || []).map(u => [u.id, u.username]));
  return orders.map(o => ({ ...o, username: userMap.get(o.user_id) || '', payment: payMap.get(o.id) || null, pay_status: payMap.get(o.id)?.status }))
    .filter(o => !status || o.pay_status === status);
}

export const handler = async (event) => {
  if (!db || !secretKey || (signingSecret || '').length < 32) {
    return fail(503, 'Server belum dikonfigurasi. Isi SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, dan FOODWEB_SECRET (minimal 32 karakter).');
  }
  let path;
  try { path = normalizePath(event); } catch { return fail(400, 'URL tidak valid.'); }
  const method = event.httpMethod || event.requestContext?.http?.method || 'GET';
  let body = {};
  try { if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) body = bodyOf(event); }
  catch (e) { return fail(e.status || 400, e.message); }

  try {
    if (method === 'GET' && path === '/health') return json(200, { ok: true, service: 'foodweb-api' });

    if (method === 'POST' && path === '/register') {
      checkOrigin(event);
      const username = clean(body.username, 30).toLowerCase();
      const fullName = clean(body.full_name, 60);
      const normalizedPhone = phone(body.phone);
      const password = String(body.password || '').slice(0, 128);
      const password2 = String(body.password2 || '').slice(0, 128);
      if (!/^[A-Za-z0-9_]{3,30}$/.test(username)) return fail(400, 'Username 3-30 karakter: huruf, angka, atau underscore.');
      if (fullName.length < 2) return fail(400, 'Nama lengkap wajib diisi (minimal 2 karakter).');
      if (!validPhone(normalizedPhone)) return fail(400, 'Nomor telepon tidak valid (8-15 digit).');
      if (password.length < 8 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) return fail(400, 'Password minimal 8 karakter dan mengandung huruf serta angka.');
      if (password.toLowerCase() === username.toLowerCase()) return fail(400, 'Password tidak boleh sama dengan username.');
      if (password !== password2) return fail(400, 'Konfirmasi password tidak cocok.');
      const { data: existing, error: existingErr } = await db.from('users').select('id').eq('username', username).maybeSingle();
      if (existingErr) throw new Error('Gagal memeriksa username.');
      if (existing) return fail(409, 'Username tidak tersedia. Gunakan username lain.');
      const hash = await passwordHash(password);
      const { data: user, error } = await db.from('users').insert({ username, password_hash: hash, full_name: fullName, phone: normalizedPhone, role: 'customer' }).select('id,username,full_name,phone,role,created_at').single();
      if (error) {
        if (error.code === '23505') return fail(409, 'Username tidak tersedia. Gunakan username lain.');
        throw new Error('Gagal membuat akun.');
      }
      await db.from('preferences').upsert({ user_id: user.id, has_allergy: false, allergy_text: '', tastes: [], diets: [] });
      const session = await newSession(user);
      return json(201, { user, csrf: session.csrf }, { cookies: [cookie(COOKIE_SESSION, session.token, 7200, true), cookie(COOKIE_CSRF, session.csrf, 7200, false)] });
    }

    if (method === 'POST' && path === '/login') {
      checkOrigin(event);
      const username = clean(body.username, 30).toLowerCase();
      const password = String(body.password || '').slice(0, 128);
      const ip = clean(event.headers?.['x-nf-client-connection-ip'] || event.headers?.['x-forwarded-for']?.split(',')[0] || 'unknown', 80);
      const usernameKey = username.toLowerCase();
      const since = new Date(Date.now() - 5 * 60 * 1000).toISOString();
      const { count, error: countErr } = await db.from('login_attempts').select('id', { count: 'exact', head: true }).eq('username_key', usernameKey).eq('ip_key', ip).eq('succeeded', false).gte('created_at', since);
      if (countErr) throw new Error('Gagal memeriksa percobaan login.');
      const { count: ipCount, error: ipErr } = await db.from('login_attempts').select('id', { count: 'exact', head: true }).eq('ip_key', ip).eq('succeeded', false).gte('created_at', since);
      if (ipErr) throw new Error('Gagal memeriksa percobaan login.');
      if ((count || 0) >= 5 || (ipCount || 0) >= 20) return fail(429, 'Terlalu banyak percobaan gagal. Coba lagi dalam beberapa menit.');
      const { data: user, error } = await db.from('users').select('id,username,password_hash,full_name,phone,role,created_at').eq('username', username).maybeSingle();
      if (error) throw new Error('Gagal memeriksa akun.');
      const valid = user ? await verifyPassword(password, user.password_hash) : await verifyPassword(password, 'scrypt$bad$00');
      await db.from('login_attempts').insert({ username_key: usernameKey, ip_key: ip, succeeded: Boolean(user && valid) });
      if (!user || !valid) return fail(401, 'Username atau password salah.');
      await db.from('login_attempts').delete().eq('username_key', usernameKey).eq('ip_key', ip);
      const session = await newSession(user);
      const { password_hash, ...safeUser } = user;
      return json(200, { user: safeUser, csrf: session.csrf }, { cookies: [cookie(COOKIE_SESSION, session.token, 7200, true), cookie(COOKIE_CSRF, session.csrf, 7200, false)] });
    }

    if (method === 'GET' && path === '/me') {
      const user = await requireUser(event);
      return json(200, { user });
    }

    if (method === 'POST' && path === '/logout') {
      checkOrigin(event);
      await requireUser(event, { csrf: true });
      return json(200, { ok: true }, { cookies: clearCookies() });
    }

    if (method === 'GET' && path === '/menus') {
      const user = await requireUser(event);
      const url = new URL(event.rawUrl || `https://localhost${event.path || '/api/menus'}`);
      const { data: menus, error } = await db.from('menus').select('id,name,description,price,taste,diets,allergens,allergen_known').eq('active', true).order('id');
      if (error) throw new Error('Gagal membaca menu.');
      const prefs = await getPreferences(user.id);
      return json(200, { menus: url.searchParams.get('filter') === '1' ? filterMenus(menus || [], prefs) : (menus || []), preferences: prefs });
    }

    if (path === '/preferences' && method === 'GET') {
      const user = await requireUser(event);
      return json(200, { preferences: await getPreferences(user.id) });
    }
    if (path === '/preferences' && method === 'PUT') {
      checkOrigin(event);
      const user = await requireUser(event, { csrf: true });
      const hasAllergy = Boolean(body.has_allergy);
      const allergyText = hasAllergy ? clean(body.allergy_text, 200) : '';
      const tastes = Array.isArray(body.tastes) ? [...new Set(body.tastes.filter(x => TASTES.has(x)))] : [];
      const diets = Array.isArray(body.diets) ? [...new Set(body.diets.filter(x => DIETS.has(x)))] : [];
      if (hasAllergy && allergyText.length < 2) return fail(400, 'Sebutkan alergi Anda (minimal 2 karakter).');
      const { data, error } = await db.from('preferences').upsert({ user_id: user.id, has_allergy: hasAllergy, allergy_text: allergyText, tastes, diets, updated_at: new Date().toISOString() }).select().single();
      if (error) throw new Error('Preferensi gagal disimpan.');
      return json(200, { preferences: data });
    }

    if (path === '/orders' && method === 'POST') {
      checkOrigin(event);
      const user = await requireUser(event, { csrf: true });
      const contactName = clean(body.contact_name, 60);
      const contactPhone = phone(body.contact_phone);
      const eventDate = clean(body.event_date, 10);
      const specialRequest = clean(body.special_request, 500);
      const allergyText = clean(body.allergy_text, 200);
      const items = Array.isArray(body.items) ? body.items.map(i => ({ menu_id: Number(i.menu_id), qty: Number(i.qty) })) : [];
      if (items.some(i => !Number.isSafeInteger(i.menu_id) || !Number.isSafeInteger(i.qty) || i.qty < 5 || i.qty > 500)) return fail(400, 'Jumlah porsi harus bilangan bulat 5-500.');
      const { data, error } = await db.rpc('create_foodweb_order', { p_user_id: user.id, p_contact_name: contactName, p_contact_phone: contactPhone, p_event_date: eventDate, p_special_request: specialRequest, p_allergy_text: allergyText, p_items: items });
      if (error) {
        const msg = error.message || '';
        const known = ['Nama pemesan','Nomor telepon','Tanggal pesanan','Jumlah porsi','Menu tidak ditemukan','Keranjang'];
        return fail(400, known.some(x => msg.includes(x)) ? msg : 'Pesanan gagal dibuat. Periksa tanggal, data kontak, dan jumlah porsi.');
      }
      await db.from('audit_log').insert({ actor_id: user.id, action: 'create_order', detail: `${data.ref} dibuat` });
      return json(201, { order: data });
    }

    if (path === '/orders' && method === 'GET') {
      const user = await requireUser(event);
      const orders = await listOrders(user.id);
      return json(200, { orders: orders.map(o => ({ ref: o.ref, event_date: o.event_date, total: o.total, created_at: o.created_at, pay_status: o.pay_status, status_label: PAY_STATUS[o.pay_status] || o.pay_status })) });
    }

    let match = path.match(/^\/orders\/([A-Z0-9-]+)$/);
    if (match && method === 'GET') {
      const user = await requireUser(event);
      return json(200, { order: await getOrder(match[1], user, true) });
    }
    match = path.match(/^\/orders\/([A-Z0-9-]+)\/confirm$/);
    if (match && method === 'POST') {
      checkOrigin(event);
      const user = await requireUser(event, { csrf: true });
      const order = await getOrder(match[1], user, true);
      const note = clean(body.payer_note, 200);
      const { data: updated, error } = await db.from('payments').update({ status: 'awaiting_verification', payer_note: note, confirmed_at: new Date().toISOString() }).eq('order_id', order.id).eq('status', 'awaiting_payment').select('id').maybeSingle();
      if (error) throw new Error('Konfirmasi pembayaran gagal.');
      if (!updated) return fail(409, 'Konfirmasi tidak dapat diulang untuk pesanan ini.');
      await db.from('audit_log').insert({ actor_id: user.id, action: 'confirm_payment', detail: `${order.ref} menunggu verifikasi` });
      return json(200, { ok: true });
    }

    if (path === '/admin/orders' && method === 'GET') {
      await requireUser(event, { admin: true });
      const url = new URL(event.rawUrl || 'https://localhost/api/admin/orders');
      const status = url.searchParams.get('status') || '';
      const orders = await listOrders(null, Object.hasOwn(PAY_STATUS, status) ? status : '');
      return json(200, { orders: orders.map(o => ({ ref: o.ref, contact_name: o.contact_name, event_date: o.event_date, total: o.total, created_at: o.created_at, allergy_text: o.allergy_text, username: o.username, pay_status: o.pay_status, status_label: PAY_STATUS[o.pay_status] || o.pay_status })) });
    }
    match = path.match(/^\/admin\/orders\/([A-Z0-9-]+)$/);
    if (match && method === 'GET') {
      const user = await requireUser(event, { admin: true });
      return json(200, { order: await getOrder(match[1], user, false) });
    }
    match = path.match(/^\/admin\/orders\/([A-Z0-9-]+)\/verify$/);
    if (match && method === 'POST') {
      checkOrigin(event);
      const user = await requireUser(event, { csrf: true, admin: true });
      const order = await getOrder(match[1], user, false);
      const action = body.action;
      if (!['approve', 'reject'].includes(action)) return fail(400, 'Aksi verifikasi tidak valid.');
      const newStatus = action === 'approve' ? 'paid' : 'rejected';
      const note = clean(body.admin_note, 200);
      const { data: updated, error } = await db.from('payments').update({ status: newStatus, verified_by: user.id, verified_at: new Date().toISOString(), admin_note: note }).eq('order_id', order.id).eq('status', 'awaiting_verification').select('id').maybeSingle();
      if (error) throw new Error('Verifikasi pembayaran gagal.');
      if (!updated) return fail(409, 'Status hanya dapat diubah saat pembayaran menunggu verifikasi.');
      await db.from('audit_log').insert({ actor_id: user.id, action: 'verify_payment', detail: `${order.ref} -> ${newStatus}` });
      return json(200, { ok: true, status: newStatus, status_label: PAY_STATUS[newStatus] });
    }

    return fail(404, 'Endpoint tidak ditemukan.');
  } catch (error) {
    const status = error.status || 500;
    if (status >= 500) console.error('FoodWeb API error:', error);
    return fail(status, status >= 500 ? (error.message || 'Terjadi kesalahan server.') : error.message);
  }
};
