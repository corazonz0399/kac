from flask import Flask, render_template, request, jsonify, Response, redirect, session, make_response, send_file
from datetime import datetime, date, timedelta
import json
import hmac
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from functools import wraps

from config import SECRET_KEY, ACCOUNTS, ADMIN_PASSWORD, GROUPS, ACCOUNT_COLORS
try:
    from config import META_ACCESS_TOKEN as META_TOKEN
except:
    META_TOKEN = ''

# Load Telegram token — prioritaskan config.json langsung
TELEGRAM_BOT_TOKEN = ''
_config_json_path = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(_config_json_path, encoding='utf-8') as _f:
        _cfg = json.load(_f)
        TELEGRAM_BOT_TOKEN = _cfg.get('telegram_bot_token', '') or ''
except:
    pass

# Cron Bridge config
CRON_BRIDGE_URL = _cfg.get('cron_bridge_url', 'http://43.133.57.134:5002') if _cfg.get('cron_bridge_url') else 'http://43.133.57.134:5002'
# DeepSeek API key (chat cepat widget)
DEEPSEEK_API_KEY = _cfg.get('deepseek_api_key', '') or os.environ.get('DEEPSEEK_API_KEY', '')
CRON_BRIDGE_TOKEN = _cfg.get('cron_bridge_token', '') or ''
# Token buat cron LOKAL nembak /api/fetch (meta_sync.py, kac_sync_15min.py) — biar endpoint itu
# nggak perlu dibuka publik. Header: X-Cron-Token.
CRON_API_TOKEN = _cfg.get('cron_api_token', '') or ''

def _bridge_call(endpoint, method='GET', data=None, timeout=15):
    """Call Hermes Cron Bridge API. Returns parsed JSON response."""
    import urllib.request as ureq
    url = f"{CRON_BRIDGE_URL}{endpoint}"
    headers = {
        'Authorization': f'Bearer {CRON_BRIDGE_TOKEN}',
        'Content-Type': 'application/json',
    }
    if data is not None:
        data = json.dumps(data).encode()
    req = ureq.Request(url, data=data, headers=headers, method=method)
    try:
        with ureq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {'success': False, 'error': str(e)}

# Fallback: dari config.py
if not TELEGRAM_BOT_TOKEN:
    try:
        from config import TELEGRAM_BOT_TOKEN as _tg_token
        TELEGRAM_BOT_TOKEN = _tg_token or ''
    except:
        pass

# Fallback: dari Hermes .env
if not TELEGRAM_BOT_TOKEN:
    try:
        import subprocess
        _r = subprocess.run(
            ['python3', '-c', 'import os; from hermes_cli.env_loader import load_hermes_dotenv; load_hermes_dotenv(); t = os.environ.get(\"TELEGRAM_BOT_TOKEN\", \"\"); print(t)'],
            capture_output=True, text=True, cwd=os.path.expanduser('~/.hermes'),
            timeout=10
        )
        TELEGRAM_BOT_TOKEN = _r.stdout.strip()
    except:
        pass
from database import (
    init_db, get_accounts, get_campaigns, get_daily_summary,
    save_campaigns, save_adsets, get_adsets, log_report, get_latest_date,
    update_campaign_status, update_adset_status, save_budget_change, get_budget_changes,
    get_connection, save_page_post, get_page_posts, get_page_post_pages,
    get_products, get_product, save_product, delete_product,
    hash_api_key as db_hash_api_key, get_api_key_by_hash, touch_api_key,
    log_api_access, count_api_calls_last_minute
)
from meta_fetcher import fetch_all_accounts, fetch_all_adsets, format_currency, generate_adset_suggestions

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB max
app.secret_key = SECRET_KEY
app.config['JSON_AS_ASCII'] = False
# Session login persistent — biar nggak bolak-balik login
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ===== API KEY (akses eksternal) =====
# Whitelist endpoint yang boleh diakses pakai header X-API-Key.
# Di luar daftar ini TETAP butuh login session admin — toggle campaign/adset, ubah budget,
# /api/fetch, sync, chat, dailyleveling add/remove SENGAJA tidak dibuka.
API_KEY_ALLOWED = {
    ('GET', '/api/summary'),
    ('GET', '/api/summary-live'),
    ('GET', '/api/konten-rasio'),
    ('GET', '/api/accounts'),
    ('GET', '/api/campaigns'),
    ('GET', '/api/campaign/detail'),
    ('GET', '/api/adsets'),
    ('GET', '/api/ads'),
    ('GET', '/api/budget/campaigns'),
    ('GET', '/api/bank-konten'),
    ('GET', '/api/bank-konten/genmilk'),
    ('GET', '/api/dailyleveling/data'),
    ('GET', '/api/page-posts'),
    ('GET', '/api/ads-posts'),
    ('GET', '/api/post-by-id'),
    ('GET', '/api/custom-audiences/meta'),
    ('GET', '/api/search-interests'),
    # satu-satunya endpoint mutasi yang dibuka: bikin campaign dari Campaign Studio
    ('POST', '/api/campaign-studio/create'),
}
API_RATE_LIMIT_PER_MIN = 60
API_KEY_HEADER = 'X-API-Key'


def _api_client_ip():
    """IP pemanggil (hormati X-Forwarded-For karena app di belakang tunnel/nginx)."""
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:64]
    return (request.remote_addr or '')[:64]


def _api_log(key_id, key_prefix, status, note=''):
    try:
        log_api_access(key_id, key_prefix, request.method, request.path, status, _api_client_ip(), note)
    except Exception as e:
        print(f"[APIKEY] log error: {e}", flush=True)


def _send_api_create_notification(key_row, req_body, resp_payload, http_code):
    """Notif Telegram tiap /api/campaign-studio/create dipanggil pakai API key (jejak + safety)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    import subprocess
    ok = (http_code or 0) == 200 and isinstance(resp_payload, dict) and resp_payload.get('status') == 'success'
    icon = '🚀' if ok else '⚠️'
    label = (key_row or {}).get('label', '-')
    prefix = (key_row or {}).get('key_prefix', '-')
    body = req_body if isinstance(req_body, dict) else {}
    adsets = body.get('adsets') if isinstance(body.get('adsets'), list) else []
    budget = body.get('daily_budget') or 0
    try:
        budget_txt = f"Rp{int(budget):,}".replace(',', '.')
    except Exception:
        budget_txt = str(budget)
    msg = ''
    if isinstance(resp_payload, dict):
        msg = (resp_payload.get('message') or '') if ok else (resp_payload.get('error') or resp_payload.get('message') or '')
    status_txt = 'BERHASIL dibuat (ACTIVE)' if ok else ('GAGAL — ' + (f'HTTP {http_code}' if http_code != 200 else 'ditolak server'))
    text = (
        f"{icon} <b>Campaign Studio via API Key</b>\n"
        f"{'━'*30}\n"
        f"🔑 Key: {label} ({prefix}…)\n"
        f"📛 Campaign: {body.get('name', '-')}\n"
        f"👤 Akun: {body.get('account', '-')}\n"
        f"📦 Adset: {len(adsets)}\n"
        f"💰 Budget: {budget_txt}\n"
        f"🌐 IP: {_api_client_ip()}\n"
        f"📌 Status: {status_txt}\n"
        + (f"📝 {str(msg)[:220]}\n" if msg else '')
        + f"{'━'*30}"
    )
    payload = urllib.parse.urlencode({
        'chat_id': '-1003990111670', 'message_thread_id': '1958',
        'text': text, 'parse_mode': 'HTML',
    })
    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10',
             f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"],
            capture_output=True, text=True, timeout=15
        )
        print(f"[APIKEY] notif create: {r.stdout[:160]}", flush=True)
    except Exception as e:
        print(f"[APIKEY] notif create error: {e}", flush=True)


# ===== ADMIN DECORATOR =====
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('admin'):
            return f(*args, **kwargs)

        # ---- Token cron lokal (cuma buat /api/fetch, dipanggil meta_sync/kac_sync_15min) ----
        if request.path == '/api/fetch' and CRON_API_TOKEN:
            _tok = (request.headers.get('X-Cron-Token') or '').strip()
            if _tok and hmac.compare_digest(_tok, CRON_API_TOKEN):
                return f(*args, **kwargs)

        # ---- Jalur API key (akses eksternal) ----
        raw_key = (request.headers.get(API_KEY_HEADER) or '').strip()
        if raw_key:
            key_row = None
            try:
                key_row = get_api_key_by_hash(db_hash_api_key(raw_key))
            except Exception as e:
                print(f"[APIKEY] verify error: {e}", flush=True)
            if not key_row:
                _api_log(None, raw_key[:12], 401, 'key tidak valid / sudah di-revoke')
                return jsonify({'status': 'error', 'message': 'API key tidak valid atau sudah di-revoke'}), 401

            if (request.method, request.path) not in API_KEY_ALLOWED:
                _api_log(key_row['id'], key_row['key_prefix'], 403, 'endpoint di luar whitelist')
                return jsonify({
                    'status': 'error',
                    'message': f"{request.method} {request.path} tidak dibuka untuk API key (butuh login admin)",
                }), 403

            used = count_api_calls_last_minute(key_row['id'])
            if used >= API_RATE_LIMIT_PER_MIN:
                _api_log(key_row['id'], key_row['key_prefix'], 429, f'rate limit {used}/menit')
                return jsonify({
                    'status': 'error',
                    'message': f'Rate limit {API_RATE_LIMIT_PER_MIN} request/menit terlampaui, coba lagi sebentar',
                }), 429

            try:
                touch_api_key(key_row['id'])
            except Exception:
                pass
            resp = f(*args, **kwargs)
            code = resp[1] if isinstance(resp, tuple) and len(resp) > 1 else getattr(resp, 'status_code', 200)
            _api_log(key_row['id'], key_row['key_prefix'], code, 'ok')
            if request.path == '/api/campaign-studio/create':
                try:
                    payload = (resp[0] if isinstance(resp, tuple) else resp).get_json(silent=True)
                except Exception:
                    payload = None
                _send_api_create_notification(key_row, request.get_json(silent=True) or {}, payload, code)
            return resp

        if request.path.startswith('/api/'):
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
        return redirect('/')
    return decorated


@app.route('/')
def index():
    # Kalau admin sudah login → langsung ke dashboard (nggak lihat landing lagi)
    if session.get('admin'):
        return redirect('/dashboard')
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/dashboard')
@app.route('/dashboard/<group>')
@admin_required
def dashboard(group='generos'):
    if group not in GROUPS:
        group = 'generos'
    g = GROUPS[group]
    accounts = g['accounts']
    group_names = g.get('names') or {a: a for a in accounts}
    return render_template('dashboard.html',
        group=group,
        group_label=g['label'],
        group_accounts=accounts,
        group_names=group_names,
        acc_colors_dark={a: ACCOUNT_COLORS[a]['dark'] for a in accounts},
        acc_colors_light={a: ACCOUNT_COLORS[a]['light'] for a in accounts},
        acc_donut_dark={a: ACCOUNT_COLORS[a]['donut_dark'] for a in accounts},
        acc_donut_light={a: ACCOUNT_COLORS[a]['donut_light'] for a in accounts},
        acc_thr=g['thresholds'],
        total_thr=g['total_threshold'],
    )


@app.route('/dashboard-v2')
@admin_required
def dashboard_v2():
    """Halaman baru (sampel): dashboard gaya Plausible Analytics — data embedded dari DB KAC."""
    return render_template('dashboard_v2.html')


@app.route('/dev/campaign-builder')
@admin_required
def dev_campaign_builder():
    return render_template('dev_campaign_builder.html')

@app.route('/dev/campaign-studio')
@admin_required
def dev_campaign_studio():
    return render_template('cb_studio.html')


@app.route('/custom-audiences')
@admin_required
def custom_audiences_page():
    return render_template('custom_audiences.html')


@app.route('/kuota-9router')
@app.route('/kuota-9-router')
@admin_required
def page_kuota_9router():
    return render_template('kuota_9router.html')


@app.route('/api/kuota-9router')
@admin_required
def api_kuota_9router():
    """Sync kuota provider 9router (read-only, dari server ke server)."""
    try:
        import quota_9router
        return jsonify(quota_9router.sync())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/fetch', methods=['POST'])
@admin_required
def api_fetch():
    """Trigger Meta API fetch and save to database."""
    try:
        since_str = request.args.get('since') or date.today().isoformat()
        until_str = request.args.get('until') or date.today().isoformat()
        since_date = datetime.strptime(since_str, '%Y-%m-%d').date()
        until_date = datetime.strptime(until_str, '%Y-%m-%d').date()

        all_results = []
        total_saved_all = 0
        total_adset_saved = 0
        current = since_date
        while current <= until_date:
            report_date = current
            # Fetch and save campaign data
            data = fetch_all_accounts(
                since_date=report_date.isoformat(),
                until_date=report_date.isoformat()
            )
            total_saved = 0
            results = []
            for short_name, info in data.items():
                account_id = info['account_id']
                saved = save_campaigns(account_id, info['campaigns'], report_date)
                log_report(
                    account_id, report_date,
                    info['total_spend'], info['total_results'],
                    info['avg_cpr'], info['active_count']
                )
                total_saved += saved
                results.append({
                    'account': short_name,
                    'campaigns_saved': saved,
                    'total_spend': info['total_spend'],
                    'total_results': info['total_results'],
                    'avg_cpr': info['avg_cpr'],
                    'active': info['active_count']
                })
            total_saved_all += total_saved

            # Fetch and save adset data
            adset_data = fetch_all_adsets(
                since_date=report_date.isoformat(),
                until_date=report_date.isoformat()
            )
            adset_results = []
            for short_name, info in adset_data.items():
                account_id = info['account_id']
                saved_adsets = save_adsets(account_id, info['adsets'], report_date)
                total_adset_saved += saved_adsets
                adset_results.append({
                    'account': short_name,
                    'adsets_saved': saved_adsets,
                    'total_spend': info['total_spend'],
                    'total_results': info['total_results'],
                    'active': info['active_count']
                })

            all_results.append({
                'date': report_date.isoformat(),
                'accounts': results,
                'adsets': adset_results
            })
            current += timedelta(days=1)

        return jsonify({
            'status': 'success',
            'date_from': since_str,
            'date_to': until_str,
            'total_saved': total_saved_all,
            'total_adsets_saved': total_adset_saved,
            'dates': len(all_results),
            'detail': all_results
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Cache in-memory untuk /api/summary — dashboard manggil endpoint ini tiap render + auto-refresh 5 menit.
# Query JOIN campaigns+accounts GROUP BY 30 hari bisa lambat (6-9 detik). Cache 60 detik biar responsif.
_SUMMARY_CACHE = {}
_SUMMARY_CACHE_TTL = 60
# Cache in-memory untuk endpoint analisis berat (campaigns/adsets/ads)
# Query MySQL JOIN 44K+ baris bisa 1-6 detik — cache biar load halaman ngebut.
_ANALYSIS_CACHE = {}
_ANALYSIS_CACHE_TTL = 90
# Cache in-memory untuk /api/summary-live (live fetch langsung Meta API, opsi C 2026-09-09).
# TTL pendek (45 dtk) karena ini "live"; dipisah dari _SUMMARY_CACHE biar nggak ganggu query DB.
_LIVE_SUMMARY_CACHE = {}
_LIVE_SUMMARY_TTL = 45
# Proteksi anti rate-limit: min interval antar fetch Meta live per key (detik).
_LIVE_MIN_INTERVAL = 15

def _analysis_cache(key, producer, ttl=None):
    """Cache hasil producer() per key dgn TTL detik. Aman untuk JSON-serializable."""
    ttl = ttl or _ANALYSIS_CACHE_TTL
    now = time.time()
    cached = _ANALYSIS_CACHE.get(key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    val = producer()
    if val is not None:
        _ANALYSIS_CACHE[key] = (now, val)
    return val


@app.route('/api/summary')
@admin_required
def api_summary():
    days = request.args.get('days', 7, type=int)
    account_id = request.args.get('account_id')
    group = request.args.get('group')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    cache_key = (days, account_id or '', group or '', date_from or '', date_to or '')

    now = time.time()
    cached = _SUMMARY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _SUMMARY_CACHE_TTL:
        return jsonify(cached[1])

    summary = []
    try:
        account_ids = None
        if group in GROUPS:
            account_ids = [ACCOUNTS[a]['id'] for a in GROUPS[group]['accounts']]
        summary = get_daily_summary(account_id=account_id, days=days, date_from=date_from, date_to=date_to, account_ids=account_ids)
    except Exception as e:
        print(f"[SUMMARY] DB query error: {e}", flush=True)
    if not summary:
        # Guard: JANGAN manggil URL sendiri (loop request). Fallback cuma untuk dev lokal
        # yang butuh ambil data dari production (host beda).
        try:
            url = 'https://report.anaksehatgeneros.com/api/summary?days=' + str(days)
            if group: url += '&group=' + group
            if account_id: url += '&account_id=' + account_id
            if date_from: url += '&date_from=' + date_from
            if date_to: url += '&date_to=' + date_to
            if request.host != 'report.anaksehatgeneros.com':
                resp = urllib.request.urlopen(url, timeout=10)
                summary = json.loads(resp.read())
        except Exception as e:
            print(f"[SUMMARY] fallback skip/error: {e}", flush=True)

    _SUMMARY_CACHE[cache_key] = (now, summary)
    return jsonify(summary)


# ===== LIVE SUMMARY (opsi C — 2026-09-09) =====
# Dashboard "Hari Ini" fetch LANGSUNG ke Meta API (kayak GIGAS), bukan dari DB snapshot.
# Dipakai hanya untuk range single-day. Multi-day tetap DB (historis nggak berubah & hemat request).
def _live_action_type(acc_key):
    """Action type utk 'Hasil' per akun — konsisten dengan aturan evaluasi (G1/G2=lead pixel, G3/G4=ATC)."""
    ev = (ACCOUNTS.get(acc_key) or {}).get('event', '')
    return 'add_to_cart' if ev == 'add_to_cart' else 'offsite_conversion.fb_pixel_lead'


def _live_fetch_account_day(acc_key, day):
    """Fetch spend + hasil 1 akun utk 1 hari langsung dari Meta (level campaign, filter by time_range)."""
    acc = ACCOUNTS.get(acc_key)
    if not acc:
        return None
    aid = acc['id']  # act_xxx
    action_type = _live_action_type(acc_key)
    try:
        import urllib.request as _ur, urllib.parse as _up, urllib.error as _ue
        token = _get_meta_token()
        qs = _up.urlencode({
            'access_token': token,
            'fields': 'campaign_id,campaign_name,spend,actions',
            'level': 'campaign',
            'time_range': json.dumps({'since': day, 'until': day}),
            'limit': 500,
        })
        url = f'https://graph.facebook.com/v26.0/{aid}/insights?{qs}'
        resp = _ur.urlopen(_ur.Request(url), timeout=30)
        data = json.loads(resp.read())
    except Exception as e:
        print(f'[LIVE] fetch {acc_key} {day} error: {e}', flush=True)
        return None

    rows = data.get('data', []) or []
    spend = 0.0
    hasil = 0
    active = 0
    for r in rows:
        s = float(r.get('spend', 0) or 0)
        if s <= 0:
            continue
        spend += s
        active += 1
        for a in r.get('actions', []) or []:
            if a.get('action_type') == action_type:
                hasil += int(a.get('value', 0) or 0)
    if spend <= 0 and hasil <= 0:
        return None
    return {
        'account_id': aid,
        'short_name': acc_key,
        'total_spend': round(spend, 0),
        'total_results': hasil,
        'avg_cpr': round(spend / hasil, 0) if hasil > 0 else 0,
        'active_campaigns': active,
        'total_campaigns': len(rows),
    }


@app.route('/api/summary-live')
@admin_required
def api_summary_live():
    """Ringkasan LIVE langsung dari Meta API untuk range single-day (Hari Ini / Kemarin).

    - Hanya single-day (date_from == date_to). Multi-day → fallback ke DB summary.
    - Cache 45 detik + min interval 15 detik antar fetch per key (proteksi rate limit).
    - Format respons SAMA dengan /api/summary: [{account_id, date, short_name, total_spend,
      total_results, avg_cpr, active_campaigns, total_campaigns}]
    """
    group = request.args.get('group', 'generos')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    if group not in GROUPS:
        group = 'generos'

    # Default single day = hari ini (WIB)
    if not date_from or not date_to:
        from datetime import timezone as _tz, timedelta as _td
        _wib = datetime.now(_tz(_td(hours=7))).strftime('%Y-%m-%d')
        date_from = date_to = _wib

    # Validasi format
    try:
        d1 = datetime.strptime(date_from, '%Y-%m-%d').date()
        d2 = datetime.strptime(date_to, '%Y-%m-%d').date()
    except Exception:
        return jsonify({'error': 'format tanggal YYYY-MM-DD'}), 400

    # Multi-day → DB summary biasa (historis nggak berubah; hemat request Meta)
    if d1 != d2:
        account_ids = [ACCOUNTS[a]['id'] for a in GROUPS[group]['accounts']]
        return jsonify(get_daily_summary(account_ids=account_ids, date_from=date_from, date_to=date_to))

    day = d1.isoformat()
    cache_key = (group, day)
    now = time.time()

    cached = _LIVE_SUMMARY_CACHE.get(cache_key)
    if cached:
        age = now - cached[0]
        if age < _LIVE_SUMMARY_TTL:
            return jsonify(cached[1])
        # Cache expired — kalau belum lewat min interval, tetap balikin cache lama (anti spam)
        if age < _LIVE_MIN_INTERVAL:
            return jsonify(cached[1])

    rows = []
    for acc_key in GROUPS[group]['accounts']:
        row = _live_fetch_account_day(acc_key, day)
        if row:
            row['date'] = day
            rows.append(row)

    if not rows:
        # Meta gagal / kosong → fallback DB (jangan sampai dashboard kosong)
        account_ids = [ACCOUNTS[a]['id'] for a in GROUPS[group]['accounts']]
        rows = get_daily_summary(account_ids=account_ids, date_from=day, date_to=day)

    _LIVE_SUMMARY_CACHE[cache_key] = (now, rows)
    return jsonify(rows)


@app.route('/api/campaigns')
@admin_required
def api_campaigns():
    account_id = request.args.get('account_id')
    date_str = request.args.get('date')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    limit = request.args.get('limit', 3000, type=int)

    date_obj = None
    date_from_obj = None
    date_to_obj = None

    if date_str:
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        except:
            date_obj = None

    if date_from and date_to:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d').date()
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d').date()
        except:
            pass

    campaigns = []
    try:
        def _produce_campaigns():
            try:
                return get_campaigns(account_id=account_id, date=date_obj,
                                     date_from=date_from_obj, date_to=date_to_obj,
                                     limit=limit)
            except Exception:
                # Fallback ke live API kalo MySQL lokal gagal
                try:
                    url = 'https://report.anaksehatgeneros.com/api/campaigns?limit=' + str(limit)
                    if account_id: url += '&account_id=' + account_id
                    if date_str: url += '&date=' + date_str
                    if date_from and date_to: url += '&date_from=' + date_from + '&date_to=' + date_to
                    # Guard: jangan manggil diri sendiri (loop request) — fallback cuma untuk dev lokal
                    if request.host != 'report.anaksehatgeneros.com':
                        resp = urllib.request.urlopen(url, timeout=10)
                        return json.loads(resp.read())
                except Exception as e:
                    print(f"[CAMPAIGNS] fallback skip/error: {e}", flush=True)
                return None
        ckey = ('campaigns', account_id, date_str, date_from, date_to, limit)
        campaigns = _analysis_cache(ckey, _produce_campaigns) or []
    except Exception as e:
        print(f"[CAMPAIGNS] cache error: {e}", flush=True)

    return jsonify(campaigns)


@app.route('/api/accounts')
@admin_required
def api_accounts():
    accounts = []
    try:
        accounts = get_accounts()
    except:
        pass
    if not accounts:
        try:
            # Guard: jangan manggil diri sendiri (loop request)
            if request.host != 'report.anaksehatgeneros.com':
                resp = urllib.request.urlopen('https://report.anaksehatgeneros.com/api/accounts', timeout=10)
                accounts = json.loads(resp.read())
        except Exception as e:
            print(f"[ACCOUNTS] fallback skip/error: {e}", flush=True)
    return jsonify([{
        'short_name': a['short_name'],
        'account_id': a['account_id'],
        'name': a['name'],
        'event_type': a['event_type']
    } for a in accounts])


BANK_DATA_FILE = os.path.join(os.path.dirname(__file__), 'bank_konten_data.json')

@app.route('/api/konten-rasio')
@admin_required
def api_konten_rasio():
    """Return rasio konten — jenis konten iklan ACTIVE per kategori (dari nama iklan)."""
    token = META_TOKEN or ''
    if not token:
        try:
            with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                cfg = json.load(f)
                token = cfg.get('meta_token', '')
        except:
            pass
    if not token:
        return jsonify({'categories': [], 'total': 0, 'error': 'No token'})

    import subprocess, json as _json
    from config import ACCOUNTS
    import re as _re

    CONTENT_CATEGORIES = ['Promo', 'Edukasi', 'Testi', 'Fear', 'Gimmick']

    # Ambil semua ads ACTIVE dari semua akun
    category_counts = {}
    for sn, acc_info in ACCOUNTS.items():
        acc_id = acc_info['id']
        try:
            url = (f"https://graph.facebook.com/v26.0/{acc_id}/ads"
                   f"?fields=id,name,status&limit=250"
                   f"&filtering=%5B%7B%22field%22%3A%22ad.effective_status%22%2C%22operator%22%3A%22IN%22%2C%22value%22%3A%5B%22ACTIVE%22%5D%7D%5D"
                   f"&access_token={token}")
            r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url],
                               capture_output=True, text=True, timeout=20)
            data = _json.loads(r.stdout)
            ads = data.get('data')
            if not isinstance(ads, list):
                continue
            for ad in ads:
                name = ad.get('name', '')
                # Ambil kategori: kata pertama sebelum " - " atau " -" atau "- "
                m = _re.match(r'^([^-]+?)\s*-', name.strip())
                if m:
                    category = m.group(1).strip()
                else:
                    # Fallback: ambil kata pertama
                    category = name.strip().split()[0] if name.strip() else 'Lainnya'
                # Normalisasi variasi
                cat_normalized = None
                for cc in CONTENT_CATEGORIES:
                    if category.lower().startswith(cc.lower()):
                        cat_normalized = cc
                        break
                if not cat_normalized:
                    cat_normalized = 'Lainnya'
                category_counts[cat_normalized] = category_counts.get(cat_normalized, 0) + 1
        except:
            pass

    # Sort by count descending, ambil top 10
    sorted_cats = sorted(category_counts.items(), key=lambda x: -x[1])
    categories = [{'name': k, 'count': v} for k, v in sorted_cats[:10]]
    total = sum(c['count'] for c in categories)

    return jsonify({'categories': categories, 'total': total})


# ===== CRON API ROUTES =====
CRON_DATA_FILE = os.path.join(os.path.dirname(__file__), 'cron_data.json')

def _load_cron_data():
    """Load cron data from synced JSON file."""
    try:
        with open(CRON_DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'jobs': [], 'total': 0, 'active': 0, '_sync_error': 'File cron_data.json belum tersedia. Tunggu sinkronisasi pertama.'}

@app.route('/api/cron/list')
@admin_required
def api_cron_list():
    """List all cron jobs from synced data."""
    try:
        data = _load_cron_data()
        jobs = []
        for j in data.get('jobs', []):
            jobs.append({
                'id': j.get('id', ''),
                'name': j.get('name', ''),
                'schedule': j.get('schedule', ''),
                'status': j.get('status', 'unknown'),
                'repeat': j.get('repeat', ''),
                'next_run': j.get('next_run', ''),
                'last_run': j.get('last_run', ''),
                'last_status': j.get('last_status', ''),
                'script': j.get('script', ''),
                'deliver': j.get('deliver', ''),
                'mode': j.get('mode', ''),
                'log_preview': j.get('log_preview', '')
            })
        return jsonify(jobs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cron/log')
@admin_required
def api_cron_log():
    """Get log for a specific cron job."""
    cron_id = request.args.get('id', '')
    if not cron_id:
        return jsonify({'log': 'ID cron tidak diberikan'})
    try:
        data = _load_cron_data()
        for j in data.get('jobs', []):
            if j.get('id') == cron_id:
                return jsonify({'log': j.get('log_full', j.get('log_preview', 'Tidak ada log'))})
        return jsonify({'log': f'Cron {cron_id} tidak ditemukan'})
    except Exception as e:
        return jsonify({'log': f'Error: {str(e)}'})

@app.route('/api/cron/create', methods=['POST'])
@admin_required
def api_cron_create():
    """Create a new cron job."""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        schedule = data.get('schedule', '').strip()
        prompt = data.get('prompt', '').strip()

        if not all([name, schedule, prompt]):
            return jsonify({'status': 'error', 'message': 'Semua field harus diisi'}), 400

        return jsonify({
            'status': 'error',
            'message': 'Pembuatan cron via dashboard dinonaktifkan. Jalankan via Hermes CLI langsung.'
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cron/toggle', methods=['POST'])
@admin_required
def api_cron_toggle():
    """Toggle cron job pause/resume via Hermes Bridge langsung."""
    try:
        data = request.get_json()
        cron_id = data.get('cron_id', '').strip()
        action = data.get('action', '').strip()

        if not cron_id or action not in ('pause', 'resume'):
            return jsonify({'status': 'error', 'message': 'Parameter cron_id dan action (pause/resume) required'}), 400

        # Call bridge langsung
        bridge_resp = _bridge_call('/api/cron/toggle', method='POST', data={
            'cron_id': cron_id, 'action': action
        })

        if bridge_resp.get('success'):
            return jsonify({'status': 'ok', 'message': f'{action} untuk {cron_id} berhasil real-time', 'bridge': bridge_resp})
        else:
            return jsonify({'status': 'error', 'message': bridge_resp.get('error', 'Bridge call failed')}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500



@app.route('/api/dailyleveling/data')
@admin_required
def api_dailyleveling_data():
    """List semua campaign + status DL + state base budget (via bridge)."""
    try:
        campaigns = get_campaigns(limit=5000)
    except Exception:
        campaigns = []
    # Ambil snapshot terbaru per campaign (rows urut date DESC)
    latest = {}
    for c in campaigns:
        cid = c.get('campaign_id')
        if cid and cid not in latest:
            latest[cid] = c
    # State dari bridge
    state = {}
    st = {}
    try:
        st = _bridge_call('/api/dailyleveling/state', method='GET')
        if st.get('success'):
            state = st.get('campaigns', {}) or {}
    except Exception:
        pass
    import re as _re
    def _level_of(name):
        m = _re.search(r'lv(\d+)', name or '', _re.IGNORECASE)
        return int(m.group(1)) if m else 1
    rows = []
    for cid, c in latest.items():
        # Hanya tampilkan campaign ACTIVE dengan spend > 0
        if c.get('status') != 'ACTIVE':
            continue
        if float(c.get('spend') or 0) <= 0:
            continue
        name = c.get('campaign_name', '') or ''
        info = state.get(cid, {})
        is_dl = ('DL - ' in name) or bool(info)
        rows.append({
            'campaign_id': cid,
            'name': name,
            'account_id': c.get('account_id', ''),
            'account_name': c.get('account_name') or c.get('short_name', ''),
            'status': c.get('status', ''),
            'spend': c.get('spend', 0),
            'results': c.get('results', 0),
            'is_dl': is_dl,
            'base_budget': info.get('base_budget'),
            'level': info.get('current_level') or _level_of(name),
            'initial_name': info.get('initial_name', ''),
            'group': info.get('group', ''),
        })
    rows.sort(key=lambda r: (not r['is_dl'], r['account_name'] or '', r['name'] or ''))
    # Tampilkan hanya kampanye ACTIVE dengan spending > 0
    rows = [r for r in rows if r['status'] == 'ACTIVE' and (r['spend'] or 0) > 0]
    return jsonify({
        'campaigns': rows,
        'total': len(rows),
        'dl_count': sum(1 for r in rows if r['is_dl']),
        'groups': st.get('groups') or {},
    })


@app.route('/api/dailyleveling/add', methods=['POST'])
@admin_required
def api_dailyleveling_add():
    """Masukkan campaign ke dailyleveling (rename ke DL - nama (lead/atc 0) (lv1))."""
    data = request.get_json(force=True) or {}
    campaign_id = str(data.get('campaign_id', '')).strip()
    if not campaign_id:
        return jsonify({'status': 'error', 'message': 'campaign_id required'}), 400
    res = _bridge_call('/api/dailyleveling/add', method='POST', data={'campaign_id': campaign_id})
    if res.get('status') == 'ok':
        return jsonify({'status': 'ok', 'message': res.get('message', 'OK'), 'new_name': res.get('new_name', '')})
    return jsonify({'status': 'error', 'message': res.get('message') or res.get('error', 'Bridge gagal')}), 400


@app.route('/api/dailyleveling/remove', methods=['POST'])
@admin_required
def api_dailyleveling_remove():
    """Batalkan campaign dari dailyleveling (balikin nama asli)."""
    data = request.get_json(force=True) or {}
    campaign_id = str(data.get('campaign_id', '')).strip()
    if not campaign_id:
        return jsonify({'status': 'error', 'message': 'campaign_id required'}), 400
    res = _bridge_call('/api/dailyleveling/remove', method='POST', data={'campaign_id': campaign_id})
    if res.get('status') == 'ok':
        return jsonify({'status': 'ok', 'message': res.get('message', 'OK'), 'new_name': res.get('new_name', '')})
    return jsonify({'status': 'error', 'message': res.get('message') or res.get('error', 'Bridge gagal')}), 400


@app.route('/api/dailyleveling/base-budget', methods=['POST'])
@admin_required
def api_dailyleveling_base_budget():
    """Set base budget campaign DL (via bridge)."""
    data = request.get_json(force=True) or {}
    campaign_id = str(data.get('campaign_id', '')).strip()
    try:
        base_budget = int(float(data.get('base_budget', 0)))
    except (TypeError, ValueError):
        base_budget = 0
    if not campaign_id or base_budget <= 0:
        return jsonify({'status': 'error', 'message': 'campaign_id + base_budget required'}), 400
    res = _bridge_call('/api/dailyleveling/base-budget', method='POST', data={
        'campaign_id': campaign_id, 'base_budget': base_budget
    })
    if res.get('status') == 'ok':
        return jsonify({'status': 'ok', 'message': res.get('message', 'OK')})
    return jsonify({'status': 'error', 'message': res.get('message') or res.get('error', 'Bridge gagal')}), 400


# ===== CHAT CEPAT (DeepSeek langsung — widget nggak berubah, polling tetap jalan) =====
_CHAT_REPLIES = {}  # job_id -> reply (in-memory)

def _chat_perf_context():
    """Ringkasan performa per akun: hari ini + 7 hari + 30 hari, buat konteks chat."""
    def _agg_summary(date_from, date_to, label):
        try:
            rows = get_daily_summary(date_from=date_from, date_to=date_to)
            if not rows:
                return f"{label}: belum ada data."
            agg = {}
            for r in rows:
                sn = r.get('short_name') or '?'
                a = agg.setdefault(sn, {'spend': 0, 'results': 0, 'active': 0})
                a['spend'] += float(r.get('total_spend') or 0)
                a['results'] += int(r.get('total_results') or 0)
                a['active'] += int(r.get('active_campaigns') or 0)
            lines = [f"{label}:"]
            for sn, a in agg.items():
                cpr = a['spend'] / a['results'] if a['results'] else 0
                lines.append(
                    f"- {sn}: spend Rp{int(a['spend']):,}, hasil {a['results']}, "
                    f"CPR Rp{int(cpr):,}, campaign aktif {a['active']}"
                )
            return '\n'.join(lines)
        except Exception:
            return f"{label}: data tidak tersedia."

    def _top_campaigns(date_from, date_to, n=5):
        try:
            rows = get_campaigns(date_from=date_from, date_to=date_to, limit=1500)
            agg = {}
            for c in rows:
                cid = c.get('campaign_id')
                if not cid:
                    continue
                a = agg.setdefault(cid, {'name': c.get('campaign_name') or '?', 'acct': c.get('short_name') or '', 'spend': 0, 'results': 0})
                a['spend'] += float(c.get('spend') or 0)
                a['results'] += int(c.get('results') or 0)
            top = sorted(agg.values(), key=lambda x: -x['spend'])[:n]
            if not top:
                return 'Top campaign 7 hari: belum ada data.'
            lines = ['Top campaign 7 hari (by spend):']
            for t in top:
                cpr = t['spend'] / t['results'] if t['results'] else 0
                lines.append(
                    f"- {t['name']} ({t['acct']}): spend Rp{int(t['spend']):,}, "
                    f"hasil {t['results']}, CPR Rp{int(cpr):,}"
                )
            return '\n'.join(lines)
        except Exception:
            return 'Top campaign 7 hari: data tidak tersedia.'

    try:
        today = date.today().isoformat()
        d7 = (date.today() - timedelta(days=6)).isoformat()
        d30 = (date.today() - timedelta(days=29)).isoformat()
        parts = [
            _agg_summary(today, today, 'HARI INI'),
            _agg_summary(d7, today, '7 HARI TERAKHIR'),
            _agg_summary(d30, today, '30 HARI TERAKHIR'),
            _top_campaigns(d7, today, 5),
        ]
        return '\n\n'.join(parts)
    except Exception:
        return 'Data performa belum tersedia.'


_KOWALSKI_SYSTEM = """Kamu adalah Kowalski, partner operasional Gilang di Kowalski Ads Center (dashboard Meta Ads 4 akun: G1/G2 LEAD, G3/G4 ATC).
Kamu membantu analisa campaign, budget, dan performa iklan Generos. Bahasa Indonesia informal, langsung, singkat, pakai angka dari data yang dikasih.
Jangan mengarang angka — kalau nggak ada di data, bilang nggak tau / minta lihat halaman analisis."""


@app.route('/api/chat', methods=['POST'])
@admin_required
def api_chat():
    """Chat widget — DeepSeek langsung (cepat, 2-5 detik). Reply disimpan sementara, widget polling seperti biasa."""
    data = request.get_json(force=True) or {}
    message = str(data.get('message', '')).strip()
    if not message:
        return jsonify({'status': 'error', 'message': 'message required'}), 400
    if not DEEPSEEK_API_KEY:
        return jsonify({'status': 'error', 'message': 'DeepSeek API key belum di-set di config.json'}), 500
    history = data.get('history') or []
    system = _KOWALSKI_SYSTEM + '\n\n=== DATA PERFORMA HARI INI ===\n' + _chat_perf_context()
    messages = [{'role': 'system', 'content': system}]
    for h in history[-12:]:
        if h.get('role') in ('user', 'assistant') and h.get('content'):
            messages.append({'role': h['role'], 'content': str(h['content'])[:2000]})
    messages.append({'role': 'user', 'content': message[:4000]})
    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': messages,
        'max_tokens': 800,
        'temperature': 0.6,
    }).encode()
    reply = None
    last_err = None
    # Retry 3x dengan jeda — DeepSeek kadang 503/429 sesaat pas overload
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                'https://api.deepseek.com/chat/completions',
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + DEEPSEEK_API_KEY,
                }
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
            reply = (out.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
            if reply:
                break
            last_err = 'DeepSeek balas kosong'
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
    if reply is None:
        return jsonify({'status': 'error', 'message': 'DeepSeek error: ' + str(last_err)}), 502
    import uuid
    job_id = 'fast_' + uuid.uuid4().hex[:12]
    _CHAT_REPLIES[job_id] = reply
    # Bersihin reply lama (TTL sederhana: max 200)
    if len(_CHAT_REPLIES) > 200:
        for k in list(_CHAT_REPLIES)[:100]:
            _CHAT_REPLIES.pop(k, None)
    return jsonify({'status': 'ok', 'message_id': job_id})


@app.route('/api/chat/result/<job_id>')
@admin_required
def api_chat_result(job_id):
    """Polling hasil chat — langsung dapat reply kalau udah jadi (chat cepat)."""
    if job_id in _CHAT_REPLIES:
        return jsonify({'status': 'done', 'reply': _CHAT_REPLIES[job_id]})
    return jsonify({'status': 'pending', 'reply': ''})


def _send_cron_notif(action, cron_id, cron_name, extra_text=""):
    """Send cron change notification to Telegram group topic 1958."""
    action_label = {
        'pause': '⏸️ Cron di-PAUSE',
        'resume': '▶️ Cron di-RESUME',
        'create': '➕ Cron DIBUAT',
        'edit': '✏️ Cron di-EDIT',
        'delete': '🗑️ Cron di-HAPUS',
    }
    emojis = {
        'pause': '⏸️',
        'resume': '▶️',
        'create': '➕',
        'edit': '✏️',
        'delete': '🗑️',
    }

    text = (
        f"{emojis.get(action, '🔄')} Perubahan Cron\n"
        f"{'━'*30}\n"
        f"📛 {cron_name}\n"
        f"🆔 {cron_id}\n"
        f"📌 Aksi: {action_label.get(action, action)}\n"
        f"{'━'*30}\n"
        f"{extra_text}"
    )

    import subprocess, urllib.parse
    chat_id = '-1003990111670'
    topic_id = '1958'
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'message_thread_id': topic_id,
        'text': text,
        'parse_mode': 'HTML',
    })
    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
            capture_output=True, text=True, timeout=15
        )
        ok = '{"ok":true' in r.stdout
        print(f"[CRON-NOTIF] {action} {cron_id}: {'OK' if ok else 'FAIL'}", flush=True)
        return ok
    except Exception as e:
        print(f"[CRON-NOTIF] Error: {e}", flush=True)
        return False


@app.route('/api/cron/rename', methods=['POST'])
@admin_required
def api_cron_rename():
    """Rename cron job via Hermes Bridge langsung."""
    try:
        data = request.get_json()
        cron_id = data.get('job_id', '').strip() or data.get('cron_id', '').strip()
        new_name = data.get('new_name', '').strip()

        if not cron_id or not new_name:
            return jsonify({'status': 'error', 'message': 'Parameter job_id dan new_name required'}), 400

        # Call bridge rename endpoint
        bridge_resp = _bridge_call('/api/cron/rename', method='POST', data={
            'cron_id': cron_id, 'new_name': new_name
        })

        if bridge_resp.get('success'):
            _send_cron_notif('edit', cron_id, new_name, '✅ Cron beneran diubah (real-time)')
            return jsonify({'status': 'ok', 'message': f'Nama cron {cron_id} berhasil diubah jadi "{new_name}"', 'bridge': bridge_resp})
        else:
            return jsonify({'status': 'error', 'message': bridge_resp.get('error', 'Bridge rename failed')}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cron/sync-local', methods=['POST'])
@admin_required
def api_cron_sync_local():
    """Sync cron_data.json from Hermes Bridge (live data)."""
    try:
        bridge_resp = _bridge_call('/api/cron/sync')
        if not bridge_resp.get('jobs') and not bridge_resp.get('success'):
            return jsonify({'status': 'error', 'message': 'Bridge sync failed', 'detail': bridge_resp}), 500

        # Save to cron_data.json
        sync_path = os.path.join(os.path.dirname(__file__), 'cron_data.json')
        bridge_resp['_last_sync'] = datetime.now().isoformat()
        with open(sync_path, 'w', encoding='utf-8') as f:
            json.dump(bridge_resp, f, indent=2, ensure_ascii=True)

        return jsonify({'status': 'ok', 'message': f'{len(bridge_resp.get("jobs", []))} cron tersinkronisasi live', 'total': len(bridge_resp.get("jobs", []))})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cron/delete', methods=['POST'])
@admin_required
def api_cron_delete():
    """Delete cron job via Hermes Bridge langsung."""
    try:
        data = request.get_json()
        cron_id = data.get('cron_id', '').strip() or data.get('job_id', '').strip()

        if not cron_id:
            return jsonify({'status': 'error', 'message': 'Parameter cron_id required'}), 400

        bridge_resp = _bridge_call('/api/cron/delete', method='POST', data={
            'cron_id': cron_id
        })

        if bridge_resp.get('success') and 'Failed to remove' not in bridge_resp.get('output', ''):
            return jsonify({'status': 'ok', 'message': f'Cron {cron_id} berhasil dihapus', 'bridge': bridge_resp})
        else:
            err_msg = bridge_resp.get('output', bridge_resp.get('error', 'Bridge delete failed'))
            return jsonify({'status': 'error', 'message': err_msg}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/cron/test-notify', methods=['POST'])
@admin_required
def api_cron_test_notify():
    """TEST endpoint — kirim notif Telegram tanpa ubah cron beneran."""
    try:
        data = request.get_json()
        cron_id = data.get('cron_id', '').strip()
        cron_name = data.get('cron_name', '').strip() or 'Tanpa nama'
        action = data.get('action', '').strip()

        if action not in ('pause', 'resume', 'create', 'edit', 'delete'):
            return jsonify({'status': 'error', 'message': 'Action harus pause/resume/create/edit/delete'}), 400

        # Build message
        action_label = {
            'pause': '⏸️ Cron di-PAUSE',
            'resume': '▶️ Cron di-RESUME',
            'create': '➕ Cron DIBUAT',
            'edit': '✏️ Cron di-EDIT',
            'delete': '🗑️ Cron di-HAPUS',
        }
        emojis = {
            'pause': '⏸️',
            'resume': '▶️',
            'create': '➕',
            'edit': '✏️',
            'delete': '🗑️',
        }

        text = (
            f"{emojis.get(action, '🔄')} [TEST] Perubahan Cron\n"
            f"{'━'*30}\n"
            f"📛 {cron_name}\n"
            f"🆔 {cron_id}\n"
            f"📌 Aksi: {action_label.get(action, action)}\n"
            f"{'━'*30}\n"
            f"⚠️ Ini TEST — cron beneran TIDAK diubah"
        )

        # Send to group topic 1958
        import subprocess, urllib.parse
        chat_id = '-1003990111670'
        topic_id = '1958'
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            'chat_id': chat_id,
            'message_thread_id': topic_id,
            'text': text,
            'parse_mode': 'HTML',
        })

        try:
            r = subprocess.run(
                ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
                capture_output=True, text=True, timeout=15
            )
            sent_ok = '{"ok":true' in r.stdout
            print(f"[TEST-NOTIFY] TG response: {r.stdout[:200]}", flush=True)
        except Exception as e:
            print(f"[TEST-NOTIFY] TG curl error: {e}", flush=True)
            sent_ok = False

        return jsonify({
            'status': 'ok',
            'message': f'Notif {action} untuk {cron_name} dikirim',
            'telegram_ok': sent_ok
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ===== CRON PAGES =====
@app.route('/cron/list')
@admin_required
def cron_list():
    return redirect('/cron-management')

@app.route('/cron/log')
@admin_required
def cron_log():
    return redirect('/cron-management')

@app.route('/cron/remake')
@admin_required
def cron_remake():
    return redirect('/cron-management')

@app.route('/cron/detail')
@admin_required
def cron_detail():
    return redirect('/cron-management')

@app.route('/cron-management')
@admin_required
def cron_management():
    return render_template('demo_cron.html')


@app.route('/dailyleveling')
@admin_required
def dailyleveling_page():
    """Halaman Daily Leveling — kelola akun & base budget (struktur awal)."""
    return render_template('dailyleveling.html', active_page='dailyleveling')


def _get_cron_narrative(name):
    """Generate narrative/alur for a cron job based on its name."""
    n = name or ''
    if 'Hourly' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengirim laporan performa semua akun Meta Ads (G1-G4) setiap jam pada menit ke-15 WIB ke Telegram.

2. CARA KERJA
- Script "hourly_check.py" berjalan otomatis tiap jam (15 * * * *)
- Mengambil data dari Meta Ads API untuk 4 akun: G1 (LEAD), G2 (LEAD), G3 (ATC), G4 (ATC)
- Menampilkan total spend, jumlah hasil, dan CPR per akun
- Data dikirim ke Telegram dalam format tabel sederhana

3. OUTPUT
Pesan Telegram berisi tabel: Akun, Spend, Hasil, CPR, Status per akun + total.

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'LLM Report 09' in n and '13' not in n and '16' not in n and '20' not in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengirim laporan analisa performa harian pertama jam 09:00 WIB ke Telegram.

2. CARA KERJA
- Script "prefetch_report.py" mengambil data performa Meta Ads
- Data dikirim ke LLM untuk dianalisis
- LLM menghasilkan laporan dengan insight: akun BAGUS/WASPADA, perbandingan kemarin, rekomendasi
- Laporan dikirim ke Telegram group

3. PERBEDAAN DENGAN HOURLY CHECK
- Hourly check: data mentah (tabel angka)
- LLM Report: analisis + insight + rekomendasi

4. DURASI
Terjadwal 31 kali (repeat: 31/999)"""
    elif 'LLM Report 13' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengirim laporan analisa performa harian jam 13:00 WIB.

2. CARA KERJA
Sama seperti LLM Report 09:00, tapi berjalan jam 13:00 WIB (schedule: 0 14 * * *).

3. FITUR
- Analisis performa tiap akun
- Perbandingan dengan hari sebelumnya
- Status BAGUS/WASPADA per akun
- Rekomendasi tindakan

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'LLM Report 16' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengirim laporan analisa performa harian jam 16:00 WIB (schedule: 0 17 * * *).

2. CARA KERJA
Sama seperti LLM Report lainnya, menggunakan prefetch_report.py + LLM analysis.

3. OUTPUT
Laporan detail per akun dengan perbandingan data dan rekomendasi.

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'LLM Report 20' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengirim laporan analisa penutup hari jam 20:00 WIB (schedule: 0 21 * * *).

2. FITUR KHUSUS
- Laporan terakhir hari ini
- Ringkasan performa seharian penuh
- Evaluasi apakah target tercapai

3. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'Creative' in n or 'Remake' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Secara otomatis memilih 2 creative dengan biaya terendah, lalu membuat ulang (remake) creative tersebut.

2. CARA KERJA
- Script "win_creative_fetcher.py" berjalan jam 11:00 WIB setiap hari
- Mencari campaign aktif dengan performa terbaik (CPR terendah)
- Memilih 2 creative termurah dari campaign tersebut
- Melakukan remake: membuat creative baru berdasarkan creative pemenang
- Creative baru otomatis dibuat di Meta Ads

3. KRITERIA PEMILIHAN
- Sortir semua campaign berdasarkan CPR (termurah)
- Pilih 2 campaign teratas
- Ambil iklan aktif dengan biaya terendah dari tiap campaign
- Duplikasi dengan variasi baru

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'dailyleveling' in n and 'G1' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengatur budget campaign daily leveling (DL) di akun G1 secara otomatis setiap 2 jam.

2. CARA KERJA
- Script "dailyleveling_g1_scale.py" berjalan tiap 2 jam
- Mengevaluasi performa semua campaign DL G1
- Aturan scaling: performa bagus (CPR rendah) -> NAIK level, jelek -> TURUN level, standar -> TETAP
- Setiap level punya budget tetap (lv1=RpX, lv2=RpY, dst)

3. CONTOH OUTPUT
"DL - F1 - 12 06 26 - Testing Konten... -> NAIK lv6->lv7 | Rp896.000"

4. DURASI
Berjalan terus menerus tiap 2 jam (repeat: infinite)"""
    elif 'dailyleveling' in n and 'G2' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengatur budget campaign daily leveling (DL) di akun G2 secara otomatis setiap 2 jam.

2. CARA KERJA
Sama seperti dailyleveling G1, tapi untuk akun G2 (LEAD).
Script: dailyleveling_g2_scale.py

3. ATURAN
- Performa bagus -> NAIK level
- Performa jelek -> TURUN level
- Performa standar -> TETAP

4. DURASI
Berjalan terus menerus tiap 2 jam (repeat: infinite)"""
    elif 'dailyleveling' in n and 'G3' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengatur budget campaign daily leveling (DL) di akun G3 (ATC) secara otomatis setiap 2 jam.

2. CARA KERJA
Script: dailyleveling_g3_scale.py
Mengelola campaign ATC (add to cart) dengan aturan scaling yang sama.

3. DURASI
Berjalan terus menerus tiap 2 jam (repeat: infinite)"""
    elif 'Dailyleveling' in n and 'G4' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengatur budget campaign daily leveling (DL) di akun G4 khusus campaign F2 Dadaida.

2. CARA KERJA
Script: dailyleveling_g4_scale.py
Fokus pada satu campaign: F2 Dadaida di akun G4 (ATC).
Aturan: NAIK/TURUN/TETAP berdasarkan performa.

3. DURASI
Berjalan terus menerus tiap 2 jam (repeat: infinite)"""
    elif 'Budget' in n or 'Spreadsheet' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Merekapitulasi total spend harian semua akun ke spreadsheet Google setiap jam 08:00 WIB.

2. CARA KERJA
- Script "daily_budget_report.py" berjalan jam 08:00 WIB
- Mengakumulasi total spend semua akun (G1-G4) dari hari sebelumnya
- Mencatat ke spreadsheet Google Drive
- Format: Tanggal | Total Spend

3. OUTPUT
"OK: Minggu, 12 Juli 2026 -> Rp16.383.253 (row 287)"

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'TST Evaluator' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mengevaluasi campaign testing (TST) di akun G1 setiap jam 09:30 WIB dan otomatis mem-pause yang jelek.

2. CARA KERJA
- Script "tst_evaluator_g1.py" berjalan jam 09:30 WIB
- Mengecek semua campaign TST yang aktif
- Aturan evaluasi: Day 1-2 CPR < Rp150rb -> hold, Day 2 > Rp150rb -> PAUSE, Day 3 rata2 > Rp150rb -> PAUSE
- Campaign yang di-pause tetap tersimpan, tidak dihapus

3. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'TST Creator' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Membuat 1 campaign testing (TST) baru di akun G1 secara otomatis setiap jam 10:00 WIB.

2. CARA KERJA
- Script "tst_creator_g1.py" berjalan jam 10:00 WIB
- Memilih konten video yang belum pernah digunakan dari library
- Membuat campaign TST baru dengan: CBO Rp250.000, Broad, P 25-50, CTA Get_Offer, Status PAUSED
- Maksimal 4 campaign TST aktif dalam satu waktu

3. PEMILIHAN KONTEN
- Skip video >50MB
- Skip konten yang sudah dipakai
- Pilih konten baru yang belum di-test

4. DURASI
Berjalan terus menerus (repeat: infinite)"""
    elif 'sync' in n:
        return """ALUR PEMBUATAN CRON INI:

1. TUJUAN
Mensinkronkan data cron job dari server Hermes lokal ke dashboard cPanel setiap 5 menit.

2. CARA KERJA
- Script "cron_sync.py" berjalan tiap 5 menit
- Mengambil daftar cron job dari Hermes CLI (hermes cron list)
- Mengambil log output dari direktori output cron
- Menggabungkan semua data ke file JSON
- Upload file JSON ke cPanel via FTP
- Dashboard membaca file JSON untuk menampilkan data

3. DATA YANG DISINKRON
- Nama cron, schedule, status
- Last run, next run
- Log preview dan log full
- Total cron aktif

4. DURASI
Berjalan terus menerus tiap 5 menit (repeat: infinite)"""
    else:
        return "Cron job ini adalah bagian dari sistem otomatisasi Hermes. Detail selengkapnya dapat dilihat di dokumentasi sistem."


@app.route('/api/cron/download/<cron_id>')
@admin_required
def api_cron_download(cron_id):
    """Download cron detail as DOCX or PDF."""
    import io
    from datetime import datetime as dt

    data = _load_cron_data()
    job = None
    for j in data.get('jobs', []):
        if j.get('id') == cron_id:
            job = j
            break

    if not job:
        return jsonify({'error': 'Cron tidak ditemukan'}), 404

    fmt = request.args.get('format', 'docx')
    name = job.get('name', 'Unnamed')
    schedule = job.get('schedule', '-')
    status = job.get('status', '-')
    last_run = job.get('last_run', '-')
    last_status = job.get('last_status', '-')
    script = job.get('script', '-')
    mode = job.get('mode', '-')
    deliver = job.get('deliver', '-')
    log_preview = (job.get('log_preview') or '')[:2000]

    # Generate narrative
    narrative = _get_cron_narrative(name)

    def plaintext_download(name, schedule, status, last_run, last_status, script, mode, deliver, log_preview, narrative, ext):
        text = f"""========================================
DETAIL CRON
========================================
Nama: {name}
Schedule: {schedule}
Status: {status}

--- ALUR PEMBUATAN ---
{narrative}
---
Generated: {dt.now().strftime('%Y-%m-%d %H:%M')}
"""
        safe_name = name.replace('/', '_').replace(' ', '_')[:50]
        return Response(
            text,
            mimetype='text/plain',
            headers={'Content-Disposition': f'attachment; filename=cron_{safe_name}.{ext}'}
        )

    if fmt == 'docx':
        try:
            from docx import Document
        except ImportError:
            # Fallback: generate plain text
            return plaintext_download(name, schedule, status, last_run, last_status, script, mode, deliver, log_preview, narrative, 'txt')

        doc = Document()

        # Title
        doc.add_heading(f'Detail Cron: {name}', 0)

        # Info table
        table = doc.add_table(rows=8, cols=2, style='Light Shading Accent 1')
        cells = [
            ('Nama', name),
            ('Schedule', schedule),
            ('Status', status),
            ('Script', script),
            ('Mode', mode),
            ('Last Run', last_run),
            ('Last Status', last_status),
            ('Deliver', deliver),
        ]
        for i, (label, value) in enumerate(cells):
            table.rows[i].cells[0].text = label
            table.rows[i].cells[1].text = str(value)

        doc.add_paragraph()
        doc.add_heading('Log Preview', 1)
        doc.add_paragraph(log_preview[:1000])

        doc.add_paragraph()
        p = doc.add_paragraph(f'Generated: {dt.now().strftime("%Y-%m-%d %H:%M")}')
        p.style = doc.styles['Normal']

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        safe_name = name.replace('/', '_').replace(' ', '_')[:50]
        return Response(
            buf.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            headers={'Content-Disposition': f'attachment; filename=cron_{safe_name}.docx'}
        )

    elif fmt == 'pdf':
        try:
            from fpdf import FPDF
        except ImportError:
            return plaintext_download(name, schedule, status, last_run, last_status, script, mode, deliver, log_preview, narrative, 'txt')

        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)

        # Title
        pdf.set_font('Helvetica', 'B', 16)
        pdf.cell(0, 10, f'Detail Cron: {name}', new_x='LMARGIN', new_y='NEXT', align='C')
        pdf.ln(5)

        # Info
        pdf.set_font('Helvetica', '', 10)
        items = [
            ('Nama', name),
            ('Schedule', schedule),
            ('Status', status),
            ('Script', script),
            ('Mode', mode),
            ('Last Run', last_run),
            ('Last Status', last_status),
            ('Deliver', deliver),
        ]
        for label, value in items:
            pdf.set_font('Helvetica', 'B', 10)
            pdf.cell(40, 7, label + ':')
            pdf.set_font('Helvetica', '', 10)
            pdf.multi_cell(0, 7, str(value))
            pdf.ln(1)

        pdf.ln(5)
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(0, 10, 'Log Preview', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 8)
        pdf.multi_cell(0, 4, log_preview[:1000])

        pdf.ln(5)
        pdf.set_font('Helvetica', 'I', 8)
        pdf.cell(0, 5, f'Generated: {dt.now().strftime("%Y-%m-%d %H:%M")}', new_x='LMARGIN', new_y='NEXT')

        buf = io.BytesIO()
        pdf.output(buf)
        buf.seek(0)

        safe_name = name.replace('/', '_').replace(' ', '_')[:50]
        return Response(
            buf.getvalue(),
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename=cron_{safe_name}.pdf'}
        )

    return jsonify({'error': 'Format tidak didukung'}), 400


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, message='Halaman tidak ditemukan'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500, message='Kesalahan server'), 500


@app.route('/analisis/kampanye')
@admin_required
def analisis_kampanye():
    return render_template('analisis_kampanye.html')


@app.route('/analisis/adset')
@admin_required
def analisis_adset():
    return render_template('analisis_adset.html')


@app.route('/adset-generator')
@admin_required
def adset_generator():
    return render_template('adset_generator.html', products=_safe_products())


@app.route('/api/adset-generator/ads-list')
@admin_required
def api_adset_generator_ads_list():
    """Fetch ads with performance data for Adset Generator."""
    try:
        account_id = request.args.get('account_id', '').strip()
        days = request.args.get('days', 7, type=int)
        token = _get_meta_token()
        if not token:
            return jsonify({'error': 'Meta token tidak tersedia'}), 400

        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        until = date.today().isoformat()

        from config import ACCOUNTS

        # Determine which account IDs to fetch
        target_accounts = []
        if account_id:
            # Single account
            sn = ''
            for s, a in ACCOUNTS.items():
                if a['id'] == account_id:
                    sn = s
                    break
            target_accounts = [(account_id, sn)]
        else:
            # All accounts
            target_accounts = [(a['id'], s) for s, a in ACCOUNTS.items()]

        import requests as _req
        result = []

        for act_id, sn in target_accounts:
            # Determine action field
            event_type = 'add_to_cart'
            for s, a in ACCOUNTS.items():
                if a['id'] == act_id:
                    event_type = a.get('event', 'add_to_cart')
                    break
            action_field = 'add_to_cart' if event_type == 'add_to_cart' else 'lead'

            url = (
                f"https://graph.facebook.com/v26.0/{act_id}/ads"
                f"?fields=id,name,status,adset_id,campaign_id,campaign{{name}},"
                f"creative{{id,title,body,thumbnail_url,image_url,video_id}},"
                f"insights.time_range(%7B%22since%22%3A%22{since}%22%2C%22until%22%3A%22{until}%22%7D)%7Bspend,actions,ctr,impressions%7D"
                f"&effective_status=%5B%22ACTIVE%22%2C%22PAUSED%22%5D"
                f"&limit=200"
            )
            try:
                resp = _req.get(url, params={'access_token': token}, timeout=25)
                data = resp.json()
                if 'error' in data:
                    continue

                for ad in data.get('data', []):
                    ins_list = ad.get('insights', {}).get('data', [])
                    ins = ins_list[0] if ins_list else {}
                    spend = float(ins.get('spend', 0))
                    if spend <= 0:
                        continue

                    results = 0
                    for act in ins.get('actions', []):
                        if act.get('action_type') == action_field:
                            results = int(act.get('value', 0))
                            break
                    cpr = round(spend / results, 2) if results > 0 else 0
                    ctr = float(ins.get('ctr', 0))
                    campaign_info = ad.get('campaign', {})

                    result.append({
                        'ad_id': ad['id'],
                        'adset_id': ad.get('adset_id', ''),
                        'ad_name': ad.get('name', ''),
                        'campaign_name': campaign_info.get('name', '') if campaign_info else '',
                        'short_name': sn,
                        'spend': spend,
                        'results': results,
                        'cpr': cpr,
                        'ctr': ctr,
                        'status': ad.get('status', ''),
                        'interests': [],
                    })
            except:
                continue

        result.sort(key=lambda x: (-x['results'], x['cpr']))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adset-generator/creative')
@admin_required
def api_adset_generator_creative():
    """Fetch creative details for a given ad."""
    try:
        ad_id = request.args.get('ad_id', '').strip()
        if not ad_id:
            return jsonify({'status': 'error', 'message': 'ad_id required'}), 400

        token = _get_meta_token()
        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'})

        import requests as _req
        url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=creative{{id,title,body,thumbnail_url,image_url,video_id,name,object_story_spec}}&access_token={token}"
        resp = _req.get(url, timeout=15)
        data = resp.json()
        if 'error' in data:
            return jsonify({'status': 'error', 'message': data['error'].get('message', '')})

        cr = data.get('creative', {})
        oss = cr.get('object_story_spec', {})
        link_data = oss.get('link_data', {})
        video_data = oss.get('video_data', {})

        return jsonify({
            'status': 'success',
            'data': {
                'ad_name': data.get('name', ''),
                'body': link_data.get('message', '') or video_data.get('message', '') or cr.get('body', ''),
                'headline': link_data.get('name', '') or video_data.get('title', '') or cr.get('title', ''),
                'thumbnail_url': cr.get('thumbnail_url', '') or cr.get('image_url', ''),
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/adset-generator/generate', methods=['POST'])
@admin_required
def api_adset_generator_generate():
    """Generate interest suggestions — by reference ad or custom prompt."""
    try:
        body = request.get_json(force=True) or {}
        mode = body.get('mode', 'referensi')

        if mode == 'referensi':
            ad_id = body.get('ad_id', '')
            adset_id = body.get('adset_id', '')
            if not ad_id:
                return jsonify({'error': 'ad_id required'}), 400

            # Fetch ad creative to get headline + body
            token = _get_meta_token()
            import requests as _req
            url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative{{title,body,thumbnail_url,object_story_spec}}&access_token={token}"
            resp = _req.get(url, timeout=15)
            ad_data = resp.json()
            cr = ad_data.get('creative', {})
            oss = cr.get('object_story_spec', {})
            link_data = oss.get('link_data', {})
            video_data = oss.get('video_data', {})

            headline = link_data.get('name', '') or video_data.get('title', '') or cr.get('title', '')
            body_text = link_data.get('message', '') or video_data.get('message', '') or cr.get('body', '')
            ad_name = ad_data.get('name', '')

            # Fetch adset targeting to get existing interests
            existing_interests = []
            try:
                t_url = f"https://graph.facebook.com/v26.0/{adset_id}?fields=targeting&access_token={token}"
                t_resp = _req.get(t_url, timeout=10)
                t_data = t_resp.json()
                targeting = t_data.get('targeting', {})
                fs = targeting.get('flexible_spec', [])
                for spec in fs:
                    for interest in spec.get('interests', []):
                        existing_interests.append({
                            'name': interest.get('name', ''),
                            'audience_size': interest.get('audience_size', 0),
                        })
            except:
                pass

            # Get demographics from targeting
            demographics = {}
            try:
                targeting = t_data.get('targeting', {})
                age_min = targeting.get('age_min', '')
                age_max = targeting.get('age_max', '')
                genders = targeting.get('genders', [])
                geo = targeting.get('geo_locations', {})

                if age_min and age_max:
                    demographics['age_display'] = f"{age_min}-{age_max} tahun"
                if genders:
                    gender_map = {1: 'Pria', 2: 'Wanita'}
                    demographics['gender_display'] = ', '.join(gender_map.get(g, '') for g in genders)
                if geo.get('countries'):
                    demographics['country_display'] = ', '.join(geo['countries'])
            except:
                pass

            # Use DeepSeek to analyze ad and suggest interests
            keywords_used = []
            prompt = f"""Analisis iklan Facebook Ads ini dan berikan rekomendasi interest targeting yang relevan.

Judul Iklan: {headline}
Teks Iklan: {body_text}
Nama Iklan: {ad_name}

Beri rekomendasi 10-15 interest Facebook Ads yang PALING RELEVAN dengan audiens target iklan ini.
Format: JSON array of objects dengan field "name" (nama interest), "topic" (kategori), "reason" (kenapa relevan).
Hanya return JSON, tanpa teks lain."""
        else:
            # Custom prompt mode
            prompt_text = body.get('prompt', '')
            if not prompt_text:
                return jsonify({'error': 'Prompt required'}), 400

            # Konteks produk dari dropdown Data Produk (cuma ngefek di mode Custom —
            # mode Referensi ambil dari creative iklan, nggak nyentuh profil produk).
            _pctx_ad = _generator_product_ctx(body.get('product_id'))
            prompt = f"""Seseorang mendeskripsikan audiens target mereka untuk Facebook Ads:

{prompt_text}
{_pctx_ad['block']}
Berdasarkan deskripsi ini, berikan 10-15 rekomendasi interest Facebook Ads yang PALING RELEVAN.
Format: JSON array of objects dengan field "name" (nama interest), "topic" (kategori), "reason" (kenapa relevan).
Hanya return JSON, tanpa teks lain."""
            demographics = {}
            existing_interests = []
            keywords_used = [prompt_text]

        # Call DeepSeek API
        deepseek_key = ''
        try:
            import json as _json
            _cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
            if os.path.exists(_cfg_path):
                with open(_cfg_path, encoding='utf-8') as _f:
                    _cfg = _json.load(_f)
                    deepseek_key = _cfg.get('deepseek_api_key', '')
        except:
            pass
        if not deepseek_key:
            deepseek_key = os.environ.get('DEEPSEEK_API_KEY', '')

        suggestions = []
        if deepseek_key:
            try:
                import requests as _req2
                ds_resp = _req2.post('https://api.deepseek.com/chat/completions', json={
                    'model': 'deepseek-chat',
                    'messages': [
                        {'role': 'system', 'content': 'Anda adalah ahli Facebook Ads targeting. Berikan rekomendasi interest yang spesifik dan relevan. Response dalam format JSON array saja.'},
                        {'role': 'user', 'content': prompt}
                    ],
                    'temperature': 0.7,
                    'max_tokens': 2048,
                }, headers={'Authorization': f'Bearer {deepseek_key}'}, timeout=30)
                ds_data = ds_resp.json()
                content = ds_data.get('choices', [{}])[0].get('message', {}).get('content', '[]')
                # Clean JSON
                content = content.strip()
                if content.startswith('```'):
                    content = content.split('\\n', 1)[-1]
                    if '```' in content:
                        content = content.split('```')[0]
                content = content.strip()
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    suggestions = parsed
            except:
                pass

        # If DeepSeek fails, return fallback suggestions based on keywords
        if not suggestions:
            fallback_keywords = []
            if mode == 'referensi':
                words = (headline + ' ' + body_text).lower()
                # Common parenting/health interests for fallback
                _common_kw = ['parenting', 'ibu', 'anak', 'balita', 'tumbuh kembang', 'speech delay',
                              'stunting', 'mpasi', 'asi', 'imunisasi', 'generos', 'vitamin', 'madu']
                for kw in _common_kw:
                    if kw.lower() in words:
                        fallback_keywords.append(kw)
                if not fallback_keywords:
                    fallback_keywords = ['Parenting Indonesia', 'Ibu Rumah Tangga', 'Tumbuh Kembang Anak']
            else:
                fallback_keywords = [prompt_text]

            suggestions = []
            for kw in fallback_keywords[:5]:
                suggestions.append({
                    'name': kw.title(),
                    'topic': 'Parenting',
                    'audience_size': 5000000,
                })

        return jsonify({
            'suggestions': suggestions,
            'demographics': demographics,
            'existing_interests': existing_interests,
            'keywords_used': keywords_used if mode == 'custom' else [],
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adsets/quick-suggestions', methods=['POST'])
@admin_required
def api_adsets_quick_suggestions():
    """Quick interest search via Meta API."""
    try:
        body = request.get_json(force=True) or {}
        keyword = body.get('keyword', '').strip()
        if not keyword or len(keyword) < 2:
            return jsonify({'error': 'Keyword minimal 2 karakter'}), 400

        token = _get_meta_token()
        if not token:
            return jsonify({'error': 'Meta token tidak tersedia'})

        import urllib.request, urllib.parse, json as _json
        url = f"https://graph.facebook.com/v26.0/search?type=adinterest&q={urllib.parse.quote(keyword)}&limit=10&access_token={token}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = _json.loads(resp.read())

        results = []
        for item in data.get('data', []):
            results.append({
                'name': item.get('name', ''),
                'audience_size': item.get('audience_size', 0),
                'topic': item.get('topic', ''),
            })

        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
@app.route('/copywriting-generator')
@admin_required
def copywriting_generator():
    return render_template('copywriting_generator.html', products=_safe_products())


@app.route('/analisis/iklan')
@admin_required
def analisis_iklan():
    return render_template('analisis_iklan.html')


@app.route('/bank_konten')
@app.route('/bank-konten')
@admin_required
def bank_konten():
    return render_template('bank_konten.html')


BANK_CONTEN_FILE = os.path.join(os.path.dirname(__file__), 'bank_konten_data.json')
BANK_CONTEN_GENMILK_FILE = os.path.join(os.path.dirname(__file__), 'bank_konten_genmilk_data.json')

# Mapping fanpage -> brand bank konten. Fanpage yang tidak terdaftar = Generos.
# Generos Milk Indonesia (GenMilk) = 1046676591867056
PAGE_BANK_KONTEN_MAP = {
    '1046676591867056': 'genmilk',   # Generos Milk Indonesia
}


def _bank_konten_file_for_page(page_id='', page_name=''):
    """Pilih file bank konten sesuai fanpage yang dipilih.

    Return (path, brand). Default brand = 'generos'.
    """
    key = PAGE_BANK_KONTEN_MAP.get(str(page_id or '').strip(), '')
    if not key and page_name and 'milk' in page_name.lower():
        key = 'genmilk'
    if key == 'genmilk':
        return BANK_CONTEN_GENMILK_FILE, 'genmilk'
    return BANK_CONTEN_FILE, 'generos'


@app.route('/api/bank-konten')
@admin_required
def api_bank_konten():
    try:
        if os.path.exists(BANK_CONTEN_FILE):
            with open(BANK_CONTEN_FILE, encoding='utf-8') as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({'error': 'Data belum tersedia', 'files': []})
    except Exception as e:
        return jsonify({'error': str(e), 'files': []})


@app.route('/bank-konten/genmilk')
@admin_required
def bank_konten_genmilk():
    return render_template('bank_konten_genmilk.html')


@app.route('/api/bank-konten/genmilk')
@admin_required
def api_bank_konten_genmilk():
    try:
        if os.path.exists(BANK_CONTEN_GENMILK_FILE):
            with open(BANK_CONTEN_GENMILK_FILE, encoding='utf-8') as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({'error': 'Data belum tersedia', 'files': []})
    except Exception as e:
        return jsonify({'error': str(e), 'files': []})


@app.route('/api/bank-konten/sync', methods=['POST'])
@admin_required
def api_bank_konten_sync():
    """Jalankan sync bank konten via bridge (fetch Meta Ads + upload JSON ke Hostinger)."""
    res = _bridge_call('/api/trigger-bank-sync', method='POST', timeout=180)
    if res.get('status') == 'ok' or res.get('success'):
        msg = res.get('message') or res.get('output') or 'Sync selesai'
        return jsonify({'status': 'ok', 'message': msg})
    return jsonify({'status': 'error', 'error': res.get('error') or res.get('message') or 'Bridge gagal'}), 400


@app.route('/api/analisis/kampanye/llm', methods=['POST'])
@admin_required
def api_analisis_kampanye_llm():
    try:
        body = request.get_json() or {}
        campaigns = body.get('campaigns', [])
        if not campaigns:
            return jsonify({'status': 'error', 'analysis': 'Tidak ada data campaign'})
        
        # Simple analysis without LLM
        total_spend = sum(float(c.get('spend', 0)) for c in campaigns)
        total_results = sum(int(c.get('results', 0)) for c in campaigns)
        total_cpr = total_spend / total_results if total_results > 0 else 0
        active = sum(1 for c in campaigns if c.get('status') == 'ACTIVE')
        
        analysis = f"""Ringkasan {len(campaigns)} campaign:
• Total Spend: Rp{total_spend:,.0f}
• Total Hasil: {total_results}
• Rata-rata CPR: Rp{total_cpr:,.0f}
• Campaign Active: {active}
• Periode: {body.get('date_from', '-')} s/d {body.get('date_to', '-')}
        """
        return jsonify({'status': 'success', 'analysis': analysis.strip()})
    except Exception as e:
        return jsonify({'status': 'error', 'analysis': f'Error: {str(e)}'})


@app.route('/api/adsets')
@admin_required
def api_adsets():
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        account_id = request.args.get('account_id')
        def _produce_adsets():
            adsets = get_adsets(account_id=account_id, date_from=date_from, date_to=date_to, limit=10000)
            result = []
            for a in adsets:
                result.append({
                    'short_name': a.get('short_name', ''),
                    'adset_id': a.get('adset_id', ''),
                    'adset_name': a.get('adset_name', ''),
                    'campaign_id': a.get('campaign_id', ''),
                    'campaign_name': a.get('campaign_name', ''),
                    'status': a.get('status', ''),
                    'spend': float(a.get('spend', 0)),
                    'results': int(a.get('results', 0)),
                    'cpr': float(a.get('cpr', 0)),
                    'cpc': float(a.get('cpc', 0)),
                    'ctr': float(a.get('ctr', 0)),
                    'date': str(a.get('date', ''))
                })
            return result
        ckey = ('adsets', account_id, date_from, date_to)
        return jsonify(_analysis_cache(ckey, _produce_adsets) or [])
    except Exception as e:
        return jsonify({'error': str(e), 'data': []})


@app.route('/api/adsets/targeting-suggestions', methods=['POST'])
@admin_required
def api_targeting_suggestions():
    try:
        data = request.get_json() or {}
        adset_id = data.get('adset_id', '').strip()
        if not adset_id:
            return jsonify({'error': 'adset_id required'}), 400

        from meta_fetcher import get_adset_targeting, search_related_interests
        import requests

        targeting = get_adset_targeting(adset_id)
        if not targeting:
            return jsonify({'error': 'Targeting tidak ditemukan'}), 404

        # Extract interests — bisa dari 'interests' langsung atau dari 'flexible_spec'
        existing_interests = list(targeting.get('interests', []) or [])
        
        # Juga ambil dari flexible_spec
        flexible_spec = targeting.get('flexible_spec', []) or []
        for spec in flexible_spec:
            interests = spec.get('interests', []) or []
            for i in interests:
                dup = any(e.get('id') == i.get('id') for e in existing_interests)
                if not dup:
                    existing_interests.append(i)

        existing_names = [i.get('name', '') for i in existing_interests]

        # Demographics lebih detail
        age_min = targeting.get('age_min') or 18
        age_max = targeting.get('age_max') or 65
        # age_range lebih akurat (bisa beda dari age_min/age_max)
        age_range = targeting.get('age_range', [])
        if age_range and len(age_range) >= 2:
            age_min_display = age_range[0]
            age_max_display = age_range[1]
        else:
            age_min_display = age_min
            age_max_display = age_max

        genders = targeting.get('genders', []) or []
        gender_label = ', '.join({1: 'Pria', 2: 'Wanita'}.get(g, str(g)) for g in genders) if genders else 'Semua'

        geo = targeting.get('geo_locations', {}) or {}
        countries = geo.get('countries', []) or []
        country_str = ', '.join(countries) if countries else 'Indonesia'

        demographics = {
            'age_display': f'{age_min_display}-{age_max_display} thn',
            'gender_display': gender_label,
            'country_display': country_str,
        }

        # Search related interests for each existing interest
        import re as _re
        all_suggestions = {}
        seen_names = set(existing_names)
        for interest in existing_interests:
            name = interest.get('name', '')
            if not name:
                continue
            # Bersihin keyword: hapus bagian dalam kurung kaya "(science)", "(food and drink)"
            clean_name = _re.sub(r'\s*\([^)]*\)', '', name).strip()
            if not clean_name:
                clean_name = name
            related = search_related_interests(clean_name, limit=15)
            for r in related:
                n = r['name']
                if n not in seen_names:
                    seen_names.add(n)
                    if n not in all_suggestions:
                        r['source_interest'] = name
                        all_suggestions[n] = r

        return jsonify({
            'existing_interests': existing_interests,
            'suggestions': list(all_suggestions.values())[:40],
            'demographics': demographics,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adsets/quick-suggestions', methods=['POST'])
@admin_required
def api_quick_suggestions():
    """Search interests directly by keyword."""
    try:
        data = request.get_json() or {}
        keyword = data.get('keyword', '').strip()
        if not keyword:
            return jsonify({'error': 'keyword required'}), 400

        from meta_fetcher import search_related_interests
        results = search_related_interests(keyword, limit=25)
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/adsets/ads', methods=['POST'])
@admin_required
def api_adsets_ads():
    """Fetch individual ads within an adset from Meta API."""
    try:
        data = request.get_json() or {}
        adset_id = data.get('adset_id', '').strip()
        if not adset_id:
            return jsonify({'error': 'adset_id required'}), 400

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify({'error': 'Meta token tidak tersedia'}), 400

        import subprocess, json as _json
        url = f"https://graph.facebook.com/v26.0/{adset_id}/ads?fields=id,name,status,creative{{id,title,body}}&effective_status=[\"ACTIVE\",\"PAUSED\"]&limit=100&access_token={token}"
        r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20)
        data = _json.loads(r.stdout)

        if 'error' in data:
            return jsonify({'error': data['error'].get('message', 'Meta API error')}), 400

        ads = []
        for ad in data.get('data', []):
            cr = ad.get('creative', {})
            ads.append({
                'ad_id': ad['id'],
                'ad_name': ad.get('name', ''),
                'status': ad.get('status', ''),
                'headline': cr.get('title', ''),
                'primary_text': cr.get('body', ''),
            })

        return jsonify({'ads': ads})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/copywriting/ads-list')
@admin_required
def api_copywriting_ads_list():
    """Fetch ads with performance data filter by account + period."""
    try:
        account_id = request.args.get('account_id', '').strip()
        days = request.args.get('days', 7, type=int)

        if not account_id:
            return jsonify({'error': 'account_id required'}), 400

        token = META_TOKEN or ''
        if not token:
            with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                cfg = json.load(f)
                token = cfg.get('meta_token', '')
        if not token:
            return jsonify({'error': 'Meta token tidak tersedia'}), 400

        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        until = date.today().isoformat()

        import subprocess, json as _json
        import requests as _req

        url = (
            f"https://graph.facebook.com/v26.0/{account_id}/ads"
            f"?fields=id,name,status,campaign_id,campaign{{name}},creative{{id,title,body}},"
            f"insights.time_range(%7B%22since%22%3A%22{since}%22%2C%22until%22%3A%22{until}%22%7D)%7Bspend,actions,action_values,cpc,ctr,impressions,reach,frequency%7D"
            f"&effective_status=%5B%22ACTIVE%22%2C%22PAUSED%22%5D"
            f"&limit=200"
        )

        resp = _req.get(url, params={'access_token': token}, timeout=25)
        data = resp.json()

        if 'error' in data:
            return jsonify({'error': data['error'].get('message', 'Meta API error')}), 400

        # Determine action type from account
        from config import ACCOUNTS
        event_type = 'add_to_cart'
        for sn, acc in ACCOUNTS.items():
            if acc['id'] == account_id:
                event_type = acc.get('event', 'add_to_cart')
                break
        action_field = 'add_to_cart' if event_type == 'add_to_cart' else 'lead'

        result = []
        for ad in data.get('data', []):
            ins_list = ad.get('insights', {}).get('data', [])
            ins = ins_list[0] if ins_list else {}
            spend = float(ins.get('spend', 0))

            # Only include ads with spend
            if spend <= 0:
                continue

            # Extract results
            results = 0
            for act in ins.get('actions', []):
                if act.get('action_type') == action_field:
                    results = int(act.get('value', 0))
                    break

            cpr = round(spend / results, 2) if results > 0 else 0
            campaign_info = ad.get('campaign', {})
            cr = ad.get('creative', {})
            sn = ''
            for s, a in ACCOUNTS.items():
                if a['id'] == account_id:
                    sn = s
                    break

            result.append({
                'ad_id': ad['id'],
                'ad_name': ad.get('name', ''),
                'campaign_name': campaign_info.get('name', '') if campaign_info else '',
                'short_name': sn,
                'spend': spend,
                'results': results,
                'cpr': cpr,
                'status': ad.get('status', ''),
                'headline': cr.get('title', '') if cr else '',
                'primary_text': cr.get('body', '') if cr else '',
            })

        result.sort(key=lambda x: (-x['results'], x['cpr']))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ads')
@admin_required
def api_ads():
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        account_id = request.args.get('account_id')
        def _produce_ads():
            campaigns = get_campaigns(account_id=account_id, date_from=date_from, date_to=date_to, limit=10000)

            # Ambil campaign_id unik per akun
            seen = set()
            unique_camps = []
            acc_short_names = set()
            for c in campaigns:
                cid = c.get('campaign_id', '')
                sn = c.get('short_name', '')
                if cid and cid not in seen:
                    seen.add(cid)
                    unique_camps.append(c)
                    if sn:
                        acc_short_names.add(sn)

            # Fetch creative data dari Meta API via akun (lebih efisien)
            token = META_TOKEN or ''
            if not token:
                try:
                    with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                        cfg = json.load(f)
                        token = cfg.get('meta_token', '')
                except:
                    pass

            ad_creatives = {}  # campaign_id -> list of ads
            if token:
                import subprocess, json as _json
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from config import ACCOUNTS
                # Ambil account IDs dari ACCOUNTS config, filter yang relevan
                acc_ids = []
                for sn in acc_short_names:
                    if sn in ACCOUNTS:
                        acc_ids.append(ACCOUNTS[sn]['id'])

                def _fetch_acc_ads(acc_id):
                    try:
                        url = f"https://graph.facebook.com/v26.0/{acc_id}/ads?fields=id,name,campaign_id,creative%7Bthumbnail_url,image_url,video_id,id,title,body,object_story_id%7D&limit=250&filtering=%5B%7B%22field%22%3A%22ad.effective_status%22%2C%22operator%22%3A%22IN%22%2C%22value%22%3A%5B%22ACTIVE%22%2C%22PAUSED%22%5D%7D%5D&access_token={token}"
                        r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '20', url], capture_output=True, text=True, timeout=25)
                        data = _json.loads(r.stdout)
                        return data.get('data', [])
                    except:
                        return []

                # Parallel fetch per akun
                acc_ads_map = {}
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = {pool.submit(_fetch_acc_ads, aid): aid for aid in acc_ids}
                    for fut in as_completed(futures):
                        acc_ads_map[futures[fut]] = fut.result()
                for ads in acc_ads_map.values():
                    for ad in ads:
                        cid = ad.get('campaign_id', '')
                        if cid:
                            ad_creatives.setdefault(cid, []).append(ad)

            # Gabung data campaign + creative
            result = []
            for c in campaigns:
                cid = c.get('campaign_id', '')
                creative = ad_creatives.get(cid, [])
                thumb = ''
                img = ''
                vid = ''
                cr_title = ''
                cr_body = ''
                obj_story_id = ''
                ad_id = cid
                ad_name = c.get('campaign_name', '')
                if creative:
                    first = creative[0]
                    cr = first.get('creative', {})
                    thumb = cr.get('thumbnail_url', '')
                    img = cr.get('image_url', '')
                    vid = cr.get('video_id', '')
                    cr_title = cr.get('title', '') or first.get('name', '')
                    cr_body = cr.get('body', '')
                    obj_story_id = cr.get('object_story_id', '')
                    ad_id = first.get('id', cid)
                    ad_name = first.get('name', ad_name)
                result.append({
                    'short_name': c.get('short_name', ''),
                    'ad_id': ad_id,
                    'ad_name': ad_name,
                    'campaign_name': c.get('campaign_name', ''),
                    'status': c.get('status', ''),
                    'spend': float(c.get('spend', 0)),
                    'results': int(c.get('results', 0)),
                    'cpr': float(c.get('cpr', 0)),
                    'cpc': float(c.get('cpc', 0)),
                    'ctr': float(c.get('ctr', 0)),
                    'thumbnail_url': thumb,
                    'image_url': img,
                    'video_id': vid,
                    'object_story_id': obj_story_id,
                    'creative_title': cr_title,
                    'creative_body': cr_body,
                    'date': str(c.get('date', ''))
                })
            return result
        ckey = ('ads', account_id, date_from, date_to)
        return jsonify(_analysis_cache(ckey, _produce_ads) or [])
    except Exception as e:
        return jsonify({'error': str(e), 'data': []})


@app.route('/api/ads/primary-text', methods=['POST'])
@admin_required
def api_ads_primary_text():
    """Fetch primary text from a specific ad via Meta API."""
    try:
        data = request.get_json() or {}
        ad_id = data.get('ad_id', '').strip()
        if not ad_id:
            return jsonify({'error': 'ad_id required'}), 400

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass

        if not token:
            return jsonify({'error': 'Meta token tidak tersedia'}), 400

        import subprocess, json as _json
        url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative{{title,body,id}}&access_token={token}"
        r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20)
        ad_data = _json.loads(r.stdout)

        if 'error' in ad_data:
            return jsonify({'error': ad_data['error'].get('message', 'Meta API error')}), 400

        cr = ad_data.get('creative', {})
        return jsonify({
            'ad_id': ad_data.get('id', ad_id),
            'ad_name': ad_data.get('name', ''),
            'headline': cr.get('title', ''),
            'primary_text': cr.get('body', ''),
        })
    except Exception as e:
        return jsonify({'error': str(e), 'data': []})


@app.route('/api/copywriting/generate', methods=['POST'])
@admin_required
def api_copywriting_generate():
    """Generate copywriting variations using DeepSeek API."""
    try:
        data = request.get_json() or {}
        mode = data.get('mode', 'referensi')
        custom_prompt = data.get('prompt', '').strip()
        headline = data.get('headline', '').strip()
        primary_text = data.get('primary_text', '').strip()
        ad_name = data.get('ad_name', '')
        tone = data.get('tone', 'natural')

        if mode == 'custom':
            if not custom_prompt:
                return jsonify({'error': 'Tulis dulu ide copywriting kamu'}), 400
        elif not primary_text and not headline:
            return jsonify({'error': 'Primary text atau headline diperlukan'}), 400

        # Dapatkan DeepSeek key dari config
        deepseek_key = ''
        try:
            with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                cfg = json.load(f)
                deepseek_key = cfg.get('deepseek_api_key', '')
        except:
            pass
        if not deepseek_key:
            try:
                from config import DEEPSEEK_API_KEY
                deepseek_key = DEEPSEEK_API_KEY
            except:
                pass

        if not deepseek_key:
            return jsonify({'error': 'DeepSeek API key tidak tersedia'}), 400

        tone_names = {
            'natural': 'Natural & hangat',
            'professional': 'Profesional & kredibel',
            'urgent': 'Urgent & FOMO',
            'storytelling': 'Storytelling & emosional',
            'benefit': 'Benefit-focused'
        }
        tone_name = tone_names.get(tone, 'Natural & hangat')

        # Konteks produk dari dropdown Data Produk. Kosong → teks default (perilaku lama persis).
        _pctx = _generator_product_ctx(data.get('product_id'))
        _p_brand = _pctx['brand'] or 'Produk'
        _p_desc = _pctx['desc'] or 'multivitamin alami untuk daya tahan tubuh anak & dewasa'
        _p_manfaat = '; '.join(_pctx['benefits']) if _pctx['benefits'] else '5 bahan alami (mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan)'

        if mode == 'custom':
            referensi_blok = f"""IDE KONTEN DARI USER:
{custom_prompt}"""
        else:
            referensi_blok = f"""REFERENSI IKLAN:
Nama Iklan: {ad_name}
Headline: {headline}
Primary Text Asli:
{primary_text}"""

        prompt = f"""Kamu adalah copywriter {_p_brand}, {_p_desc}.

TUGAS:
Buat 3 variasi copywriting/primary text untuk iklan Meta Ads berdasarkan referensi di bawah.

TONE: {tone_name}

{referensi_blok}
{_pctx['block']}
KETENTUAN:
1. Bahasa Indonesia yang natural dan engaging
2. Maksimal 150 kata per variasi
3. Tetap mention manfaat: {_p_manfaat}
4. Setiap variasi harus punya angle berbeda
5. Jangan angka/nomor di awal teks variasi
6. Variasi 1: Fokus pada MANFAAT & SOLUSI
7. Variasi 2: Fokus pada BAHAN ALAMI & KEPERCAYAAN
8. Variasi 3: Fokus pada TESTIMONIAL/EMOSI
9. WAJIB: body ditulis 2-3 paragraf pendek, setiap paragraf 2-3 kalimat, pisahkan paragraf dengan baris kosong.
10. WAJIB: setiap variasi punya 4 bagian lengkap: HEADLINE, SUBHEADLINE, BODY, BENEFITS (3-4 poin), dan CTA. Ikuti format output di bawah.

FORMAT OUTPUT — keluarkan persis dalam format ini tanpa nomor di awal:

[VARIASI 1: Manfaat & Solusi]
HEADLINE: (judul utama singkat, maksimal 12 kata, menarik)
SUBHEADLINE: (1 kalimat pendukung headline)
BODY:
(teks body 2-3 paragraf pendek)
BENEFITS:
- (benefit 1, mulai dengan "✅")
- (benefit 2, mulai dengan "✅")
- (benefit 3, mulai dengan "✅")
CTA: (kalimat ajakan aksi + nama tombol, contoh: Klik Tombol "Ambil Promo" sekarang juga)

[VARIASI 2: Bahan Alami & Kepercayaan]
HEADLINE: (judul utama singkat, maksimal 12 kata, menarik)
SUBHEADLINE: (1 kalimat pendukung headline)
BODY:
(teks body 2-3 paragraf pendek)
BENEFITS:
- (benefit 1, mulai dengan "✅")
- (benefit 2, mulai dengan "✅")
- (benefit 3, mulai dengan "✅")
CTA: (kalimat ajakan aksi + nama tombol)

[VARIASI 3: Testimonial & Emosi]
HEADLINE: (judul utama singkat, maksimal 12 kata, menarik)
SUBHEADLINE: (1 kalimat pendukung headline)
BODY:
(teks body 2-3 paragraf pendek)
BENEFITS:
- (benefit 1, mulai dengan "✅")
- (benefit 2, mulai dengan "✅")
- (benefit 3, mulai dengan "✅")
CTA: (kalimat ajakan aksi + nama tombol)"""

        import subprocess, json as _json

        payload = _json.dumps({
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'system', 'content': f'Kamu adalah copywriter expert untuk produk kesehatan natural {_p_brand}.'},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.8,
            'max_tokens': 1500,
        })

        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', 'https://api.deepseek.com/chat/completions',
             '-H', 'Content-Type: application/json',
             '-H', f'Authorization: Bearer {deepseek_key}',
             '-d', payload,
             '--connect-timeout', '10', '--max-time', '30'],
            capture_output=True, timeout=35
        )

        stdout_str = r.stdout.decode('utf-8')
        result = _json.loads(stdout_str)

        if 'choices' not in result or not result['choices']:
            error_msg = result.get('error', {}).get('message', 'DeepSeek API error')
            print(f"[COPYWRITING] API error: {stdout_str[:300]}", flush=True)
            return jsonify({'error': error_msg}), 500

        generated_text = result['choices'][0]['message']['content'].strip()

        def _rapikan_paragraf(txt):
            """Pecah blok panjang jadi 2-3 kalimat per paragraf (kalau AI masih satu blok)."""
            lines = [l.strip() for l in txt.split('\n') if l.strip()]
            # Kalau udah ada baris kosong / paragraf terpisah, biarkan
            if len(lines) > 1:
                return '\n\n'.join(lines)
            # Satu blok: pecah per kalimat
            import re as _re
            sentences = _re.split(r'(?<=[.!?])\s+', txt.strip())
            if len(sentences) <= 2:
                return txt.strip()
            # Kelompokkan 2-3 kalimat per paragraf
            groups, cur = [], []
            for i, s in enumerate(sentences):
                cur.append(s)
                if len(cur) >= 3 or i == len(sentences) - 1:
                    groups.append(' '.join(cur))
                    cur = []
            return '\n\n'.join(groups)

        # Parse variasi (format terstruktur: HEADLINE / SUBHEADLINE / BODY / BENEFITS / CTA)
        variations = []
        current_var = None
        current_lines = []

        def _parse_structure(block_lines):
            """Parse blok variasi jadi dict {headline, subheadline, body, benefits, cta}.
            Fallback: kalau format struktur tidak ketemu, seluruh teks jadi body."""
            out = {'headline': '', 'subheadline': '', 'body': '', 'benefits': [], 'cta': ''}
            lines = [l.strip() for l in block_lines if l.strip()]
            if not lines:
                return out

            section = None
            body_parts = []
            for raw_line in lines:
                # Normalisasi: buang markdown bold/asterisk + strip
                line = raw_line.lstrip('*').strip()
                lower = line.lower()
                if lower.startswith('headline:'):
                    out['headline'] = line.split(':', 1)[1].strip()
                    section = None
                elif lower.startswith('subheadline:') or lower.startswith('sub headline:'):
                    out['subheadline'] = line.split(':', 1)[1].strip()
                    section = None
                elif lower.startswith('body:'):
                    section = 'body'
                elif lower.startswith('benefits:') or lower.startswith('benefit:'):
                    section = 'benefits'
                elif lower.startswith('cta:'):
                    out['cta'] = line.split(':', 1)[1].strip()
                    section = None
                elif section == 'body':
                    body_parts.append(raw_line)
                elif section == 'benefits':
                    b = raw_line.lstrip('-•').strip()
                    if b:
                        out['benefits'].append(b)
                else:
                    # Teks di luar section — treat sebagai body (toleran)
                    body_parts.append(raw_line)

            out['body'] = _rapikan_paragraf('\n'.join(body_parts).strip())
            # Kalau tidak ada struktur sama sekali, gunakan teks asli sebagai body
            if not out['headline'] and not out['body'] and not out['cta']:
                out['body'] = _rapikan_paragraf('\n'.join(lines))
            return out

        for line in generated_text.split('\n'):
            line = line.strip()
            if line.startswith('[VARIASI'):
                if current_var is not None:
                    variations.append({
                        'title': current_var,
                        'text': _rapikan_paragraf('\n'.join(current_lines).strip()),
                        **_parse_structure(current_lines)
                    })
                current_var = line.strip('[]').strip()
                current_lines = []
            elif current_var is not None:
                current_lines.append(line)

        if current_var is not None:
            variations.append({
                'title': current_var,
                'text': _rapikan_paragraf('\n'.join(current_lines).strip()),
                **_parse_structure(current_lines)
            })

        if not variations:
            # Fallback: treat entire output as one variation
            variations = [{
                'title': 'Copy Baru',
                'text': generated_text,
                **_parse_structure(generated_text.split('\n'))
            }]

        return jsonify({
            'variations': variations,
            'raw': generated_text
        })

    except Exception as e:
        print(f"[COPYWRITING] Error: {e}", flush=True)
        return jsonify({'error': str(e)}), 500


def _send_toggle_notification(campaign_id, campaign_name, short_name, action, new_status):
    """Send toggle notification to Telegram topic 1958."""
    if not TELEGRAM_BOT_TOKEN:
        print("[TOGGLE] Token empty, skipping notification", flush=True)
        return
    import subprocess, urllib.parse

    status_icon = '▶️' if action == 'resume' else '⏸'
    emoji_arrow = '🟢 ON' if action == 'resume' else '🔴 OFF'

    text = (
        f"{status_icon} Campaign Update\n"
        f"{'━'*30}\n"
        f"📛 {short_name} — {campaign_name}\n"
        f"📌 Status: {emoji_arrow}\n"
        f"{'━'*30}"
    )

    chat_id = '-1003990111670'
    topic_id = '1958'
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'message_thread_id': topic_id,
        'text': text,
        'parse_mode': 'HTML',
    })

    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
            capture_output=True, text=True, timeout=15
        )
        print(f"[TOGGLE] TG response: {r.stdout[:200]}", flush=True)
        if '{"ok":true' in r.stdout:
            print(f"[TOGGLE] TG sent OK", flush=True)
        else:
            print(f"[TOGGLE] TG failed: {r.stdout[:200]}", flush=True)
    except Exception as e:
        print(f"[TOGGLE] TG curl error: {e}", flush=True)


def _send_adset_toggle_notification(adset_id, adset_name, campaign_name, short_name, action, new_status):
    """Send adset toggle notification to Telegram topic 1958."""
    if not TELEGRAM_BOT_TOKEN:
        print("[ADSET TOGGLE] Token empty, skipping notification", flush=True)
        return
    import subprocess, urllib.parse

    status_icon = '▶️' if action == 'resume' else '⏸'
    emoji_arrow = '🟢 ON' if action == 'resume' else '🔴 OFF'

    text = (
        f"{status_icon} Adset Update\n"
        f"{'━'*30}\n"
        f"🎯 {short_name} — {campaign_name}\n"
        f"📎 Adset: {adset_name}\n"
        f"📌 Status: {emoji_arrow}\n"
        f"{'━'*30}"
    )

    chat_id = '-1003990111670'
    topic_id = '1958'
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'message_thread_id': topic_id,
        'text': text,
        'parse_mode': 'HTML',
    })

    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
            capture_output=True, text=True, timeout=15
        )
        print(f"[ADSET TOGGLE] TG response: {r.stdout[:200]}", flush=True)
        if '{"ok":true' in r.stdout:
            print(f"[ADSET TOGGLE] TG sent OK", flush=True)
        else:
            print(f"[ADSET TOGGLE] TG failed: {r.stdout[:200]}", flush=True)
    except Exception as e:
        print(f"[ADSET TOGGLE] TG curl error: {e}", flush=True)


def _send_budget_notification(short_name, campaign_name, campaign_id, level, old_budget, new_budget, status='berhasil', error_msg=''):
    """Send budget change notification to Telegram topic 1958."""
    if not TELEGRAM_BOT_TOKEN:
        print("[BUDGET] Token empty, skipping notification", flush=True)
        return
    import subprocess, urllib.parse

    if status == 'berhasil':
        icon = '✅'
        detail = f"Rp{old_budget:,} → Rp{new_budget:,}"
    else:
        icon = '❌'
        detail = f"Gagal: {error_msg}"

    level_icon = '📢' if level == 'campaign' else '🎯'
    text = (
        f"{icon} Budget Diubah\n"
        f"{'━'*30}\n"
        f"{level_icon} {short_name} — {campaign_name}\n"
        f"💰 {detail}\n"
        f"{'━'*30}"
    )

    chat_id = '-1003990111670'
    topic_id = '1958'
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        'chat_id': chat_id,
        'message_thread_id': topic_id,
        'text': text,
        'parse_mode': 'HTML',
    })

    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
            capture_output=True, text=True, timeout=15
        )
        print(f"[BUDGET] TG response: {r.stdout[:200]}", flush=True)
        if '{"ok":true' in r.stdout:
            print(f"[BUDGET] TG sent OK", flush=True)
        else:
            print(f"[BUDGET] TG failed: {r.stdout[:200]}", flush=True)
    except Exception as e:
        print(f"[BUDGET] TG curl error: {e}", flush=True)


@app.route('/api/campaign/toggle', methods=['POST'])
@admin_required
def api_campaign_toggle():
    """Toggle campaign pause/resume via Meta Ads API."""
    try:
        data = request.get_json() or {}
        campaign_id = data.get('campaign_id', '').strip()
        action = data.get('action', '').strip()

        if not campaign_id or action not in ('pause', 'resume'):
            return jsonify({'status': 'error', 'message': 'Parameter campaign_id dan action (pause/resume) required'}), 400

        # Get Meta token
        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass

        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 400

        new_status = 'PAUSED' if action == 'pause' else 'ACTIVE'
        import subprocess, urllib.parse

        url = f"https://graph.facebook.com/v26.0/{campaign_id}"
        post_data = urllib.parse.urlencode({'status': new_status, 'access_token': token})

        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', post_data, '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20
        )

        result = json.loads(r.stdout) if r.stdout.strip() else {}

        if 'success' in result and result['success']:
            # Send notification to Telegram
            try:
                _send_toggle_notification(
                    campaign_id=campaign_id,
                    campaign_name=data.get('campaign_name', ''),
                    short_name=data.get('short_name', ''),
                    action=action,
                    new_status=new_status
                )
            except Exception as e:
                print(f"[TOGGLE] Notif error: {e}", flush=True)

            # Update status di database biar dashboard langsung reflect
            try:
                affected = update_campaign_status(campaign_id, new_status)
                print(f"[TOGGLE] DB status updated for {campaign_id}: {affected} row(s)", flush=True)
            except Exception as e:
                print(f"[TOGGLE] DB update error: {e}", flush=True)

            return jsonify({'status': 'ok', 'message': f'Campaign {new_status}', 'campaign_id': campaign_id})
        elif 'error' in result:
            return jsonify({'status': 'error', 'message': result['error'].get('message', 'Unknown Meta error')}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Gagal toggle campaign', 'response': result}), 400

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/adset/toggle', methods=['POST'])
@admin_required
def api_adset_toggle():
    """Toggle adset pause/resume via Meta Ads API."""
    try:
        data = request.get_json() or {}
        adset_id = data.get('adset_id', '').strip()
        action = data.get('action', '').strip()

        if not adset_id or action not in ('pause', 'resume'):
            return jsonify({'status': 'error', 'message': 'Parameter adset_id dan action (pause/resume) required'}), 400

        # Get Meta token
        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass

        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 400

        new_status = 'PAUSED' if action == 'pause' else 'ACTIVE'
        import subprocess, urllib.parse

        url = f"https://graph.facebook.com/v26.0/{adset_id}"
        post_data = urllib.parse.urlencode({'status': new_status, 'access_token': token})

        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', post_data, '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20
        )

        result = json.loads(r.stdout) if r.stdout.strip() else {}

        if 'success' in result and result['success']:
            # Send notification to Telegram
            try:
                _send_adset_toggle_notification(
                    adset_id=adset_id,
                    adset_name=data.get('adset_name', ''),
                    campaign_name=data.get('campaign_name', ''),
                    short_name=data.get('short_name', ''),
                    action=action,
                    new_status=new_status
                )
            except Exception as e:
                print(f"[ADSET TOGGLE] Notif error: {e}", flush=True)

            # Update status di database biar dashboard langsung reflect
            try:
                affected = update_adset_status(adset_id, new_status)
                print(f"[ADSET TOGGLE] DB status updated for {adset_id}: {affected} row(s)", flush=True)
            except Exception as e:
                print(f"[ADSET TOGGLE] DB update error: {e}", flush=True)

            return jsonify({'status': 'ok', 'message': f'Adset {new_status}', 'adset_id': adset_id})
        elif 'error' in result:
            return jsonify({'status': 'error', 'message': result['error'].get('message', 'Unknown Meta error')}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Gagal toggle adset', 'response': result}), 400

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/campaign/detail')
@admin_required
def api_campaign_detail():
    """Fetch adsets + ads + creative details for a campaign (optimized)."""
    camp_id = request.args.get('campaign_id', '').strip()
    short_name = request.args.get('short_name', 'G4')
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    if not camp_id:
        return jsonify({'status': 'error', 'message': 'campaign_id required'})

    token = _get_meta_token()
    api_ver = 'v26.0'

    try:
        # Single nested call: adsets → ads → creative
        fields = 'id,name,status,ads{id,name,status,creative{id,object_story_spec{link_data{name,message},video_data{title,message}}}}'
        url = f'https://graph.facebook.com/{api_ver}/{camp_id}/adsets?fields={urllib.parse.quote(fields)}&limit=50&access_token={token}'
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as r:
            adsets_data = json.loads(r.read().decode()).get('data', [])

        # Date range default: hari ini
        if not date_from:
            date_from = date.today().isoformat()
        if not date_to:
            date_to = date.today().isoformat()

        # Metrik per adset dari DB (tabel adsets hasil sync) — hemat rate limit
        adset_metrics = {}
        try:
            conn = get_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """SELECT adset_id,
                          SUM(spend) as total_spend,
                          SUM(results) as total_results
                   FROM adsets
                   WHERE campaign_id = %s AND date BETWEEN %s AND %s
                   GROUP BY adset_id""",
                (camp_id, date_from, date_to)
            )
            for rw in cur.fetchall():
                spend = float(rw.get('total_spend') or 0)
                results = int(rw.get('total_results') or 0)
                adset_metrics[rw['adset_id']] = {
                    'spend': spend,
                    'results': results,
                    'cpr': round(spend / results, 2) if results > 0 else 0,
                }
            cur.close()
            conn.close()
        except Exception as db_e:
            print(f"DB metrics error {camp_id}: {db_e}")

        result_adsets = []
        result_ads = []
        for adset in adsets_data:
            adset_id = adset.get('id', '')
            m = adset_metrics.get(adset_id, {'spend': 0, 'results': 0, 'cpr': 0})
            ads_raw = adset.get('ads', {}).get('data', [])
            ads_result = []
            for ad in ads_raw:
                ad_entry = {
                    'ad_name': ad.get('name', ''),
                    'status': ad.get('status', ''),
                    'headline': '',
                    'primary_text': '',
                }
                cr = ad.get('creative', {})
                cr_data = cr if isinstance(cr, dict) else {}
                spec = cr_data.get('object_story_spec', {})
                if spec and isinstance(spec, str):
                    try: spec = json.loads(spec)
                    except: pass
                link_data = spec.get('link_data', {}) if isinstance(spec, dict) else {}
                video_data = spec.get('video_data', {}) if isinstance(spec, dict) else {}
                if link_data:
                    ad_entry['headline'] = link_data.get('name', '')
                    ad_entry['primary_text'] = link_data.get('message', '')
                elif video_data:
                    ad_entry['headline'] = video_data.get('title', '')
                    ad_entry['primary_text'] = video_data.get('message', '')
                ads_result.append(ad_entry)
                result_ads.append({'ad_name': ad_entry['ad_name'], 'status': ad_entry['status']})

            result_adsets.append({
                'adset_id': adset_id,
                'adset_name': adset.get('name', ''),
                'status': adset.get('status', ''),
                'spend': m['spend'],
                'results': m['results'],
                'cpr': m['cpr'],
                'ads': ads_result,
            })

        return jsonify({
            'status': 'ok',
            'count': len(result_adsets),
            'date_from': date_from,
            'date_to': date_to,
            'adsets': result_adsets,
            'ads': result_ads,
        })
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        try: err_json = json.loads(err_body)
        except: err_json = {'message': err_body}
        return jsonify({'status': 'error', 'message': err_json.get('error', {}).get('message', err_body)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Budget cache (file-based for Passenger multi-worker)
BUDGET_CACHE_FILE = os.path.join(os.path.dirname(__file__), '.budget_cache.json')
BUDGET_CACHE_TTL = 120  # detik — naik dari 30 biar nggak ke-fetch Meta tiap buka halaman

@app.route('/api/budget/campaigns')
@admin_required
def api_budget_campaigns():
    """Fetch real-time budget data for all active campaigns from Meta API."""
    try:
        days = int(request.args.get('days', 1))
        if days < 1: days = 1
        if days > 90: days = 90
        until_str = date.today().isoformat()
        since_str = (date.today() - timedelta(days=days-1)).isoformat()
        now = __import__('time').time()
        # Check file cache first
        if os.path.exists(BUDGET_CACHE_FILE):
            try:
                with open(BUDGET_CACHE_FILE, encoding='utf-8') as f:
                    cache = json.load(f)
                if cache.get('ts') and (now - cache['ts']) < BUDGET_CACHE_TTL and cache.get('key') == f'days_{days}':
                    return jsonify({'status': 'success', 'data': cache['data'], 'cached': True})
            except:
                pass

        import subprocess
        from concurrent.futures import ThreadPoolExecutor, as_completed

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 400

        def _fetch_budget_account(short_name, acc):
            """Fetch campaign budgets + adset budgets (ABO) untuk satu akun."""
            acc_id = acc['id']
            url = f'https://graph.facebook.com/v26.0/{acc_id}/campaigns'
            params = urllib.parse.urlencode({
                'access_token': token,
                'fields': 'id,name,status,daily_budget,lifetime_budget,buying_type',
                'effective_status': '["ACTIVE"]',
                'limit': 100
            })
            r = subprocess.run(
                ['curl', '-s', '--connect-timeout', '10', '--max-time', '15', f'{url}?{params}'],
                capture_output=True, text=True, timeout=20
            )
            data = json.loads(r.stdout) if r.stdout.strip() else {}
            campaigns_data = data.get('data', [])
            acc_entries = []
            for c in campaigns_data:
                name = c.get('name', '')
                has_budget = c.get('daily_budget') or c.get('lifetime_budget')
                is_abo = not has_budget
                entry = {
                    'campaign_id': c['id'],
                    'campaign_name': name,
                    'status': c.get('status', ''),
                    'budget': int(c.get('daily_budget') or c.get('lifetime_budget') or 0),
                    'type': 'ABO' if is_abo else 'CBO',
                    'account_id': acc_id,
                    'short_name': short_name,
                    'adsets': []
                }
                # For ABO, fetch adsets
                if is_abo:
                    adset_url = f'https://graph.facebook.com/v26.0/{c["id"]}/adsets'
                    adset_params = urllib.parse.urlencode({
                        'access_token': token,
                        'fields': f'id,name,daily_budget,lifetime_budget,status,insights.time_range({{"since":"{since_str}","until":"{until_str}"}}){{spend,actions}}',
                        'limit': 50
                    })
                    ar = subprocess.run(
                        ['curl', '-s', '--connect-timeout', '8', '--max-time', '12', f'{adset_url}?{adset_params}'],
                        capture_output=True, text=True, timeout=15
                    )
                    adset_data = json.loads(ar.stdout) if ar.stdout.strip() else {}
                    for a in adset_data.get('data', []):
                        adset_budget = int(a.get('daily_budget') or a.get('lifetime_budget') or 0)
                        insights = a.get('insights', {}) or {}
                        spend_val = 0
                        results_val = 0
                        if insights.get('data'):
                            ins = insights['data'][0]
                            spend_val = float(ins.get('spend', 0))
                            for act in ins.get('actions', []):
                                if act.get('action_type') in ('add_to_cart', 'lead'):
                                    results_val = int(act.get('value', 0))
                                    break
                        entry['adsets'].append({
                            'adset_id': a['id'],
                            'adset_name': a.get('name', ''),
                            'budget': adset_budget,
                            'spend': spend_val,
                            'results': results_val,
                            'status': a.get('status', '')
                        })
                acc_entries.append(entry)
            return acc_entries

        result = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_budget_account, sn, acc): sn for sn, acc in ACCOUNTS.items()}
            for fut in as_completed(futures):
                try:
                    result.extend(fut.result())
                except Exception:
                    pass

        _budget_cache = result
        _budget_cache_time = __import__('time').time()
        # Save to file cache (cross-worker)
        try:
            with open(BUDGET_CACHE_FILE, 'w') as _f:
                json.dump({'ts': __import__('time').time(), 'key': f'days_{days}', 'data': result}, _f)
        except:
            pass
        return jsonify({'status': 'success', 'data': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/budget/update', methods=['POST'])
@admin_required
def api_budget_update():
    """Update CBO campaign budget directly via Meta API."""
    try:
        import subprocess
        data = request.get_json() or {}
        campaign_id = data.get('campaign_id', '').strip()
        budget = data.get('budget', 0)
        campaign_name = data.get('campaign_name', '')
        short_name = data.get('short_name', '')
        account_id = data.get('account_id', '')
        old_budget = data.get('old_budget', 0)

        if not campaign_id or not budget:
            return jsonify({'status': 'error', 'message': 'campaign_id dan budget required'}), 400
        if budget < 5000:
            return jsonify({'status': 'error', 'message': 'Minimal budget Rp5.000'}), 400

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 400

        url = f'https://graph.facebook.com/v26.0/{campaign_id}'
        post_data = urllib.parse.urlencode({'daily_budget': budget, 'access_token': token})

        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', post_data, '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20
        )
        result = json.loads(r.stdout) if r.stdout.strip() else {}

        if 'success' in result and result['success']:
            save_budget_change(account_id, campaign_id, campaign_name, 'campaign', old_budget, budget)
            try:
                _send_budget_notification(short_name, campaign_name, campaign_id, 'campaign', old_budget, budget)
            except:
                pass
            return jsonify({'status': 'success', 'message': 'Budget berhasil diupdate'})
        elif 'error' in result:
            err_msg = result['error'].get('message', 'Unknown error')
            save_budget_change(account_id, campaign_id, campaign_name, 'campaign', old_budget, budget, 'failed', err_msg)
            try:
                _send_budget_notification(short_name, campaign_name, campaign_id, 'campaign', old_budget, budget, 'gagal', err_msg)
            except:
                pass
            return jsonify({'status': 'error', 'message': err_msg}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Gagal update budget', 'response': result}), 400

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/budget/update-adset', methods=['POST'])
@admin_required
def api_budget_update_adset():
    """Update ABO adset budget directly via Meta API."""
    try:
        import subprocess
        data = request.get_json() or {}
        adset_id = data.get('adset_id', '').strip()
        budget = data.get('budget', 0)
        campaign_name = data.get('campaign_name', '')
        short_name = data.get('short_name', '')
        account_id = data.get('account_id', '')
        old_budget = data.get('old_budget', 0)
        adset_name = data.get('adset_name', '')

        if not adset_id or not budget:
            return jsonify({'status': 'error', 'message': 'adset_id dan budget required'}), 400
        if budget < 5000:
            return jsonify({'status': 'error', 'message': 'Minimal budget Rp5.000'}), 400

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 400

        url = f'https://graph.facebook.com/v26.0/{adset_id}'
        post_data = urllib.parse.urlencode({'daily_budget': budget, 'access_token': token})

        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', post_data, '--connect-timeout', '10', '--max-time', '15', url],
            capture_output=True, text=True, timeout=20
        )
        result = json.loads(r.stdout) if r.stdout.strip() else {}

        if 'success' in result and result['success']:
            save_budget_change(account_id, adset_id, campaign_name, 'adset', old_budget, budget, adset_id=adset_id, adset_name=adset_name)
            try:
                _send_budget_notification(short_name, f"{adset_name} ({campaign_name})", adset_id, 'adset', old_budget, budget)
            except:
                pass
            return jsonify({'status': 'success', 'message': 'Budget adset berhasil diupdate'})
        elif 'error' in result:
            err_msg = result['error'].get('message', 'Unknown error')
            save_budget_change(account_id, adset_id, campaign_name, 'adset', old_budget, budget, 'failed', err_msg, adset_id=adset_id, adset_name=adset_name)
            return jsonify({'status': 'error', 'message': err_msg}), 400
        else:
            return jsonify({'status': 'error', 'message': 'Gagal update budget adset', 'response': result}), 400

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/check-auth')
def api_check_auth():
    """Cek session admin masih valid — dipakai landing page biar nggak minta login terus."""
    return jsonify({'admin': bool(session.get('admin'))})


@app.route('/api/verify-password', methods=['POST'])
def verify_password():
    data = request.get_json() or {}
    pwd = data.get('password', '')
    from werkzeug.security import check_password_hash
    # Support: werkzeug hash (scrypt/pbkdf2) OR plain text (backward compat)
    if (ADMIN_PASSWORD and pwd and ADMIN_PASSWORD.startswith(('scrypt:', 'scrypt$', 'pbkdf2:', '$2', '$5', '$6'))
        and check_password_hash(ADMIN_PASSWORD, pwd)) or pwd == ADMIN_PASSWORD:
        session['admin'] = True
        session.permanent = True
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Password salah'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'ok'})


@app.route('/logout')
@admin_required
def logout_page():
    """Logout via navigasi: hapus session server lalu balik ke landing."""
    session.clear()
    return redirect('/')


@app.route('/api/test-notif', methods=['GET'])
@admin_required
def api_test_notif():
    """Test notification endpoint."""
    import subprocess, urllib.parse, json as json_module
    
    result = {
        'token_exists': bool(TELEGRAM_BOT_TOKEN),
        'token_length': len(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else 0,
        'token_prefix': TELEGRAM_BOT_TOKEN[:15] + '...' if TELEGRAM_BOT_TOKEN else '',
    }
    
    if not TELEGRAM_BOT_TOKEN:
        result['status'] = 'error'
        result['message'] = 'Token kosong'
        return jsonify(result)
    
    text = (
        '🧪 Test Notif dari Hostinger\n'
        '━━━━━━━━━━━━━━━━━━━━━━\n'
        '📛 Test\n'
        '📌 Status: 🟢 OK\n'
        '━━━━━━━━━━━━━━━━━━━━━━'
    )
    
    payload = urllib.parse.urlencode({
        'chat_id': '-1003990111670',
        'message_thread_id': '1958',
        'text': text,
    })
    api_url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    
    try:
        r = subprocess.run(
            ['curl', '-s', '-X', 'POST', '-d', payload, '--connect-timeout', '5', '--max-time', '10', api_url],
            capture_output=True, text=True, timeout=15
        )
        result['curl_exit'] = r.returncode
        result['curl_response'] = r.stdout[:300]
        try:
            j = json_module.loads(r.stdout)
            result['tg_ok'] = j.get('ok', False)
            if not j.get('ok'):
                result['tg_error'] = j.get('description', '')
        except:
            result['tg_ok'] = False
            result['tg_error'] = 'Parse error'
        result['status'] = 'ok' if result.get('tg_ok') else 'error'
    except Exception as e:
        result['status'] = 'error'
        result['error_detail'] = str(e)
    
    return jsonify(result)


@app.route('/storyboard')
@admin_required
def storyboard():
    return render_template('storyboard_generator.html', products=_safe_products())


@app.route('/api/storyboard/ads-list')
@admin_required
def api_storyboard_ads_list():
    """Return ads data from Meta API for storyboard dropdown (filter by account + period)."""
    try:
        account_id = request.args.get('account_id', '').strip()
        days = request.args.get('days', 7, type=int)

        if not account_id:
            return jsonify([])

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify([])

        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        until = date.today().isoformat()

        import requests as _req

        # Determine action type from account
        from config import ACCOUNTS
        event_type = 'add_to_cart'
        for sn, acc in ACCOUNTS.items():
            if acc['id'] == account_id:
                event_type = acc.get('event', 'add_to_cart')
                break
        action_field = 'add_to_cart' if event_type == 'add_to_cart' else 'lead'
        short_name = {'act_314725990877293': 'G1', 'act_934880001670906': 'G2', 'act_1488472672014493': 'G3', 'act_327433619915012': 'G4'}.get(account_id, '?')

        url = (
            f"https://graph.facebook.com/v26.0/{account_id}/ads"
            f"?fields=id,name,status,campaign_id,campaign{{name}},"
            f"insights.time_range(%7B%22since%22%3A%22{since}%22%2C%22until%22%3A%22{until}%22%7D)%7Bspend,actions,cpc,ctr%7D"
            f"&effective_status=%5B%22ACTIVE%22%2C%22PAUSED%22%5D"
            f"&limit=200"
        )

        resp = _req.get(url, params={'access_token': token}, timeout=25)
        data = resp.json()

        if 'error' in data:
            return jsonify([])

        result = []
        for ad in data.get('data', []):
            ins_list = ad.get('insights', {}).get('data', [])
            ins = ins_list[0] if ins_list else {}
            spend = float(ins.get('spend', 0))
            if spend <= 0:
                continue

            results = 0
            for act in ins.get('actions', []):
                if act.get('action_type') == action_field:
                    results = int(act.get('value', 0))
                    break

            # Skip ads with 0 conversions — useless as storyboard reference
            if results <= 0:
                continue

            cpr = round(spend / results, 2)
            campaign_info = ad.get('campaign', {})

            result.append({
                'ad_id': ad['id'],
                'ad_name': ad.get('name', ''),
                'short_name': short_name,
                'campaign_name': campaign_info.get('name', '') if campaign_info else '',
                'spend': spend,
                'results': results,
                'cpr': cpr,
                'cpc': float(ins.get('cpc', 0)) if ins.get('cpc') else 0,
                'ctr': float(ins.get('ctr', 0)) if ins.get('ctr') else 0,
                'status': ad.get('status', ''),
            })

        result.sort(key=lambda x: (-x['results'], x['cpr']))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e), 'data': []})


@app.route('/api/storyboard/ad-creative')
@admin_required
def api_storyboard_ad_creative():
    """Return creative details for an ad — thumbnail, video_id, title, body, etc."""
    try:
        ad_id = request.args.get('ad_id', '').strip()
        if not ad_id:
            return jsonify({'status': 'error', 'error': 'Ad ID diperlukan'})

        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        if not token:
            return jsonify({'status': 'error', 'error': 'Meta token tidak tersedia'})

        import subprocess, json as _json
        url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative%7Btitle,body,thumbnail_url,image_url,video_id,id,call_to_action_type,object_story_spec%7D,adcreatives%7Basset_feed_spec%7D&access_token={token}"
        r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url], capture_output=True, text=True, timeout=20)
        ad_data = _json.loads(r.stdout)

        if 'error' in ad_data:
            return jsonify({'status': 'error', 'error': ad_data['error'].get('message', 'Gagal fetch creative')})

        cr = ad_data.get('creative', {})
        # Try to get object_story_spec too
        oss = cr.get('object_story_spec', {})
        video_data = oss.get('video_data', {}) or {}

        return jsonify({
            'status': 'success',
            'data': {
                'ad_name': ad_data.get('name', ''),
                'title': cr.get('title', '') or video_data.get('title', '') or ad_data.get('name', ''),
                'body': cr.get('body', '') or video_data.get('body', '') or video_data.get('message', ''),
                'thumbnail_url': cr.get('thumbnail_url', '') or video_data.get('image_url', ''),
                'image_url': cr.get('image_url', ''),
                'video_id': cr.get('video_id', '') or video_data.get('video_id', ''),
                'call_to_action': cr.get('call_to_action_type', '') or video_data.get('call_to_action', {}).get('type', ''),
                'creative_id': cr.get('id', '')
            }
        })

    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/api/storyboard/analyze-video', methods=['POST'])
@admin_required
def api_storyboard_analyze_video():
    try:
        body = request.get_json() or {}
        ad_id = body.get('ad_id', '')
        ad_name = body.get('ad_name', '')
        account_id = body.get('account_id', '')
        short_name = body.get('short_name', '')

        if not ad_id:
            return jsonify({'status': 'error', 'error': 'Ad ID diperlukan'})

        # Get Meta token
        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass

        ad_reference = ''
        if token:
            import subprocess, json as _json
            try:
                url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative%7Btitle,body,thumbnail_url,image_url,video_id,id%7D,adcreatives%7Basset_feed_spec%7D&access_token={token}"
                r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url], capture_output=True, text=True, timeout=20)
                ad_data = _json.loads(r.stdout)
                cr = ad_data.get('creative', {})
                cr_title = cr.get('title', '') or ad_data.get('name', '') or ad_name
                cr_body = cr.get('body', '')
                ad_reference = f"Nama Iklan: {ad_data.get('name', ad_name)}\nHeadline: {cr_title}\nBody:\n{cr_body}"
            except:
                ad_reference = f"Nama Iklan: {ad_name}\nAkun: {short_name}\nCampaign ID: {ad_id}"
        else:
            ad_reference = f"Nama Iklan: {ad_name}\nAkun: {short_name}\nCampaign ID: {ad_id}"

        # Ambil DeepSeek key
        from config import DEEPSEEK_API_KEY
        api_key = DEEPSEEK_API_KEY
        if not api_key:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass
        if not api_key:
            try:
                with open('/home/u1734629/report_config.json', encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass
        if not api_key:
            return jsonify({'status': 'error', 'error': 'DeepSeek API key tidak tersedia'})

        prompt = f"""Kamu adalah creative analyst untuk video iklan produk Produk (multivitamin anak dari 5 bahan alami: mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan).

Analisis iklan ini untuk bahan remake. ATURAN PENTING:
- Analisis HANYA berdasarkan informasi yang ada di REFERENSI IKLAN di bawah ini.
- JANGAN menambahkan isu, manfaat, klaim, atau konten yang TIDAK ada di referensi.
- Kalau referensi tidak membahas masalah/isu (misal nafsu makan, speech delay), jangan mengarangnya.
- Kalau referensi cuma promo/harga, analisis fokus ke promo/harga itu.

Fokus pada:
1. **STRUCTURE & PACING** — Bagaimana alur iklan? Berapa scene? Berapa durasi tiap scene?
2. **HOOK** — Gimana cara opening menarik perhatian? Apa yang bikin viewer berhenti scroll?
3. **STORYTELLING** — Gimana alur cerita yang ADA di referensi (problem → solusi → hasil → CTA ATAU langsung promo/CTA)? Jelaskan sesuai isi referensi, jangan mengarang.
4. **VISUAL** — Adegan apa yang ditampilkan? Gimana angle kamera, transisi, dan komposisi?
5. **VO & COPY** — Naskah yang digunakan, tone bicara, kata-kata kunci yang efektif
6. **CTA** — Call to action dan urgency yang dipakai
7. **STRENGTHS** — Apa yang bikin iklan ini work (berdasarkan data performa)
8. **WEAKNESSES** — Apa yang kurang dan bisa diperbaiki di versi remake
9. **SUGGESTED IMPROVEMENTS** — Saran spesifik untuk versi remake

REFERENSI IKLAN:
{ad_reference}

Output dalam Bahasa Indonesia, format naratif dengan bullet points per bagian. Analisis harus detail dan actionable buat videographer."""

        import urllib.request, json as _json
        data = _json.dumps({
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.7,
            'max_tokens': 2000
        }).encode()

        req = urllib.request.Request(
            'https://api.deepseek.com/v1/chat/completions',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = _json.loads(resp.read().decode())
        analysis = result['choices'][0]['message']['content']

        return jsonify({
            'status': 'success',
            'analysis': analysis,
            'disclaimer': 'Analisis berdasarkan TEKS iklan (judul + body copy), bukan footage video atau suara/VO. Cocok untuk remake yang fokus pada cerita/teks.'
        })

    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/api/storyboard/generate', methods=['POST'])
@admin_required
def api_storyboard_generate():
    try:
        body = request.get_json() or {}
        mode = body.get('mode', 'referensi')
        ad_reference = body.get('ad_reference', '').strip()
        ad_id = body.get('ad_id', '')
        ad_name = body.get('ad_name', '')
        account_id = body.get('account_id', '')
        short_name = body.get('short_name', '')
        custom_prompt = body.get('prompt', '').strip()
        video_analysis = body.get('video_analysis', '')

        # Konteks produk dari dropdown Data Produk. Kosong → teks default (perilaku lama persis,
        # byte-identik dengan sebelum fitur ini ada).
        _pctx = _generator_product_ctx(body.get('product_id'))
        _p_brand = _pctx['brand'] or 'Produk'
        _p_kelas = _pctx['desc'] or 'multivitamin anak dari 5 bahan alami: mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan'

        # Ambil Meta token & DeepSeek key di awal (dipakai untuk auto-analyze video)
        token = META_TOKEN or ''
        if not token:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    token = cfg.get('meta_token', '')
            except:
                pass
        api_key = ''
        try:
            from config import DEEPSEEK_API_KEY
            api_key = DEEPSEEK_API_KEY
        except:
            pass
        if not api_key:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass
        if not api_key:
            try:
                with open('/home/u1734629/report_config.json', encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass

        # MODE 2: Custom prompt — langsung dari user
        if mode == 'custom':
            if not custom_prompt:
                return jsonify({'status': 'error', 'error': 'Prompt kosong. Tulis dulu ide/hook lo.'})
            import re as _re2

            # Deteksi HOOK & CTA yang ditulis eksplisit ("Hook: ...", "CTA: ...")
            _hook_wajib = ''
            _mh = _re2.search(r'(?im)^\s*(?:hook|hok|pembuka|opening|3 detik pertama)\s*[:=]\s*(.+)$', custom_prompt)
            if _mh:
                _hook_wajib = _mh.group(1).strip().strip('"').strip("'").strip()
            _cta_wajib = ''
            _mcta = _re2.search(r'(?im)^\s*(?:cta|call to action|penutup|closing)\s*[:=]\s*(.+)$', custom_prompt)
            if _mcta:
                _cta_wajib = _mcta.group(1).strip().strip('"').strip("'").strip()

            _hook_rule = ''
            if _hook_wajib:
                _hook_rule = ('\n🎯 HOOK YANG DIMINTA USER — WAJIB PERSIS & WAJIB DI SCENE PERTAMA:\n'
                              '   "' + _hook_wajib + '"\n'
                              '   → Teks ini HARUS jadi baris tabel PERTAMA, durasi mulai dari 0s.\n'
                              '   → DILARANG mengganti hook ini dengan hook buatan kamu sendiri.\n'
                              '   → DILARANG memindahkannya ke scene 2/3/dst atau ke tengah.\n'
                              '   → Boleh dirapikan tanda baca/ejaannya, tapi MAKNA dan KATA KUNCINYA wajib sama.\n'
                              '   → VO di scene pertama = hook ini (bukan narasi masalah/edukasi/produk).\n')
            _cta_rule = ''
            if _cta_wajib:
                _cta_rule = ('\n🎯 CTA YANG DIMINTA USER — WAJIB di scene TERAKHIR:\n'
                             '   "' + _cta_wajib + '"\n')

            base_prompt_part = f"""Kamu adalah creative director senior untuk video iklan komersial {_p_brand} ({_p_kelas}).

=====================================================================
PERINTAH USER — INI PERINTAH MENGIKAT, BUKAN BAHAN MENTAH
=====================================================================
{custom_prompt}
=====================================================================
{_hook_rule}{_cta_rule}
ATURAN MUTLAK MENURUTI PERINTAH USER (melanggar = hasil ditolak):

1. **HOOK WAJIB DI SCENE PERTAMA (0-3 detik).** DILARANG menaruh hook di scene kedua, ketiga, atau di tengah. Baris tabel PERTAMA wajib hook, dan kolom Durasi baris pertama wajib mulai dari "0-3s" atau "0-4s".
2. **PAKAI PERSIS APA YANG DIMINTA USER.** Kalau user sudah menuliskan hook / kalimat / angle tertentu, PAKAI ITU APA ADANYA. JANGAN mengganti dengan versi kamu, JANGAN menggeser ke belakang, JANGAN mengubah topiknya.
3. **JANGAN MENGABSTRAKSI / MELEMBUTKAN PERINTAH USER.** Apa yang user tulis = isi iklannya. Bukan bahan untuk diterjemahkan jadi cerita lain.
4. **URUTAN WAJIB:** (a) HOOK di 0-3s sesuai permintaan user → (b) masalah yang nyambung ke hook → (c) edukasi singkat → (d) {_p_brand} muncul sekitar 40-50% durasi → (e) bukti/perubahan setelah pakai → (f) CTA di scene terakhir.
   DILARANG memulai tabel dengan masalah, edukasi, produk, atau CTA. Baris pertama HARUS hook.
5. Kalau user minta jumlah scene / durasi tertentu (misal "5 scene", "30 detik"), IKUTI angka itu. Kalau tidak disebut, pakai 6-8 scene total 30-45 detik.
6. Bagian yang tidak disebut user baru boleh kamu isi sendiri — tapi tidak boleh melanggar poin 1-5."""

        # MODE 1: Referensi iklan — existing flow
        else:
            # If ad_id provided, fetch creative from Meta API
            if ad_id and not ad_reference:
                # Get Meta token
                token = META_TOKEN or ''
                if not token:
                    try:
                        with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                            cfg = json.load(f)
                            token = cfg.get('meta_token', '')
                    except:
                        pass

                if token:
                    import subprocess, json as _json
                    try:
                        # Fetch ad creative directly
                        url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative%7Btitle,body,thumbnail_url,image_url,video_id,id%7D&access_token={token}"
                        r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url], capture_output=True, text=True, timeout=20)
                        ad_data = _json.loads(r.stdout)
                        cr = ad_data.get('creative', {})
                        cr_title = cr.get('title', '') or ad_data.get('name', '') or ad_name
                        cr_body = cr.get('body', '')
                        ad_reference = f"Nama Iklan: {ad_data.get('name', ad_name)}\nHeadline: {cr_title}\nBody:\n{cr_body}"
                    except:
                        ad_reference = f"Nama Iklan: {ad_name}\nAkun: {short_name}\nCampaign ID: {ad_id}"
                else:
                    ad_reference = f"Nama Iklan: {ad_name}\nAkun: {short_name}\nCampaign ID: {ad_id}"

            if not ad_reference:
                return jsonify({'status': 'error', 'error': 'Referensi iklan kosong'})

            # AUTO-ANALYZE VIDEO: kalau frontend belum kirim hasil analisis,
            # generate analisis video dulu otomatis — biar storyboard berbasis
            # analisis video asli, bukan cuma judul/body iklan.
            if not video_analysis and ad_id:
                try:
                    import subprocess, json as _json
                    # Fetch creative untuk bahan analisis
                    url = f"https://graph.facebook.com/v26.0/{ad_id}?fields=name,creative%7Btitle,body,thumbnail_url,image_url,video_id,id%7D,adcreatives%7Basset_feed_spec%7D&access_token={token}"
                    r = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url], capture_output=True, text=True, timeout=20)
                    ad_data = _json.loads(r.stdout)
                    cr = ad_data.get('creative', {})
                    cr_title = cr.get('title', '') or ad_data.get('name', '') or ad_name
                    cr_body = cr.get('body', '')
                    ad_ref = f"Nama Iklan: {ad_data.get('name', ad_name)}\nHeadline: {cr_title}\nBody:\n{cr_body}"

                    # Prompt analisis (sama dengan /api/storyboard/analyze-video)
                    analyze_prompt = f"""Kamu adalah creative analyst untuk video iklan produk Produk (multivitamin anak dari 5 bahan alami: mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan).

Analisis iklan ini untuk bahan remake. ATURAN PENTING:
- Analisis HANYA berdasarkan informasi yang ada di REFERENSI IKLAN di bawah ini.
- JANGAN menambahkan isu, manfaat, klaim, atau konten yang TIDAK ada di referensi.
- Kalau referensi tidak membahas masalah/isu (misal nafsu makan, speech delay), jangan mengarangnya.
- Kalau referensi cuma promo/harga, analisis fokus ke promo/harga itu.

Fokus pada:
1. **STRUCTURE & PACING** — Bagaimana alur iklan? Berapa scene? Berapa durasi tiap scene?
2. **HOOK** — Gimana cara opening menarik perhatian? Apa yang bikin viewer berhenti scroll?
3. **STORYTELLING** — Gimana alur cerita yang ADA di referensi (problem → solusi → hasil → CTA ATAU langsung promo/CTA)? Jelaskan sesuai isi referensi, jangan mengarang.
4. **VISUAL** — Adegan apa yang ditampilkan? Gimana angle kamera, transisi, dan komposisi?
5. **VO & COPY** — Naskah yang digunakan, tone bicara, kata-kata kunci yang efektif
6. **CTA** — Call to action dan urgency yang dipakai
7. **STRENGTHS** — Apa yang bikin iklan ini work (berdasarkan data performa)
8. **WEAKNESSES** — Apa yang kurang dan bisa diperbaiki di versi remake
9. **SUGGESTED IMPROVEMENTS** — Saran spesifik untuk versi remake

REFERENSI IKLAN:
{ad_ref}

Output dalam Bahasa Indonesia, format naratif dengan bullet points per bagian. Analisis harus detail dan actionable buat videographer."""

                    import urllib.request as _ureq
                    _data = _json.dumps({
                        'model': 'deepseek-chat',
                        'messages': [{'role': 'user', 'content': analyze_prompt}],
                        'temperature': 0.7,
                        'max_tokens': 2000
                    }).encode()
                    _req = _ureq.Request(
                        'https://api.deepseek.com/v1/chat/completions',
                        data=_data,
                        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
                    )
                    _resp = _ureq.urlopen(_req, timeout=60)
                    _result = _json.loads(_resp.read().decode())
                    video_analysis = _result['choices'][0]['message']['content']
                except Exception as _e:
                    video_analysis = ''

            if video_analysis:
                base_prompt_part = f"""Kamu adalah creative writer untuk video iklan produk {_p_brand} ({_p_kelas}).

Buat storyboard video iklan REMADE/ULANG berdasarkan ANALISIS VIDEO REFERENSI di bawah ini. Gunakan hasil analisis sebagai sumber UTAMA untuk struktur, hook, storytelling, visual, VO, dan CTA — JANGAN hanya mengandalkan judul/body iklan.

ANALISIS VIDEO REFERENSI:
{video_analysis}

REFERENSI IKLAN (metadata pendukung):
{ad_reference}"""
            else:
                base_prompt_part = f"""Kamu adalah creative writer untuk video iklan produk {_p_brand} ({_p_kelas}).

Buat storyboard video iklan REMADE/ULANG berdasarkan referensi iklan di bawah ini.

REFERENSI IKLAN:
{ad_reference}"""

        # --- COMMON — output format & panduan untuk kedua mode ---
        output_instruction = """
OUTPUT FORMAT — PILIH SALAH SATU berdasarkan jenis iklan, JANGAN terpaku 1 format:

**Deteksi format dari ISI brief, bukan cuma kata kunci:**
- Kalau brief mengandung UNSUR PROMO/PENAWARAN (diskon, harga, potongan ongkir, bayar di tempat, gratis, "lebih murah", bonus, penawaran khusus) WALAU kata "promo" tidak disebut eksplisit → pakai FORMAT PROMO.
- Kalau brief bercerita/emosional (kekhawatiran, cerita orang tua, perkembangan anak, transformasi, testi) → pakai FORMAT STORYTELLING.
- Kalau ragu, pilih format yang paling sesuai isi brief — jangan monoton.

=== FORMAT A — STORYTELLING / EMOSIONAL (4 kolom) ===
Markdown table dengan pipe, 4 kolom:

| Durasi | Visual / Footage | VO / Narasi | Text On Screen |
| --- | --- | --- | --- |
| 0-4s | [deskripsi visual jelas] | [naskah VO natural] | [teks overlay pendek] |
| 4-10s | [deskripsi visual jelas] | [naskah VO natural] | [teks overlay pendek] |

- 6-8 scene, timing GRANULAR KONTINU (0-4s, 4-10s, 10-17s, dst.) total 35-45 detik.
- Text On Screen WAJIB diisi tiap scene (overlay: "Dulu, Mama Sempat Khawatir...", "BEFORE → AFTER", "GENEROS", "PESAN SEKARANG", dll).
- Alur: BEFORE → kekhawatiran → usaha orang tua → PRODUK muncul di scene pertengahan (~40-50% durasi) → AFTER (split screen BEFORE→AFTER) → kebahagiaan → CTA.
- Hook 3 detik pertama yang nusuk (pertanyaan/momen yang bikin stop scroll).

=== FORMAT B — PROMO / HARGA (3 kolom + Highlight) ===
Markdown table dengan pipe, 3 kolom:

| Durasi | Footage | VO |
| --- | --- | --- |
| 0-3s | [deskripsi visual jelas] | [naskah VO natural] |
| 3-7s | [deskripsi visual jelas] | [naskah VO natural] |

- 6-8 scene, total 30-45 detik.
- Setelah tabel, TAMBAH section terpisah:
## Highlight teks di layar
- **KIRIM LANGSUNG DARI PABRIK**
- **DISKON 30%**
- **POTONGAN ONGKIR 40RB**
- **BISA BAYAR DI TEMPAT**
- **PESAN SEKARANG**
  (isi sesuai benefit di brief — tiap benefit jadi 1 bullet bold)
- Semua benefit WAJIB masuk VO cepat (bukan cuma di akhir), diulang di montage penutup.
- Boleh pilih: 3 kolom + highlight list, ATAU 4 kolom dengan Text On Screen ber-emoji (🔥🚚💰👉).

=== WAJIB UNTUK KEDUA FORMAT ===
1. Setelah tabel, kasih blok **"🔥 Hook alternatif yang lebih kuat"** — 1-2 opsi hook di luar tabel (bukan ganti isi tabel utama).
2. Setelah hook alternatif, kasih blok **"📋 Catatan produksi"** — saran footage asli/testimoni + compliance.
3. ATURAN PENTING & COMPLIANCE:
   - **IKUTI TOPIK BRIEF SECARA PERSIS**: Jika user meminta topik spesifik (seperti "stunting", "speech delay", dll), **WAJIB** buat storyboard tepat mengenai topik tersebut. JANGAN mengubah atau melarikan topik ke masalah lain (misal: jangan mengubah stunting jadi susah makan).
   - Produk = multivitamin anak (5 bahan alami: mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan).
   - JANGAN klaim produk menyembuhkan kondisi medis berat secara instan — gunakan bahasa edukasi "mendukung pencegahan stunting, optimalisasi tumbuh kembang, dan pemenuhan nutrisi harian".
   - JANGAN menambahkan isu/klaim di luar konteks brief user.
4. PANJANG VO WAJIB SESUAI DURASI. Rumus: 1 detik = 2-2.5 kata.
   Ketentuan ketat:
   * 3s → WAJIB 6-8 kata
   * 5s → WAJIB 10-13 kata (1-2 kalimat sangat pendek)
   * 7-8s → WAJIB 14-20 kata (1-2 kalimat sedang)
   * 10s → WAJIB 20-25 kata (2-3 kalimat)
   * 12-15s → WAJIB 25-35 kata (2-4 kalimat)
   JANGAN MELEBIHI batas kata — VO yang kepanjangan useless buat videographer.
5. VO dalam Bahasa Indonesia natural, santai, emosional (kayak ngobrol). Penekanan bisa pakai *italic*.
6. Footage deskripsi visual yang JELAS buat videographer (WAJIB diisi, jangan kosong).
7. Sertakan call-to-action di scene terakhir (PESAN SEKARANG / klik link di bio)."""

        # Kalau produk dipilih dari dropdown → sisipkan konteks produk + ganti baris compliance
        # "Produk = ..." biar LLM-nya nggak balik ke deskripsi default.
        if _pctx['product']:
            _prod_line = f"Produk = {_p_brand}"
            if _pctx['desc']:
                _prod_line += f" ({_pctx['desc']})"
            if _pctx['benefits']:
                _prod_line += ' — manfaat: ' + '; '.join(_pctx['benefits'])
            _prod_line += '.'
            if _pctx['promo']:
                _prod_line += f"\n   - Promo/penawaran aktif: {_pctx['promo']}"
            output_instruction = output_instruction.replace(
                'Produk = multivitamin anak (5 bahan alami: mengkudu, temulawak, ikan sidat, daun pegagan, madu hutan).',
                _prod_line
            )
            base_prompt_part = base_prompt_part + _pctx['block']

        prompt = base_prompt_part + output_instruction

        # MODE CUSTOM: tempel CHECK TERAKHIR di ujung prompt — instruksi terakhir
        # punya pengaruh paling kuat ke LLM, jadi aturan hook diulang di sini.
        if mode == 'custom':
            _cek_hook = ''
            try:
                if _hook_wajib:
                    _cek_hook = ('\n2. Hook user yang diminta: "' + _hook_wajib + '"\n'
                                 '   → Baris pertama WAJIB memuat makna hook ini. Kalau belum → GANTI baris pertama.\n')
            except NameError:
                _cek_hook = ''
            # Kalau user TIDAK menulis label Hook:/Alur:, kemungkinan isinya kutipan
            # berita/tren (dari fitur "Angle dari Tren Kreatif") → tetap butuh aturan
            # abstraksi detail instansi. Tapi TIDAK boleh mengurangi kepatuhan perintah.
            _catatan_berita = ''
            try:
                if not _hook_wajib:
                    _catatan_berita = ('\nCATATAN KONTEKS: Kalau teks di atas sebenarnya kutipan '
                                       'berita/artikel/tren (bukan instruksi langsung), ambil '
                                       'ESENSI/intisarinya saja. JANGAN tampilkan nama kampus, '
                                       'universitas, daerah, atau instansi secara literal — ubah '
                                       'jadi konteks umum (mis. "info parenting terbaru"). '
                                       'Tapi aturan 1-6 di atas TETAP berlaku: hook wajib di '
                                       'scene pertama.\n')
            except NameError:
                _catatan_berita = ''
            prompt += _catatan_berita + """

=========================================================
CHECK TERAKHIR SEBELUM KIRIM — WAJIB DICEK SENDIRI
=========================================================
1. Baris tabel PERTAMA: apakah itu HOOK? Apakah kolom Durasi baris pertama mulai dari 0s? (0-3s / 0-4s)
""" + _cek_hook + """3. Apakah ada hook yang "nyempil" di tengah (scene 3-5)? Kalau ada → SALAH, pindahkan ke baris pertama.
4. Baris pertama DILARANG berisi masalah / edukasi / produk / CTA — itu untuk scene berikutnya.
5. Kalau poin 1-4 belum benar, PERBAIKI DULU sebelum mengirim jawaban."""

        # Ambil DeepSeek key dari config
        from config import DEEPSEEK_API_KEY
        api_key = DEEPSEEK_API_KEY
        if not api_key:
            try:
                with open(os.path.join(os.path.dirname(__file__), 'config.json'), encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass
        if not api_key:
            try:
                with open('/home/u1734629/report_config.json', encoding='utf-8') as f:
                    cfg = json.load(f)
                    api_key = cfg.get('deepseek_api_key', '')
            except:
                pass
        if not api_key:
            return jsonify({'status': 'error', 'error': 'DeepSeek API key tidak tersedia'})

        import urllib.request, json as _json
        data = _json.dumps({
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.7,
            'max_tokens': 6000
        }).encode()

        req = urllib.request.Request(
            'https://api.deepseek.com/v1/chat/completions',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            }
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = _json.loads(resp.read().decode())
        storyboard_text = result['choices'][0]['message']['content']

        # Buat .docx — coba pake python-docx, fallback ke manual XML
        try:
            from docx import Document
            from docx.shared import Pt, Cm
            from docx.oxml.ns import qn, nsdecls
            from docx.oxml import parse_xml
            HAVE_DOCX = True
        except ImportError:
            HAVE_DOCX = False

        from datetime import datetime

        def parse_storyboard_table(text):
            """Parse markdown table dari LLM output — handle 3 atau 4 kolom."""
            lines = text.strip().split('\n')
            table_data = []
            in_table = False
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    if in_table:
                        break  # empty line = end of table
                    continue
                # Detect separator line: --- | --- etc
                if '|' in stripped and all(c.strip() in ('', '-', ':', '---', '----', '----') or c.strip().startswith('---') or c.strip().startswith(':') for c in stripped.split('|')):
                    # Count actual separator parts (minus empty)
                    parts = [p.strip() for p in stripped.split('|') if p.strip()]
                    if parts and all(p.replace('-','').replace(':','') == '' for p in parts):
                        in_table = True
                        continue
                if in_table and '|' in stripped:
                    # Clean leading/trailing pipe
                    clean = stripped
                    if clean.startswith('|'): clean = clean[1:]
                    if clean.endswith('|'): clean = clean[:-1]
                    cells = [c.strip() for c in clean.split('|')]
                    # Skip empty rows
                    if all(c == '' for c in cells):
                        continue
                    if len(cells) >= 4:
                        table_data.append(cells[:4])
                    elif len(cells) >= 3:
                        table_data.append(cells[:3])
            return table_data

        def parse_storyboard_sections(text):
            """Parse section non-tabel: highlight list, hook alternatif, catatan produksi.
            Return dict {highlights: [], alt_hooks: [], production_notes: []}.
            Robust terhadap variasi header (##, ###, **Highlight**, dll)."""
            highlights, alt_hooks, notes = [], [], []
            current = None
            for line in text.split('\n'):
                low = line.strip().lower()
                if 'highlight' in low and ('teks di layar' in low or 'text on screen' in low or 'di layar' in low):
                    current = 'highlights'
                    continue
                if 'hook alternatif' in low or 'hook yang lebih kuat' in low or 'alternatif hook' in low:
                    current = 'alt_hooks'
                    continue
                if 'catatan produksi' in low or 'catatan production' in low or 'production note' in low or 'catatan:' in low:
                    current = 'notes'
                    continue
                if not line.strip():
                    # Baris kosong TIDAK menghentikan section — ChatGPT sering
                    # kasih blank line antara header dan list. Lanjutkan aja.
                    continue
                # Baris header lain (##, ###, **Header**) menghentikan section saat ini
                stripped = line.strip()
                if stripped.startswith('#') or (stripped.startswith('**') and stripped.endswith('**') and len(stripped) < 60):
                    current = None
                    continue
                if current:
                    clean = stripped.lstrip('-*•').strip()
                    # Hilangkan numbering (1., 2., dst)
                    import re as _re
                    clean = _re.sub(r'^\d+[\.\)]\s*', '', clean).strip()
                    if clean.startswith('**') and clean.endswith('**'):
                        clean = clean[2:-2]
                    # Skip heading-like / numbering
                    if not clean:
                        continue
                    if current == 'highlights':
                        highlights.append(clean)
                    elif current == 'alt_hooks':
                        alt_hooks.append(clean)
                    elif current == 'notes':
                        notes.append(clean)
            return {'highlights': highlights, 'alt_hooks': alt_hooks, 'production_notes': notes}

        now_str = datetime.now().strftime('%d %B %Y %H:%M')

        # — Parse storyboard table + sections —
        table_data = parse_storyboard_table(storyboard_text)
        sections = parse_storyboard_sections(storyboard_text)

        # — Determine table column headers (3 vs 4 kolom) —
        def _detect_headers():
            if table_data and len(table_data[0]) >= 4:
                return ['Durasi', 'Visual / Footage', 'VO / Narasi', 'Text On Screen']
            return ['Durasi', 'VO', 'Footage']
        table_headers = _detect_headers()
        n_cols = len(table_headers)

        # — Generate DOCX (only if python-docx available) —
        filename = None
        if HAVE_DOCX:
            doc = Document()

            # Default font
            style = doc.styles['Normal']
            font = style.font
            font.name = 'Calibri'
            font.size = Pt(11)

            # — Title —
            title = doc.add_heading(f'Storyboard Remake — {_p_brand}', level=1)
            title.alignment = 1  # center

            # — Date —
            date_para = doc.add_paragraph()
            date_para.alignment = 0
            run = date_para.add_run(f'Dibuat: {now_str}')
            run.italic = True
            run.font.size = Pt(10)

            doc.add_paragraph()  # spacer

            if table_data:
                table = doc.add_table(rows=1 + len(table_data), cols=n_cols)
                table.alignment = 1  # center

                # Set table width to full page
                tbl = table._tbl
                tblPr = tbl.tblPr if tbl.tblPr is not None else parse_xml(f'<w:tblPr {nsdecls("w")}/>')
                tblW = parse_xml(f'<w:tblW {nsdecls("w")} w:w="9000" w:type="dxa"/>')
                tblPr.append(tblW)

                # Table borders (tebal)
                borders = parse_xml(
                    f'<w:tblBorders {nsdecls("w")}>'
                    '<w:top w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                    '<w:bottom w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                    '<w:left w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                    '<w:right w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                    '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="636e72"/>'
                    '<w:insideV w:val="single" w:sz="8" w:space="0" w:color="636e72"/>'
                    '</w:tblBorders>'
                )
                tblPr.append(borders)

                # Column widths
                if n_cols >= 4:
                    col_widths = [800, 3400, 3200, 1600]
                else:
                    col_widths = [800, 4000, 4200]
                tblGrid = tblPr.find(qn('w:tblGrid'))
                if tblGrid is None:
                    tblGrid = parse_xml(f'<w:tblGrid {nsdecls("w")}/>')
                    tblPr.append(tblGrid)
                for cw in col_widths:
                    gridCol = parse_xml(f'<w:gridCol {nsdecls("w")} w:w="{cw}"/>')
                    tblGrid.append(gridCol)

                # — Header row —
                headers = table_headers
                header_row = table.rows[0]
                for i, h in enumerate(headers):
                    cell = header_row.cells[i]
                    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="6c5ce7" w:val="clear"/>')
                    cell._tc.get_or_add_tcPr().append(shading)
                    p = cell.paragraphs[0]
                    p.alignment = 1
                    run = p.add_run(h)
                    run.bold = True
                    run.font.size = Pt(11)
                    from docx.shared import RGBColor
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

                # — Data rows — (bersihkan ** marker dari sel)
                for ri, row_data in enumerate(table_data):
                    row = table.rows[ri + 1]
                    for ci, cell_val in enumerate(row_data):
                        cell = row.cells[ci]
                        p = cell.paragraphs[0]
                        if ci == 0:  # Durasi — center
                            p.alignment = 1
                        run = p.add_run(cell_val.replace('**', ''))
                        run.font.size = Pt(10)

                # Set cell widths
                for row in table.rows:
                    for i, cell in enumerate(row.cells):
                        if i < len(col_widths):
                            tc = cell._tc
                            tcPr = tc.get_or_add_tcPr()
                            tcW = parse_xml(f'<w:tcW {nsdecls("w")} w:w="{col_widths[i]}" w:type="dxa"/>')
                            tcPr.append(tcW)
            else:
                doc.add_paragraph(storyboard_text)

            # — Sections (highlight, hook alternatif, catatan produksi) —
            def _clean_sb_item(item):
                """Bersihkan ** marker & emoji dari item section."""
                clean = item.replace('**', '').strip()
                return clean

            if sections['highlights']:
                doc.add_paragraph()
                doc.add_heading('Highlight Teks di Layar', level=2)
                for item in sections['highlights']:
                    p = doc.add_paragraph(_clean_sb_item(item), style='List Bullet')
                    for run in p.runs:
                        run.bold = True
            if sections['alt_hooks']:
                doc.add_paragraph()
                doc.add_heading('Hook Alternatif', level=2)
                for item in sections['alt_hooks']:
                    doc.add_paragraph(_clean_sb_item(item), style='List Bullet')
            if sections['production_notes']:
                doc.add_paragraph()
                doc.add_heading('Catatan Produksi', level=2)
                for item in sections['production_notes']:
                    doc.add_paragraph(_clean_sb_item(item), style='List Bullet')

            # — Margins —
            section = doc.sections[0]
            section.top_margin = Cm(2.0)
            section.bottom_margin = Cm(2.0)
            section.left_margin = Cm(2.0)
            section.right_margin = Cm(2.0)

            # — Save —
            output_dir = os.path.join(os.path.dirname(__file__), 'tmp')
            os.makedirs(output_dir, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'Storyboard_Remake_{ts}.docx'
            filepath = os.path.join(output_dir, filename)
            doc.save(filepath)
        else:
            # Fallback: manual XML DOCX (python-docx tidak tersedia)
            from zipfile import ZipFile
            import io as _io

            col_widths = [500, 800, 3800, 3900]
            headers = ['Scene', 'Durasi', 'VO', 'Footage']

            body_parts = []
            body_parts.append(f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:sz w:val="36"/><w:b/><w:color w:val="6c5ce7"/></w:rPr><w:t>Storyboard Remake — {_p_brand}</w:t></w:r></w:p>')
            body_parts.append(f'<w:p><w:r><w:rPr><w:i/><w:sz w:val="20"/></w:rPr><w:t>Dibuat: {now_str}</w:t></w:r></w:p>')
            body_parts.append('<w:p><w:r><w:t> </w:t></w:r></w:p>')

            if table_data:
                tbl = '<w:tbl>'
                tbl += '<w:tblPr><w:tblW w:w="9000" w:type="dxa"/><w:tblBorders>'
                tbl += '<w:top w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                tbl += '<w:bottom w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                tbl += '<w:left w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                tbl += '<w:right w:val="single" w:sz="12" w:space="0" w:color="2d3436"/>'
                tbl += '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="636e72"/>'
                tbl += '<w:insideV w:val="single" w:sz="8" w:space="0" w:color="636e72"/>'
                tbl += '</w:tblBorders></w:tblPr><w:tblGrid>'
                for cw in col_widths:
                    tbl += f'<w:gridCol w:w="{cw}"/>'
                tbl += '</w:tblGrid>'

                # Header
                tbl += '<w:tr>'
                for ci, h in enumerate(headers):
                    tbl += f'<w:tc><w:tcPr><w:shd w:fill="6c5ce7" w:val="clear"/><w:tcW w:w="{col_widths[ci]}" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="22"/><w:color w:val="FFFFFF"/></w:rPr><w:t>{h}</w:t></w:r></w:p></w:tc>'
                tbl += '</w:tr>'

                # Data rows
                for ri, row_data in enumerate(table_data):
                    tbl += '<w:tr>'
                    # If 3 columns data (Durasi|VO|Footage), prepend scene number
                    if len(row_data) == 3:
                        row_data_display = [str(ri + 1), row_data[0], row_data[1], row_data[2]]
                    else:
                        row_data_display = list(row_data)
                    for ci, cell_val in enumerate(row_data_display):
                        val = str(cell_val).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        jc = 'center' if ci in (0, 1) else 'left'
                        tbl += f'<w:tc><w:tcPr><w:tcW w:w="{col_widths[ci]}" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:jc w:val="{jc}"/></w:pPr><w:r><w:rPr><w:sz w:val="20"/></w:rPr><w:t>{val}</w:t></w:r></w:p></w:tc>'
                    tbl += '</w:tr>'
                tbl += '</w:tbl>'
                body_parts.append(tbl)
            else:
                body_parts.append(f'<w:p><w:r><w:t>{storyboard_text}</w:t></w:r></w:p>')

            body_xml = '<w:body>' + ''.join(body_parts) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body>'
            doc_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">{body_xml}</w:document>'

            ct = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
            rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'

            output_dir = os.path.join(os.path.dirname(__file__), 'tmp')
            os.makedirs(output_dir, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'Storyboard_Remake_{ts}.docx'
            filepath = os.path.join(output_dir, filename)

            buf = _io.BytesIO()
            with ZipFile(buf, 'w') as zf:
                zf.writestr('[Content_Types].xml', ct.encode('utf-8'))
                zf.writestr('_rels/.rels', rels.encode('utf-8'))
                zf.writestr('word/document.xml', doc_xml.encode('utf-8'))
            with open(filepath, 'wb') as f:
                f.write(buf.getvalue())

        return jsonify({
            'status': 'success',
            'storyboard': storyboard_text,
            'download_url': f'/api/storyboard/download/{filename}' if filename else None,
            'sections': sections,
            'table_headers': table_headers,
            'n_cols': n_cols
        })

    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/api/storyboard/download/<filename>')
@admin_required
def api_storyboard_download(filename):
    try:
        filepath = os.path.join(os.path.dirname(__file__), 'tmp', filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File tidak ditemukan'}), 404
        return open(filepath, 'rb').read(), 200, {
            'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/storyboard/upload-drive', methods=['POST'])
@admin_required
def api_storyboard_upload_drive():
    """Upload storyboard DOCX to Google Drive Creative Remake folder."""
    try:
        body = request.get_json() or {}
        filename = body.get('filename', '')
        if not filename:
            return jsonify({'status': 'error', 'error': 'filename required'}), 400

        filepath = os.path.join(os.path.dirname(__file__), 'tmp', filename)
        if not os.path.exists(filepath):
            return jsonify({'status': 'error', 'error': 'File tidak ditemukan'}), 404

        # Load OAuth
        gd_path = os.path.join(os.path.dirname(__file__), 'gdrive_oauth.json')
        if not os.path.exists(gd_path):
            return jsonify({'status': 'error', 'error': 'Google Drive credentials tidak ditemukan'}), 500

        with open(gd_path, encoding='utf-8') as f:
            creds = json.load(f)

        # Refresh token
        import requests as _req
        rr = _req.post(creds['token_uri'], data={
            'client_id': creds['client_id'],
            'client_secret': creds['client_secret'],
            'refresh_token': creds['refresh_token'],
            'grant_type': 'refresh_token'
        }, timeout=15)
        at = rr.json()['access_token']

        # Upload to shared drive
        drive_id = '0AAVoojLkaE6aUk9PVA'
        metadata = json.dumps({'name': filename, 'parents': [drive_id]})
        boundary = f"==={os.urandom(8).hex()}==="

        with open(filepath, 'rb') as f:
            file_bytes = f.read()

        body_parts = []
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: application/json; charset=UTF-8")
        body_parts.append("")
        body_parts.append(metadata)
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        body_parts.append("")
        body_parts.append("")

        body_bytes = "\r\n".join(body_parts).encode("utf-8")
        body_bytes = body_bytes[:-2]
        body_bytes += b"\r\n" + file_bytes + f"\r\n--{boundary}--\r\n".encode()

        resp = _req.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
            headers={"Authorization": f"Bearer {at}", "Content-Type": f"multipart/related; boundary={boundary}"},
            data=body_bytes, timeout=60
        )

        if resp.status_code == 200:
            fid = resp.json()['id']
            # Make publicly viewable (anyone with link)
            try:
                _req.post(
                    f"https://www.googleapis.com/drive/v3/files/{fid}/permissions",
                    headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
                    params={"supportsAllDrives": "true"},
                    json={"type": "anyone", "role": "reader"},
                    timeout=10
                )
            except:
                pass
            # Get webViewLink
            try:
                info = _req.get(
                    f"https://www.googleapis.com/drive/v3/files/{fid}",
                    headers={"Authorization": f"Bearer {at}"},
                    params={"fields": "webViewLink", "supportsAllDrives": "true"},
                    timeout=10
                ).json()
                link = info.get('webViewLink', '')
            except:
                link = ''
            return jsonify({
                'status': 'success',
                'drive_id': fid,
                'drive_link': link,
                'message': f'✅ Terupload ke Creative Remake!'
            })
        else:
            return jsonify({'status': 'error', 'error': resp.text[:300]}), 500

    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


# ===== BETA: ROAS Dashboard (Google Sheets integration) =====
# Halaman terpisah — jangan deploy ke live sebelum selesai

def _parse_rp(val):
    """Parse 'Rp1.250.000,00' → 1250000.0"""
    if not val or not str(val).strip():
        return 0.0
    s = str(val).replace('Rp', '').replace('.', '').replace(',', '.').strip()
    try:
        return float(s)
    except:
        return 0.0

def _parse_int(val):
    """Parse angka — handle 0 atau string."""
    if not val or not str(val).strip():
        return 0
    try:
        return int(float(str(val).replace('.', '').replace(',', '.')))
    except:
        return 0

def _fetch_sheet_data(range_str=None):
    """Baca data dari Google Sheet ADV Gilang."""
    import subprocess, json
    script = os.path.expanduser('~/.hermes/skills/productivity/google-workspace/scripts/google_api.py')

    SHEET_ID = '18TjRUsmgM4Wf70IF2WjuCNRqplvXM-wpGAJMCJBwxeg'
    rng = range_str or 'ADV Gilang!A1:Z400'
    cmd = ['python3', script, 'sheets', 'get', SHEET_ID, rng]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {'error': f'Sheet fetch failed: {r.stderr[:200]}'}

    data = json.loads(r.stdout)
    return data

def _process_sheet_rows(raw_data):
    """Process raw sheet data into structured daily rows."""
    if not raw_data or len(raw_data) < 5:
        return []

    rows = []
    for i in range(5, len(raw_data)):
        row = raw_data[i]
        if not row or not row[0]:
            continue

        date_str = str(row[0]).strip()
        # Skip summary rows
        if date_str.upper() in ('GRAND TOTAL', 'PER MINGGU', 'TRACEHOLD', '') or date_str.startswith('M'):
            continue
        # Skip empty dates
        if not any(c.isdigit() for c in date_str):
            continue

        # GENEROS LP (Cols 0-12)
        lp_budget = _parse_rp(row[1] if len(row) > 1 else '')
        lp_lead = _parse_int(row[3] if len(row) > 3 else '')
        lp_closing = _parse_int(row[4] if len(row) > 4 else '')
        lp_botol = _parse_int(row[5] if len(row) > 5 else '')
        lp_closing_rate = _parse_rp(row[6] if len(row) > 6 else '')
        lp_omset = _parse_rp(row[11] if len(row) > 11 else '')
        lp_upsell = _parse_rp(row[12] if len(row) > 12 else '')

        # GENEROS CTWA (Cols 14-25)
        ctwa_budget = _parse_rp(row[14] if len(row) > 14 else '')
        ctwa_lead = _parse_int(row[16] if len(row) > 16 else '')
        ctwa_closing = _parse_int(row[17] if len(row) > 17 else '')
        ctwa_botol = _parse_int(row[18] if len(row) > 18 else '')
        ctwa_omset = _parse_rp(row[24] if len(row) > 24 else '')
        ctwa_upsell = _parse_rp(row[25] if len(row) > 25 else '')

        total_budget = lp_budget + ctwa_budget
        total_omset = lp_omset + ctwa_omset
        total_closing = lp_closing + ctwa_closing
        total_lead = lp_lead + ctwa_lead
        roas = round(total_omset / total_budget, 2) if total_budget > 0 else 0
        closing_rate = round((total_closing / total_lead * 100), 1) if total_lead > 0 else 0

        rows.append({
            'date': date_str,
            'lp': {
                'budget': lp_budget,
                'lead': lp_lead,
                'closing': lp_closing,
                'botol': lp_botol,
                'omset': lp_omset,
                'upsell': lp_upsell,
                'closing_rate': round((lp_closing / lp_lead * 100), 1) if lp_lead > 0 else 0,
                'roas': round(lp_omset / lp_budget, 2) if lp_budget > 0 else 0
            },
            'ctwa': {
                'budget': ctwa_budget,
                'lead': ctwa_lead,
                'closing': ctwa_closing,
                'botol': ctwa_botol,
                'omset': ctwa_omset,
                'upsell': ctwa_upsell,
                'closing_rate': round((ctwa_closing / ctwa_lead * 100), 1) if ctwa_lead > 0 else 0,
                'roas': round(ctwa_omset / ctwa_budget, 2) if ctwa_budget > 0 else 0
            },
            'total_budget': total_budget,
            'total_omset': total_omset,
            'total_closing': total_closing,
            'total_lead': total_lead,
            'roas': roas,
            'closing_rate': closing_rate
        })

    return rows


@app.route('/api/roas/data')
@admin_required
def api_roas_data():
    """Beta API — ambil data ROAS Landing Page Juli dari Google Sheet."""
    try:
        # Baca range open-ended: auto-detect bulan baru (Sep, Okt, dst)
        raw = _fetch_sheet_data('ADV Gilang!A274:M')
        if isinstance(raw, dict) and 'error' in raw:
            return jsonify({'status': 'error', 'error': raw['error']})

        # Process LP-only rows (skip headers, empty, summaries)
        rows = []
        for i, row in enumerate(raw):
            if i < 2:  # skip header rows (row 274=header, 275=empty)
                continue
            if not row or not row[0]:
                continue

            date_str = str(row[0]).strip()
            # Skip summary/empty
            if date_str.upper() in ('GRAND TOTAL', 'PER MINGGU', 'TRACEHOLD', '') or (len(date_str) > 1 and date_str[0] == 'M' and date_str[1:].lstrip()[0:1].isdigit()):
                continue
            if not any(c.isdigit() for c in date_str):
                continue

            budget = _parse_rp(row[1] if len(row) > 1 else '')
            lead = _parse_int(row[3] if len(row) > 3 else '')
            closing = _parse_int(row[4] if len(row) > 4 else '')
            botol = _parse_int(row[5] if len(row) > 5 else '')
            omset = _parse_rp(row[11] if len(row) > 11 else '')
            upsell = _parse_rp(row[12] if len(row) > 12 else '')

            roas = round(omset / budget, 2) if budget > 0 else 0
            closing_rate = round((closing / lead * 100), 1) if lead > 0 else 0

            rows.append({
                'date': date_str,
                'budget': budget,
                'lead': lead,
                'closing': closing,
                'botol': botol,
                'omset': omset,
                'upsell': upsell,
                'roas': roas,
                'closing_rate': closing_rate
            })

        total_budget = sum(r['budget'] for r in rows)
        total_omset = sum(r['omset'] for r in rows)
        total_closing = sum(r['closing'] for r in rows)
        total_lead = sum(r['lead'] for r in rows)

        return jsonify({
            'status': 'success',
            'total_days': len(rows),
            'summary': {
                'total_budget': total_budget,
                'total_omset': total_omset,
                'total_closing': total_closing,
                'total_lead': total_lead,
                'roas': round(total_omset / total_budget, 2) if total_budget > 0 else 0,
                'closing_rate': round((total_closing / total_lead * 100), 1) if total_lead > 0 else 0,
                'avg_budget_per_day': round(total_budget / len(rows)) if rows else 0,
                'avg_omset_per_day': round(total_omset / len(rows)) if rows else 0,
                'hari_profit': sum(1 for r in rows if r['roas'] >= 1),
                'hari_rugi': sum(1 for r in rows if 0 < r['roas'] < 1)
            },
            'daily': rows
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/roas')
@admin_required
def roas():
    """ROAS Dashboard with sidebar, header, filtering, and sync."""
    try:
        data_path = os.path.join(os.path.dirname(__file__), 'roas_live_data.json')
        if os.path.exists(data_path):
            with open(data_path, encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = []
    except:
        data = []
    return render_template('roas.html', data=data)


@app.route('/api/roas/sync')
@admin_required
def api_roas_sync():
    """Sync ROAS: baca data terbaru roas_live_data.json."""
    import subprocess, sys
    try:
        # Try to run sync_roas.py locally if available (will work on VM, may fail on Hostinger)
        script_path = os.path.join(os.path.dirname(__file__), 'sync_roas.py')
        if os.path.exists(script_path):
            r = subprocess.run(
                [sys.executable, script_path],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60
            )
            if r.returncode != 0:
                print(f"[ROAS] sync_roas.py exit {r.returncode}: {r.stderr[:200]}", flush=True)
    except Exception as e:
        print(f"[ROAS] sync_roas.py error (non-fatal): {e}", flush=True)

    # 2. Baca data terbaru dari local file (always works if file exists)
    data_path = os.path.join(os.path.dirname(__file__), 'roas_live_data.json')
    try:
        with open(data_path, encoding='utf-8') as f:
            daily = json.load(f)
    except:
        daily = []

    return jsonify({
        'status': 'success',
        'total_days': len(daily),
        'daily': daily,
        'message': r.stdout.strip() if r else ''
    })


# ===== CAMPAIGN BUILDER API =====
PIXEL_ID = '693288149418652'
PAGE_ID = '117958034744653'

ACCOUNT_CONFIG = {
    'G1': {'act': 'act_314725990877293', 'event': 'LEAD', 'pixel': '1058410491815964', 'page_id': '109906238862188', 'ig': '17841459432787055'},
    'G2': {'act': 'act_934880001670906', 'event': 'LEAD', 'pixel': '1191645919206946', 'page_id': '244588782080580', 'ig': '17841473667898390'},
    'G3': {'act': 'act_1488472672014493', 'event': 'PURCHASE', 'pixel': '695730420223615', 'page_id': '434998763031798', 'ig': '17841463389003508'},
    'G4': {'act': 'act_327433619915012', 'event': 'PURCHASE', 'pixel': '693288149418652', 'page_id': '117958034744653', 'ig': '17841462922856231'},
    'GM1': {'act': 'act_1346535746506682', 'event': 'LEAD', 'pixel': '1032295285845243', 'page_id': '1046676591867056', 'ig': '17841432094380116'},
    'GM3': {'act': 'act_1050879816724959', 'event': 'LEAD', 'pixel': '1574290620945008', 'page_id': '1046676591867056', 'ig': '17841432094380116'},
    'GM4': {'act': 'act_1014791740508771', 'event': 'LEAD', 'pixel': '1450768597077542', 'page_id': '1046676591867056', 'ig': '17841432094380116'},
}

# Nama display untuk notifikasi (berdasarkan isu, bukan kode akun)
ACCOUNT_DISPLAY = {
    'G1': 'G1', 'G2': 'G2', 'G3': 'G3', 'G4': 'G4',
    'GM1': 'BB Booster', 'GM3': 'TB', 'GM4': 'Pertumbuhan',
}


def _acc_display(acc_key):
    """Return nama display akun (isu) untuk notifikasi; fallback ke acc_key."""
    return ACCOUNT_DISPLAY.get(acc_key, acc_key)

OBJECTIVE_MAP = {'LEAD': 'OUTCOME_LEADS', 'ADD_TO_CART': 'OUTCOME_SALES', 'PURCHASE': 'OUTCOME_SALES'}


def _oss(acc, **fields):
    """Build object_story_spec dict dengan instagram_user_id per account (auto-attach IG)."""
    oss = {'page_id': acc['page_id']}
    if acc.get('ig'):
        oss['instagram_user_id'] = acc['ig']
    oss.update(fields)
    return json.dumps(oss)


def _get_meta_token():
    """Get Meta token from config chain."""
    token = META_TOKEN or ''
    if not token:
        try:
            cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
                token = cfg.get('meta_token', '')
        except:
            pass
    if not token:
        try:
            with open('/home/ubuntu/.hermes/.env', encoding='utf-8') as f:
                for l in f:
                    l = l.strip()
                    if 'META_TOKEN' in l and '=' in l and not l.startswith('#'):
                        token = l.split('=', 1)[1]
                        break
        except:
            pass
    return token


def _meta_api_post(url_path, data):
    """POST to Meta Graph API with token. Returns JSON body even on HTTP errors.
    Retry 3x dgn backoff untuk rate limit (code 4 transient) & koneksi error."""
    import urllib.request, urllib.parse, time
    token = _get_meta_token()
    data['access_token'] = token
    url = f'https://graph.facebook.com/v26.0{url_path}'
    body = urllib.parse.urlencode(data).encode()
    last = None
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=60)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read())
            except Exception:
                err_body = {'error': {'message': str(e)}}
            err = err_body.get('error', {})
            is_rate = (err.get('code') == 4) or (err.get('is_transient') and err.get('code') in (4, 613, 80000, 80004))
            if is_rate and attempt < 2:
                time.sleep(6 * (attempt + 1))
                last = err_body
                continue
            return err_body
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                last = {'error': {'message': str(e)}}
                continue
            return {'error': {'message': str(e)}}
    return last or {'error': {'message': 'unknown error'}}



def _find_video_file(konten_name):
    """Find local video file matching konten name."""
    konten_dir = '/home/ubuntu/konten'
    if not os.path.exists(konten_dir):
        konten_dir = '/home/u1734629/konten'
    if not os.path.exists(konten_dir):
        return None
    for root, dirs, files in os.walk(konten_dir):
        for f in files:
            if f.lower() == konten_name.lower():
                return os.path.join(root, f)
    return None


def _upload_video_meta(act_id, video_path):
    """Upload video to Meta Ad Account, return video_id."""
    import urllib.request
    token = _get_meta_token()
    url = f'https://graph.facebook.com/v26.0/{act_id}/advideos'
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    
    file_size = os.path.getsize(video_path)
    file_name = os.path.basename(video_path)
    
    # Build multipart body
    body = []
    body.append(f'--{boundary}')
    body.append('Content-Disposition: form-data; name="access_token"')
    body.append('')
    body.append(token)
    body.append(f'--{boundary}')
    body.append(f'Content-Disposition: form-data; name="file"; filename="{file_name}"')
    body.append('Content-Type: video/mp4')
    body.append('')
    
    with open(video_path, 'rb') as f:
        video_data = f.read()
    
    body_bin = ('\r\n'.join(body) + '\r\n').encode()
    body_bin += video_data
    body_bin += f'\r\n--{boundary}--\r\n'.encode()
    
    req = urllib.request.Request(url, data=body_bin)
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')

    # Retry 3x untuk rate limit (403 code 4 transient) & koneksi error
    last_err = None
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            result = json.loads(resp.read())
            return result.get('id')
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                err = {'error': {'message': str(e)}}
            err_obj = err.get('error', {})
            is_rate = (err_obj.get('code') == 4) or (err_obj.get('is_transient') and err_obj.get('code') in (4, 613, 80000, 80004))
            if is_rate and attempt < 2:
                time.sleep(8 * (attempt + 1))
                last_err = err
                continue
            msg = err_obj.get('message', str(err))
            title = err_obj.get('error_user_title', '')
            umsg = err_obj.get('error_user_msg', '')
            raise Exception(f"Video upload error: {msg} | title: {title} | user_msg: {umsg}")
        except (TimeoutError, ConnectionError, OSError, urllib.error.URLError) as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                last_err = {'error': {'message': str(e)}}
                continue
            raise Exception(f"Video upload error (network): {e}")
    raise Exception("Video upload error: " + str(last_err or 'unknown'))


def _gen_upload_thumb(act_id, video_path):
    """Generate thumbnail from video, upload to Meta, return image_hash."""
    import urllib.request, subprocess, shutil
    token = _get_meta_token()
    
    # Check if ffmpeg is available
    if not shutil.which('ffmpeg'):
        raise Exception('ffmpeg not available on this server')
    
    thumb_path = f'/tmp/thumb_{os.path.basename(video_path)}.jpg'
    subprocess.run(
        ['ffmpeg', '-y', '-i', video_path, '-ss', '00:00:01',
         '-vframes', '1', '-q:v', '2', thumb_path],
        capture_output=True, timeout=30
    )
    
    if not os.path.exists(thumb_path):
        raise Exception('Thumbnail generation failed')
    
    url = f'https://graph.facebook.com/v26.0/{act_id}/adimages'
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    
    body = []
    body.append(f'--{boundary}')
    body.append('Content-Disposition: form-data; name="access_token"')
    body.append('')
    body.append(token)
    body.append(f'--{boundary}')
    body.append('Content-Disposition: form-data; name="file"; filename="thumb.jpg"')
    body.append('Content-Type: image/jpeg')
    body.append('')
    
    with open(thumb_path, 'rb') as f:
        img_data = f.read()
    
    body_bin = ('\r\n'.join(body) + '\r\n').encode()
    body_bin += img_data
    body_bin += f'\r\n--{boundary}--\r\n'.encode()
    
    req = urllib.request.Request(url, data=body_bin)
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        os.remove(thumb_path)
        for val in result.get('images', {}).values():
            if val.get('hash'):
                return val['hash']
        raise Exception('No image hash returned')
    except urllib.error.HTTPError as e:
        err = json.loads(e.read())
        os.remove(thumb_path)
        raise Exception(f"Thumb upload error: {err.get('error',{}).get('message', str(err))}")
@app.route('/api/campaign-builder/create', methods=['POST'])
@admin_required
def api_campaign_builder_create():
    """Create campaign + adset + ad via Meta Ads API."""
    body = request.get_json(force=True) or {}
    acc_key = body.get('account', 'G1')
    name = body.get('name', '')
    is_cbo = body.get('is_cbo', True)
    daily_budget = str(body.get('daily_budget', 250000))
    age_min = body.get('age_min', 25)
    age_max = body.get('age_max', 50)
    gender = body.get('gender', 'F')
    adset_name = body.get('adset_name', '')
    interests = body.get('interests', [])
    primary_text = body.get('primary_text', '')
    headline = body.get('headline', '')
    cta = body.get('cta', 'Dapatkan Penawaran')
    landing_url = body.get('landing_url', 'https://generosindo.com')
    konten_selected = body.get('konten_selected', [])
    existing_post_ids = body.get('existing_post_ids', [])

    if acc_key not in ACCOUNT_CONFIG:
        return jsonify({'status': 'error', 'message': f'Akun {acc_key} tidak dikenal'})

    acc = ACCOUNT_CONFIG[acc_key]
    act_id = acc['act']
    event = acc['event']
    objective = OBJECTIVE_MAP.get(event, 'OUTCOME_SALES')

    gender_map = {'F': [2], 'M': [1], 'ALL': [1, 2]}
    genders = gender_map.get(gender, [2])

    excluded = {
        'regions': [{'key': '1668'}, {'key': '4140'}, {'key': '1683'}, {'key': '4138'}],
        'location_types': ['home', 'recent']
    }

    targeting = {
        'age_min': age_min,
        'age_max': age_max,
        'genders': genders,
        'geo_locations': {
            'countries': ['ID'],
            'location_types': ['home', 'recent']
        },
        'excluded_geo_locations': excluded,
        'publisher_platforms': ['facebook', 'instagram'],
        'facebook_positions': ['feed', 'story', 'marketplace'],
        'instagram_positions': ['stream', 'story', 'reels'],
        'device_platforms': ['mobile', 'desktop'],
        'targeting_automation': {'advantage_audience': 0},
    }

    if interests:
        targeting['flexible_spec'] = [{'interests': [{'id': i} for i in interests]}]

    steps = []

    try:
        # STEP 1: Create Campaign
        camp_data = {
            'name': name,
            'objective': objective,
            'status': 'PAUSED',
            'special_ad_categories': '[]',
            'buying_type': 'AUCTION',
            'bid_strategy': 'LOWEST_COST_WITHOUT_CAP',
        }
        if is_cbo:
            camp_data['daily_budget'] = daily_budget
        else:
            camp_data['is_adset_budget_sharing_enabled'] = True

        camp_result = _meta_api_post(f'/{act_id}/campaigns', camp_data)
        if 'error' in camp_result:
            raise Exception(f"Campaign error: {camp_result['error'].get('message', str(camp_result))}")
        camp_id = camp_result['id']
        steps.append({'step': 'campaign', 'id': camp_id, 'status': 'ok'})

        # STEP 2: Create Adset
        adset_name_clean = adset_name.strip() if adset_name else ''
        if not adset_name_clean:
            adset_name_clean = name[:40] + ' - Adset'
        adset_data = {
            'name': adset_name_clean,
            'campaign_id': camp_id,
            'status': 'PAUSED',
            'billing_event': 'IMPRESSIONS',
            'optimization_goal': 'OFFSITE_CONVERSIONS',
            'promoted_object': json.dumps({
                'pixel_id': acc.get('pixel', PIXEL_ID),
                'custom_event_type': event,
            }),
            'targeting': json.dumps(targeting),
        }
        if objective == 'OUTCOME_SALES':
            adset_data['conversion_strategy'] = 'ALL_CONVERSIONS'
        if not is_cbo:
            adset_data['daily_budget'] = daily_budget

        adset_result = _meta_api_post(f'/{act_id}/adsets', adset_data)
        if 'error' in adset_result:
            err = adset_result['error']
            detail = err.get('message', str(adset_result))
            subcode = err.get('error_subcode', '')
            user_msg = err.get('error_user_msg', '')
            user_title = err.get('error_user_title', '')
            raise Exception(f"Adset error: {detail} | subcode: {subcode} | user_title: {user_title} | user_msg: {user_msg}")
        adset_id = adset_result['id']
        steps.append({'step': 'adset', 'id': adset_id, 'status': 'ok'})

        # STEP 3-4: Create Creative + Ad per konten
        cta_map = {
            'Ambil Promo': 'GET_OFFER_VIEW',
            'Dapatkan Penawaran': 'GET_OFFER_VIEW',
            'Pesan Sekarang': 'ORDER_NOW',
            'Beli Sekarang': 'SHOP_NOW',
            'Daftar': 'SIGN_UP',
            'Pelajari Lebih Lanjut': 'LEARN_MORE',
        }
        cta_type = cta_map.get(cta, 'GET_OFFER_VIEW')

        ad_ids = []

        if konten_selected and len(konten_selected) > 0:
            # Loop per konten — upload video, create creative + ad
            for idx, konten in enumerate(konten_selected):
                konten_name = konten.get('name', '')
                konten_id = konten.get('id', '')
                ad_name = konten.get('ad_name', konten_name)[:50]
                
                # Find local video file
                video_path = _find_video_file(konten_name)
                
                # If not found locally, try download from Google Drive
                if not video_path or not os.path.exists(video_path):
                    if konten_id:
                        try:
                            import requests as _req
                            import re as _re
                            dl_path = f'/tmp/cb_{konten_id}.mp4'
                            # Try gdown first (handles Drive virus scan page)
                            try:
                                import gdown as _gd
                                _gd.download(id=konten_id, output=dl_path, quiet=True)
                            except:
                                pass
                            if not os.path.exists(dl_path) or os.path.getsize(dl_path) <= 1024:
                                # Fallback: session-based download with confirm handling
                                _dl_sess = _req.Session()
                                _dl_url1 = f'https://drive.google.com/uc?export=download&id={konten_id}'
                                _dl_resp1 = _dl_sess.get(_dl_url1, timeout=60)
                                if 'confirm=' in _dl_resp1.text:
                                    _dl_c = _re.search(r'confirm=([0-9A-Za-z]+)', _dl_resp1.text)
                                    if _dl_c:
                                        _dl_url2 = f'https://drive.google.com/uc?export=download&confirm={_dl_c.group(1)}&id={konten_id}'
                                        _dl_resp2 = _dl_sess.get(_dl_url2, timeout=300, stream=True)
                                        if _dl_resp2.status_code == 200:
                                            with open(dl_path, 'wb') as _f:
                                                for _chunk in _dl_resp2.iter_content(chunk_size=8192):
                                                    _f.write(_chunk)
                                elif _dl_resp1.status_code == 200:
                                    with open(dl_path, 'wb') as _f:
                                        for _chunk in _dl_resp1.iter_content(chunk_size=8192):
                                            _f.write(_chunk)
                            if os.path.exists(dl_path) and os.path.getsize(dl_path) > 1024:
                                video_path = dl_path
                                print(f"[CB] Downloaded {konten_name} from Drive ({os.path.getsize(dl_path)} bytes)", flush=True)
                        except Exception as _dl_e:
                            print(f"[CB] Drive download failed for {konten_name}: {_dl_e}", flush=True)
                
                if video_path and os.path.exists(video_path):
                    # Upload video to Meta directly (no self-HTTP-call)
                    try:
                        video_id = _upload_video_meta(act_id, video_path)
                        print(f"[CB] Video uploaded OK: {video_id} for {konten_name}", flush=True)
                        try:
                            image_hash = _gen_upload_thumb(act_id, video_path)
                        except Exception as _th_e:
                            print(f"[CB] Thumbnail skip for {konten_name}: {_th_e}", flush=True)
                            image_hash = None
                            image_url = None
                            # Try: download public image & upload, or use image_url
                            try:
                                import requests as _pl_req, urllib.request as _pl_ur
                                # Try to upload a placeholder from a reliable CDN
                                _pl_url = 'https://report.anaksehatgeneros.com/static/thumbnail_generos.jpg'
                                _pl_img = _pl_req.get(_pl_url, timeout=10).content
                                if len(_pl_img) > 100:
                                    _pl_boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
                                    _pl_body_bin = (
                                        f'--{_pl_boundary}\r\nContent-Disposition: form-data; name="access_token"\r\n\r\n{_get_meta_token()}\r\n'
                                        f'--{_pl_boundary}\r\nContent-Disposition: form-data; name="file"; filename="thumb.jpg"\r\n'
                                        f'Content-Type: image/jpeg\r\n\r\n'.encode() + _pl_img +
                                        f'\r\n--{_pl_boundary}--\r\n'.encode()
                                    )
                                    _pl_req2 = _pl_ur.Request(f'https://graph.facebook.com/v26.0/{act_id}/adimages', data=_pl_body_bin)
                                    _pl_req2.add_header('Content-Type', f'multipart/form-data; boundary={_pl_boundary}')
                                    _pl_resp = _pl_ur.urlopen(_pl_req2, timeout=30)
                                    _pl_result = json.loads(_pl_resp.read())
                                    for _pl_val in _pl_result.get('images', {}).values():
                                        if _pl_val.get('hash'):
                                            image_hash = _pl_val['hash']
                                            print(f"[CB] Placeholder thumb uploaded: {image_hash}", flush=True)
                                            break
                            except Exception as _pl_e:
                                print(f"[CB] Placeholder thumb failed, will use image_url: {_pl_e}", flush=True)
                    except Exception as e:
                        print(f"[CB] Video upload failed for {konten_name}: {e}", flush=True)
                        # Fallback: create link ad
                        creative_data = {
                            'name': f"{name[:30]} - {konten_name[:20]}",
                            'object_story_spec': _oss(acc, link_data={
                                'link': landing_url,
                                'message': primary_text,
                                'name': headline,
                                'call_to_action': {'type': cta_type},
                            }),
                        }
                        cr_result = _meta_api_post(f'/{act_id}/adcreatives', creative_data)
                        if 'error' in cr_result:
                            raise Exception(f"Creative error (fallback) for {konten_name}: {cr_result['error'].get('message', str(cr_result))}")
                        cr_id = cr_result['id']
                        steps.append({'step': f'creative_{idx}', 'id': cr_id, 'status': 'ok_fallback', 'konten': konten_name})
                        ad_data = {
                            'name': f"{ad_name}",
                            'adset_id': adset_id,
                            'creative': json.dumps({'creative_id': cr_id}),
                            'status': 'PAUSED',
                        }
                        ad_result = _meta_api_post(f'/{act_id}/ads', ad_data)
                        if 'error' in ad_result:
                            raise Exception(f"Ad error (fallback) for {konten_name}: {ad_result['error'].get('message', str(ad_result))}")
                        ad_ids.append(ad_result['id'])
                        steps.append({'step': f'ad_{idx}', 'id': ad_result['id'], 'status': 'ok_fallback', 'konten': konten_name})
                        continue
                    
                    # Create creative with video_data
                    _video_data = {
                        'video_id': video_id,
                        'title': headline,
                        'message': primary_text,
                        'call_to_action': {
                            'type': cta_type,
                            'value': {'link': landing_url}
                        },
                    }
                    if image_hash:
                        _video_data['image_hash'] = image_hash
                    else:
                        _video_data['image_url'] = 'https://report.anaksehatgeneros.com/static/thumbnail_generos.jpg'
                    creative_data = {
                        'name': f"{name[:30]} - {konten_name[:20]}",
                        'object_story_spec': _oss(acc, video_data=_video_data),
                    }
                    
                    cr_result = _meta_api_post(f'/{act_id}/adcreatives', creative_data)
                    if 'error' in cr_result:
                        raise Exception(f"Creative error for {konten_name}: {cr_result['error'].get('message', str(cr_result))}")
                    cr_id = cr_result['id']
                    print(f"[CB] Video creative OK: {cr_id} for {konten_name}", flush=True)
                    steps.append({'step': f'creative_{idx}', 'id': cr_id, 'status': 'ok', 'konten': konten_name})
                    
                    # Create ad
                    ad_data = {
                        'name': f"{ad_name}",
                        'adset_id': adset_id,
                        'creative': json.dumps({'creative_id': cr_id}),
                        'status': 'PAUSED',
                    }
                    ad_result = _meta_api_post(f'/{act_id}/ads', ad_data)
                    if 'error' in ad_result:
                        raise Exception(f"Ad error for {konten_name}: {ad_result['error'].get('message', str(ad_result))}")
                    ad_ids.append(ad_result['id'])
                    print(f"[CB] Video ad OK: {ad_result['id']} for {konten_name}", flush=True)
                    steps.append({'step': f'ad_{idx}', 'id': ad_result['id'], 'status': 'ok', 'konten': konten_name})
                else:
                    print(f"[CB] Video file not found, fallback to link ad for {konten_name}", flush=True)
                    # Fallback: create link ad without video
                    creative_data = {
                        'name': f"{name[:30]} - {konten_name[:20]}",
                        'object_story_spec': _oss(acc, link_data={
                            'link': landing_url,
                            'message': primary_text,
                            'name': headline,
                            'call_to_action': {'type': cta_type},
                        }),
                    }
                    cr_result = _meta_api_post(f'/{act_id}/adcreatives', creative_data)
                    if 'error' in cr_result:
                        raise Exception(f"Creative error (fallback) for {konten_name}: {cr_result['error'].get('message', str(cr_result))}")
                    cr_id = cr_result['id']
                    steps.append({'step': f'creative_{idx}', 'id': cr_id, 'status': 'ok_fallback', 'konten': konten_name})
                    
                    ad_data = {
                        'name': f"{ad_name}",
                        'adset_id': adset_id,
                        'creative': json.dumps({'creative_id': cr_id}),
                        'status': 'PAUSED',
                    }
                    ad_result = _meta_api_post(f'/{act_id}/ads', ad_data)
                    if 'error' in ad_result:
                        raise Exception(f"Ad error (fallback) for {konten_name}: {ad_result['error'].get('message', str(ad_result))}")
                    ad_ids.append(ad_result['id'])
                    steps.append({'step': f'ad_{idx}', 'id': ad_result['id'], 'status': 'ok_fallback', 'konten': konten_name})

        # Handle Existing Posts
        ep_count = 0
        if existing_post_ids and len(existing_post_ids) > 0:
            for ep_idx, ep_id in enumerate(existing_post_ids):
                ep_id = ep_id.strip()
                if not ep_id:
                    continue
                try:
                    creative_data = {
                        'name': f"{name[:35]} - EP {ep_idx+1}",
                        # ⚠️ Normalize: kalau ep_id udah full (pageid_postid) pakai langsung,
                        # kalau cuma angka, prefix page akun. Jangan double-prefix.
                        'object_story_id': ep_id if '_' in ep_id else acc['page_id'] + '_' + ep_id,
                    }
                    cr_result = _meta_api_post(f'/{act_id}/adcreatives', creative_data)
                    if 'error' in cr_result:
                        print(f"[CB] EP creative error for {ep_id}: {cr_result['error'].get('message', str(cr_result))}", flush=True)
                        steps.append({'step': f'ep_creative_{ep_idx}', 'status': 'error', 'message': cr_result['error'].get('message', '')})
                        continue
                    cr_id = cr_result['id']
                    steps.append({'step': f'ep_creative_{ep_idx}', 'id': cr_id, 'status': 'ok'})

                    ad_data = {
                        'name': f"{name[:45]} - EP {ep_idx+1}",
                        'adset_id': adset_id,
                        'creative': json.dumps({'creative_id': cr_id}),
                        'status': 'PAUSED',
                    }
                    ad_result = _meta_api_post(f'/{act_id}/ads', ad_data)
                    if 'error' in ad_result:
                        print(f"[CB] EP ad error for {ep_id}: {ad_result['error'].get('message', str(ad_result))}", flush=True)
                        steps.append({'step': f'ep_ad_{ep_idx}', 'status': 'error', 'message': ad_result['error'].get('message', '')})
                        continue
                    ad_ids.append(ad_result['id'])
                    ep_count += 1
                    print(f"[CB] EP ad OK: {ad_result['id']} for post {ep_id}", flush=True)
                    steps.append({'step': f'ep_ad_{ep_idx}', 'id': ad_result['id'], 'status': 'ok'})
                except Exception as ep_e:
                    print(f"[CB] EP error for {ep_id}: {ep_e}", flush=True)
                    steps.append({'step': f'ep_{ep_idx}', 'status': 'error', 'message': str(ep_e)})

        if not konten_selected and not existing_post_ids:
            # No konten & no existing post — create 1 link ad
            creative_data = {
                'name': name[:40] + ' - Creative',
                'object_story_spec': _oss(acc, link_data={
                    'link': landing_url,
                    'message': primary_text,
                    'name': headline,
                    'call_to_action': {'type': cta_type},
                }),
            }
            cr_result = _meta_api_post(f'/{act_id}/adcreatives', creative_data)
            if 'error' in cr_result:
                raise Exception(f"Creative error: {cr_result['error'].get('message', str(cr_result))}")
            cr_id = cr_result['id']
            steps.append({'step': 'creative', 'id': cr_id, 'status': 'ok'})
            
            ad_data = {
                'name': name[:50],
                'adset_id': adset_id,
                'creative': json.dumps({'creative_id': cr_id}),
                'status': 'PAUSED',
            }
            ad_result = _meta_api_post(f'/{act_id}/ads', ad_data)
            if 'error' in ad_result:
                raise Exception(f"Ad error: {ad_result['error'].get('message', str(ad_result))}")
            ad_ids.append(ad_result['id'])
            steps.append({'step': 'ad', 'id': ad_result['id'], 'status': 'ok'})

        # Send Telegram notification
        try:
            _tg_token = ''
            try:
                _cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
                with open(_cfg_path, encoding='utf-8') as _f:
                    _cfg = json.load(_f)
                    _tg_token = _cfg.get('telegram_bot_token', '')
            except:
                pass
            if not _tg_token:
                try:
                    from config import TELEGRAM_BOT_TOKEN as _tgt
                    _tg_token = _tgt
                except:
                    pass
            if _tg_token:
                _gender_label = 'Pria' if gender == 'M' else 'Wanita' if gender == 'F' else 'Semua'
                _notif_text = (
                    f"🚀 Campaign Baru Dibuat\n"
                    f"{'━'*25}\n"
                    f"📛 {name}\n"
                    f"👤 {_acc_display(acc_key)}\n"
                    f"💰 Rp{int(daily_budget):,}/hari ({'CBO' if is_cbo else 'ABO'})\n"
                    f"🎯 {_gender_label}, {age_min}-{age_max} th\n"
                    f"📦 {len(ad_ids)} iklan (PAUSED)\n"
                    f"{'━'*25}"
                )
                import urllib.parse as _up
                _api_url = f"https://api.telegram.org/bot{_tg_token}/sendMessage"
                _tg_payload = _up.urlencode({
                    'chat_id': '-1003990111670',
                    'message_thread_id': '1958',
                    'text': _notif_text,
                    'parse_mode': 'HTML',
                })
                import subprocess as _sub2
                _sub2.run(['curl', '-s', '-X', 'POST', '-d', _tg_payload,
                          '--connect-timeout', '5', '--max-time', '10', _api_url],
                         capture_output=True, timeout=15)
        except:
            pass

        return jsonify({
            'status': 'success',
            'message': f'Campaign {name} berhasil dibuat (PAUSED) - {len(ad_ids)} iklan',
            'campaign_id': camp_id,
            'adset_id': adset_id,
            'ad_ids': ad_ids,
            'ep_count': ep_count,
            'steps': steps,
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e),
            'steps': steps,
        })


# ===== END CAMPAIGN BUILDER API =====


# ===== CUSTOM AUDIENCE API =====
def _meta_api_get(url_path):
    """GET Meta Graph API dengan token. Return dict JSON (termasuk error body)."""
    import urllib.request, urllib.parse, urllib.error
    token = _get_meta_token()
    sep = '&' if '?' in url_path else '?'
    url = f'https://graph.facebook.com/v26.0{url_path}{sep}access_token={urllib.parse.quote(token)}'
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url), timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _ca_account_id(acc_key):
    """Validasi acc_key G1-G4, return act id. None kalau invalid."""
    cfg = ACCOUNT_CONFIG.get(acc_key)
    return cfg['act'] if cfg else None


def _ca_acc_short(act_id):
    for sn, cfg in ACCOUNT_CONFIG.items():
        if cfg['act'] == act_id:
            return sn
    return None


@app.route('/api/custom-audiences/meta')
@admin_required
def api_custom_audiences_meta():
    """List semua custom audience dari Meta Ads API untuk satu akun."""
    acc = request.args.get('acc', 'G1')
    act = _ca_account_id(acc)
    if not act:
        return jsonify({'status': 'error', 'message': f'Akun {acc} tidak dikenal'})
    data = _meta_api_get(f'/{act}/customaudiences?fields=id,name,subtype,operation_status,delivery_status,retention_days&limit=500')
    if 'error' in data:
        return jsonify({'status': 'error', 'message': data.get('error', {}).get('message', 'Meta API error'), 'meta': data})
    out = []
    for a in data.get('data', []):
        subtype = a.get('subtype', 'CUSTOM')
        op = a.get('operation_status') or {}
        dv = a.get('delivery_status') or {}
        op_desc = ''
        if isinstance(op, dict):
            op_desc = (op.get('description') or '')[:80]
        elif isinstance(op, str):
            op_desc = op
        dv_code = dv.get('code') if isinstance(dv, dict) else None
        dv_desc = (dv.get('description') or '')[:80] if isinstance(dv, dict) else ''
        # Siap = delivery_status code 200 (audience bisa dipakai di iklan).
        # operation_status cuma info operasi async (prefill dll) — bukan indikator siap.
        if dv_code == 200:
            st = 'Normal'
        else:
            st = op_desc or dv_desc or 'Processing'
        out.append({
            'meta_id': a.get('id'),
            'name': a.get('name', ''),
            'type': 'lookalike' if subtype == 'LOOKALIKE' else 'custom',
            'meta_subtype': subtype,
            'size': 0,  # approximate_count tidak ke-expose oleh token ini — UI tampilkan '—'
            'status': st,
            'retention_days': a.get('retention_days'),
        })
    return jsonify({'status': 'ok', 'audiences': out})


@app.route('/api/custom-audiences/pixels')
@admin_required
def api_custom_audiences_pixels():
    """List pixel website untuk satu akun (buat dropdown sumber data)."""
    acc = request.args.get('acc', 'G1')
    act = _ca_account_id(acc)
    if not act:
        return jsonify({'status': 'error', 'message': f'Akun {acc} tidak dikenal'})
    data = _meta_api_get(f'/{act}/adspixels?fields=id,name&limit=100')
    if 'error' in data:
        return jsonify({'status': 'error', 'message': data.get('error', {}).get('message', 'Meta API error')})
    pixels = [{'id': p.get('id'), 'name': p.get('name', '')} for p in data.get('data', [])]
    return jsonify({'status': 'ok', 'pixels': pixels})


# Cache video views per akun (biar nggak spam Meta API tiap modal dibuka)
_video_views_cache = {}  # {acc: {'ts': float, 'views': {video_id: {'views': int, 'spend': float}}}}


def _get_video_views(act, acc, days=90):
    """Ambil video views + spend per video dari insights (breakdown video_asset).
    Cache 10 menit per akun. Return dict video_id -> {views, spend}."""
    import time as _time
    now = _time.time()
    cached = _video_views_cache.get(acc)
    if cached and (now - cached['ts']) < 600:
        return cached['views']

    until = date.today()
    since = until - timedelta(days=days)
    data = _meta_api_get(
        f'/{act}/insights?fields=actions,spend&breakdowns=video_asset&action_breakdowns=action_type&level=ad'
        f'&since={since.isoformat()}&until={until.isoformat()}&limit=500'
    )
    views = {}
    if 'error' not in data:
        for row in data.get('data', []):
            va = row.get('video_asset') or {}
            vid = va.get('video_id')
            if not vid:
                continue
            vv = 0
            for a in (row.get('actions') or []):
                if a.get('action_type') == 'video_view':
                    try:
                        vv = int(float(a.get('value') or 0))
                    except (TypeError, ValueError):
                        vv = 0
            try:
                spend = float(row.get('spend') or 0)
            except (TypeError, ValueError):
                spend = 0.0
            if vid not in views:
                views[vid] = {'views': 0, 'spend': 0.0}
            views[vid]['views'] += vv
            views[vid]['spend'] += spend
    _video_views_cache[acc] = {'ts': now, 'views': views}
    return views


@app.route('/api/custom-audiences/videos')
@admin_required
def api_custom_audiences_videos():
    """List video iklan dari Meta untuk satu akun (buat dropdown pilih video Video Views).
    Sertakan views + tanggal terbit biar gampang pilih video."""
    acc = request.args.get('acc', 'G1')
    act = _ca_account_id(acc)
    if not act:
        return jsonify({'status': 'error', 'message': f'Akun {acc} tidak dikenal'})
    data = _meta_api_get(f'/{act}/advideos?fields=id,title,thumbnail_url,picture,created_time&limit=100')
    if 'error' in data:
        return jsonify({'status': 'error', 'message': data.get('error', {}).get('message', 'Meta API error')})
    views_map = _get_video_views(act, acc)
    videos = []
    for v in data.get('data', []):
        title = (v.get('title') or '').strip()
        if not title:
            title = f'Video {v.get("id")}'
        vid = str(v.get('id'))
        meta = views_map.get(vid, {})
        created = v.get('created_time', '')
        if created:
            try:
                created = created[:10]  # YYYY-MM-DD
            except Exception:
                pass
        videos.append({
            'id': vid,
            'title': title,
            'thumbnail': v.get('picture') or v.get('thumbnail_url') or '',
            'views': meta.get('views', 0),
            'spend': meta.get('spend', 0.0),
            'created': created,
        })
    return jsonify({'status': 'ok', 'videos': videos})


@app.route('/api/custom-audiences')
@admin_required
def api_custom_audiences_list():
    """List custom audience dari DB lokal (filter per akun)."""
    acc = request.args.get('acc', '')
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    if acc:
        cur.execute("SELECT * FROM custom_audiences WHERE account_key = %s ORDER BY id DESC", (acc,))
    else:
        cur.execute("SELECT * FROM custom_audiences ORDER BY id DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({'status': 'ok', 'audiences': rows})


@app.route('/api/custom-audiences', methods=['POST'])
@admin_required
def api_custom_audiences_create():
    """Buat custom audience — simpan lokal + create di Meta Ads API."""
    body = request.get_json(force=True) or {}
    acc = body.get('account_key', 'G1')
    name = (body.get('name') or '').strip()
    ca_type = body.get('type', 'custom')
    source = body.get('source', 'pixel')
    pixel_id = body.get('pixel_id', '')
    lookback = int(body.get('lookback_days', 90) or 90)
    video_views = 1 if body.get('video_views') else 0
    video_ids = body.get('video_ids') or []
    if isinstance(video_ids, str):
        video_ids = [x.strip() for x in video_ids.split(',') if x.strip()]
    video_ids = [str(x) for x in video_ids if str(x).strip()]
    exclude_leads_atc = 1 if body.get('exclude_leads_atc', True) else 0
    ratio = int(body.get('lookalike_ratio', 5) or 5)
    country = body.get('lookalike_country', 'ID') or 'ID'
    seed_meta_id = body.get('seed_meta_id', '')  # buat lookalike

    act = _ca_account_id(acc)
    if not act:
        return jsonify({'status': 'error', 'message': f'Akun {acc} tidak dikenal'})
    if not name:
        return jsonify({'status': 'error', 'message': 'Nama audience wajib diisi'})
    if ca_type not in ('custom', 'lookalike'):
        return jsonify({'status': 'error', 'message': 'Tipe audience tidak valid'})
    if ca_type == 'custom' and source != 'csv' and not pixel_id:
        return jsonify({'status': 'error', 'message': 'Pixel wajib dipilih (kecuali sumber CSV)'})

    meta_id = None
    meta_status = ''

    if ca_type == 'custom':
        payload = {'name': name}
        if source == 'csv':
            # CSV: butuh upload file — lewat endpoint terpisah /api/custom-audiences/csv
            return jsonify({'status': 'error', 'message': 'Sumber CSV pakai endpoint upload /api/custom-audiences/csv'})
        # Pixel-based (PageView / VideoView) — format rule v3.0+ (inclusions)
        if video_views and video_ids:
            # Video spesifik terpilih — filter video_event + video_ids
            filters = [{
                'field': 'video_event',
                'operator': '=',
                'value': {'event': 'VideoViewed', 'video_ids': video_ids}
            }]
            if exclude_leads_atc:
                filters.append({'field': 'event', 'operator': 'neq', 'value': 'Lead'})
                filters.append({'field': 'event', 'operator': 'neq', 'value': 'AddToCart'})
        elif video_views:
            # Semua video views dari pixel
            filters = [{'field': 'event', 'operator': 'eq', 'value': 'VideoView'}]
        else:
            filters = [{'field': 'event', 'operator': 'eq', 'value': 'PageView'}]
        payload['rule'] = json.dumps({
            'inclusions': {
                'operator': 'or',
                'rules': [
                    {
                        'event_sources': [{'id': pixel_id, 'type': 'pixel'}],
                        'retention_seconds': lookback * 86400,
                        'filter': {
                            'operator': 'and',
                            'filters': filters
                        }
                    }
                ]
            }
        })
        resp = _meta_api_post(f'/{act}/customaudiences', payload)
    else:
        # Lookalike — origin_audience_id TOP-LEVEL + lookalike_spec {type, country, ratio}
        if not seed_meta_id:
            return jsonify({'status': 'error', 'message': 'Lookalike butuh seed custom audience (pilih audience asal)'})
        payload = {
            'name': name,
            'subtype': 'LOOKALIKE',
            'origin_audience_id': seed_meta_id,
            'lookalike_spec': json.dumps({
                'type': 'similarity',
                'country': country,
                'ratio': round(ratio / 100.0, 3),
            }),
        }
        resp = _meta_api_post(f'/{act}/customaudiences', payload)

    if resp.get('id'):
        meta_id = resp['id']
        meta_status = 'CREATED'
    elif 'error' in resp:
        return jsonify({'status': 'error', 'message': resp['error'].get('message', 'Meta API error'), 'meta': resp})

    # Simpan ke DB lokal
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO custom_audiences
            (account_key, name, type, source, pixel_id, lookback_days, video_views,
             exclude_leads_atc, lookalike_ratio, lookalike_country, meta_audience_id, meta_status, size)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
    """, (acc, name, ca_type, source, pixel_id or None, lookback, video_views,
          exclude_leads_atc, ratio, country, meta_id, meta_status))
    new_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'status': 'ok', 'id': new_id, 'meta_audience_id': meta_id})


@app.route('/api/custom-audiences/csv', methods=['POST'])
@admin_required
def api_custom_audiences_csv():
    """Upload CSV (multipart) → create Custom Audience di Meta (customer_file_source)."""
    import requests as _req
    acc = request.form.get('account_key', 'G1')
    name = (request.form.get('name') or '').strip()
    lookback = int(request.form.get('lookback_days', 90) or 90)
    exclude_leads_atc = 1 if request.form.get('exclude_leads_atc', '1') == '1' else 0
    f = request.files.get('file')
    if not name:
        return jsonify({'status': 'error', 'message': 'Nama audience wajib diisi'})
    if not f or not f.filename:
        return jsonify({'status': 'error', 'message': 'File CSV wajib diupload'})
    act = _ca_account_id(acc)
    if not act:
        return jsonify({'status': 'error', 'message': f'Akun {acc} tidak dikenal'})
    csv_bytes = f.read()
    if len(csv_bytes) > 5 * 1024 * 1024:
        return jsonify({'status': 'error', 'message': 'File CSV maksimal 5MB'})

    token = _get_meta_token()
    try:
        url = f'https://graph.facebook.com/v26.0/{act}/customaudiences'
        r = _req.post(url, params={'access_token': token},
                      data={'name': name, 'subtype': 'CUSTOM', 'retention_days': lookback,
                            'customer_file_source': {'schema': ['EMAIL']}},
                      files={'customer_file_source': (f.filename, csv_bytes, 'text/csv')},
                      timeout=60)
        resp = r.json()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

    if resp.get('id'):
        meta_id = resp['id']
        meta_status = 'CREATED'
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO custom_audiences
                (account_key, name, type, source, lookback_days, exclude_leads_atc,
                 meta_audience_id, meta_status, size)
            VALUES (%s,%s,'custom','csv',%s,%s,%s,%s,0)
        """, (acc, name, lookback, exclude_leads_atc, meta_id, meta_status))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'status': 'ok', 'id': new_id, 'meta_audience_id': meta_id})
    return jsonify({'status': 'error', 'message': resp.get('error', {}).get('message', 'Meta API error'), 'meta': resp})


@app.route('/api/custom-audiences/<int:aud_id>', methods=['PUT'])
@admin_required
def api_custom_audiences_update(aud_id):
    """Update custom audience (nama + preferensi lokal). Kalau ada meta_audience_id, rename di Meta."""
    body = request.get_json(force=True) or {}
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM custom_audiences WHERE id = %s", (aud_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({'status': 'error', 'message': 'Audience tidak ditemukan'})

    name = (body.get('name') or row['name']).strip()
    exclude_leads_atc = 1 if body.get('exclude_leads_atc', row['exclude_leads_atc']) else 0
    cur.execute("UPDATE custom_audiences SET name=%s, exclude_leads_atc=%s WHERE id=%s",
                (name, exclude_leads_atc, aud_id))
    conn.commit()
    cur.close(); conn.close()

    if row.get('meta_audience_id'):
        resp = _meta_api_post(f"/{row['meta_audience_id']}", {'name': name})
        if 'error' in resp:
            return jsonify({'status': 'error', 'message': 'Lokal terupdate, tapi rename di Meta gagal: ' + resp['error'].get('message', '')})
    return jsonify({'status': 'ok'})


@app.route('/api/custom-audiences/<int:aud_id>', methods=['DELETE'])
@admin_required
def api_custom_audiences_delete(aud_id):
    """Hapus custom audience — lokal + Meta (kalau ada meta_audience_id)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM custom_audiences WHERE id = %s", (aud_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({'status': 'error', 'message': 'Audience tidak ditemukan'})
    cur.execute("DELETE FROM custom_audiences WHERE id = %s", (aud_id,))
    conn.commit()
    cur.close(); conn.close()

    if row.get('meta_audience_id'):
        # DELETE beneran di Meta
        import urllib.request, urllib.parse
        token = _get_meta_token()
        url = f"https://graph.facebook.com/v26.0/{row['meta_audience_id']}?access_token={urllib.parse.quote(token)}"
        try:
            req = urllib.request.Request(url, method='DELETE')
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            if 'error' in resp:
                return jsonify({'status': 'error', 'message': 'Lokal terhapus, tapi hapus di Meta gagal: ' + resp['error'].get('message', '')})
        except Exception as e:
            return jsonify({'status': 'error', 'message': 'Lokal terhapus, tapi hapus di Meta gagal: ' + str(e)})
    return jsonify({'status': 'ok'})


@app.route('/api/custom-audiences/<int:aud_id>/sync', methods=['POST'])
@admin_required
def api_custom_audiences_sync(aud_id):
    """Refresh ukuran & status dari Meta (approximate_count)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM custom_audiences WHERE id = %s", (aud_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({'status': 'error', 'message': 'Audience tidak ditemukan'})
    if not row.get('meta_audience_id'):
        cur.close(); conn.close()
        return jsonify({'status': 'error', 'message': 'Audience belum punya meta_audience_id (belum dibuat di Meta)'})

    data = _meta_api_get(f"/{row['meta_audience_id']}?fields=approximate_count,operation_status,name")
    if 'error' in data:
        cur.close(); conn.close()
        return jsonify({'status': 'error', 'message': data.get('error', {}).get('message', 'Meta API error')})

    size = data.get('approximate_count', 0)
    status = data.get('operation_status', '')
    cur.execute("UPDATE custom_audiences SET size=%s, meta_status=%s WHERE id=%s", (size, status, aud_id))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'status': 'ok', 'size': size, 'meta_status': status})


@app.route('/api/custom-audiences/delete-meta', methods=['POST'])
@admin_required
def api_custom_audiences_delete_meta():
    """Hapus audience by meta_id — Meta DELETE + hapus baris lokal yang nyambung."""
    body = request.get_json(force=True) or {}
    meta_id = str(body.get('meta_id', '') or '').strip()
    if not meta_id:
        return jsonify({'status': 'error', 'message': 'meta_id wajib diisi'})
    # Hapus lokal dulu (kalau ada)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM custom_audiences WHERE meta_audience_id = %s", (meta_id,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Gagal hapus lokal: ' + str(e)})
    # Hapus di Meta
    import urllib.request, urllib.parse
    token = _get_meta_token()
    url = f"https://graph.facebook.com/v26.0/{meta_id}?access_token={urllib.parse.quote(token)}"
    try:
        req = urllib.request.Request(url, method='DELETE')
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if 'error' in resp:
            return jsonify({'status': 'error', 'message': 'Terhapus lokal, tapi gagal hapus di Meta: ' + resp['error'].get('message', '')})
    except Exception as e:
        return jsonify({'status': 'error', 'message': 'Terhapus lokal, tapi gagal hapus di Meta: ' + str(e)})
    return jsonify({'status': 'ok'})


@app.route('/api/custom-audiences/rename', methods=['POST'])
@admin_required
def api_custom_audiences_rename():
    """Rename audience by meta_id — Meta + lokal."""
    body = request.get_json(force=True) or {}
    meta_id = str(body.get('meta_id', '') or '').strip()
    name = (body.get('name') or '').strip()
    if not meta_id or not name:
        return jsonify({'status': 'error', 'message': 'meta_id dan nama wajib diisi'})
    resp = _meta_api_post(f"/{meta_id}", {'name': name})
    if 'error' in resp:
        return jsonify({'status': 'error', 'message': resp['error'].get('message', 'Meta API error')})
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE custom_audiences SET name=%s WHERE meta_audience_id=%s", (name, meta_id))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return jsonify({'status': 'ok'})


# ===== END CUSTOM AUDIENCE API =====


@app.route('/api/campaign-studio/create', methods=['POST'])
@admin_required
def api_campaign_studio_create():
    """Create campaign + multiple adsets + ads via Meta Ads API (Campaign Studio)."""
    body = request.get_json(force=True) or {}
    acc_key = body.get('account', 'G1')
    name = body.get('name', '')
    is_cbo = body.get('is_cbo', True)
    testing_mode = body.get('testing_mode', False)
    daily_budget = str(body.get('daily_budget', 250000))
    adsets = body.get('adsets', [])
    primary_text = body.get('primary_text', '')
    headline = body.get('headline', '')
    cta = body.get('cta', 'Dapatkan Penawaran')
    landing_url = body.get('landing_url', 'https://generosindo.com')
    konten_selected = body.get('konten_selected', [])
    existing_post_ids = body.get('existing_post_ids', [])
    existing_posts = body.get('existing_posts', []) or []
    ep_mode = body.get('ep_mode', 'post')  # 'post' = object_story_id (Gunakan Postingan) | 'creative' = hybrid video_data + CTA
    attribution = body.get('attribution', ['7d_click'])
    url_tags = body.get('url_tags', '') or ''
    # Advantage+ — user bisa pilih audience &/atau placement Advantage+ per campaign
    advantage_audience = bool(body.get('advantage_audience', False))
    advantage_placement = bool(body.get('advantage_placement', False))

    # Mapping atribusi ke Meta attribution_spec (ADSET level — campaign level TIDAK support di v26.0).
    # Frontend kirim LIST opsi yang dicentang (bisa 1+), digabung jadi satu spec.
    # ⚠️ event_type HARUS CLICK_THROUGH/VIEW_THROUGH (bukan CLICK/VIEW) — CLICK/VIEW ditolak Meta.
    ATTRIBUTION_MAP = {
        '7d_click': [{'event_type': 'CLICK_THROUGH', 'window_days': 7}],
        'engaged_video_view': [{'event_type': 'ENGAGED_VIDEO_VIEW', 'window_days': 1}],
        '1d_view': [{'event_type': 'VIEW_THROUGH', 'window_days': 1}],
    }
    if isinstance(attribution, str):
        # Backward compat: string lama
        attribution = [attribution]
    if not attribution:
        attribution = ['7d_click']
    # Gabung spec dari semua opsi dicentang, dedupe (event_type, window_days)
    combined = []
    seen = set()
    for key in attribution:
        for spec in ATTRIBUTION_MAP.get(key, []):
            sig = (spec['event_type'], spec['window_days'])
            if sig not in seen:
                seen.add(sig)
                combined.append(spec)
    attribution_spec = combined if combined else ATTRIBUTION_MAP['7d_click']

    if acc_key not in ACCOUNT_CONFIG:
        return jsonify({'status': 'error', 'message': f'Akun {acc_key} tidak dikenal'})
    if not adsets:
        return jsonify({'status': 'error', 'message': 'Minimal 1 adset'})

    acc = ACCOUNT_CONFIG[acc_key]
    act_id = acc['act']
    event = acc['event']
    objective = OBJECTIVE_MAP.get(event, 'OUTCOME_SALES')

    # Mode Testing ABO wajib ABO (bukan CBO)
    if testing_mode and is_cbo:
        return jsonify({'status': 'error', 'message': 'Mode Testing ABO hanya bisa dipakai dengan ABO (bukan CBO). Matikan CBO dulu.'})

    gender_map = {'F': [2], 'M': [1], 'ALL': [1, 2]}
    cta_map = {
        'Ambil Promo': 'GET_OFFER_VIEW',
        'Dapatkan Penawaran': 'GET_OFFER_VIEW',
        'Pesan Sekarang': 'ORDER_NOW',
        'Beli Sekarang': 'SHOP_NOW',
        'Daftar': 'SIGN_UP',
        'Pelajari Lebih Lanjut': 'LEARN_MORE',
    }
    cta_type = cta_map.get(cta, 'GET_OFFER_VIEW')

    excluded_regions = {
        'regions': [{'key': '1668'}, {'key': '4140'}, {'key': '1683'}, {'key': '4138'}],
        'location_types': ['home', 'recent']
    }

    steps = []
    camp_id = None
    activated = False

    try:
        # STEP 1: Create Campaign (PAUSED)
        # NOTE: attribution_spec TIDAK bisa di campaign level (v26.0 mengabaikannya diam-diam).
        # Dikirim di STEP 2 (adset) — satu spec yang sama untuk semua adset.
        camp_data = {
            'name': name,
            'objective': objective,
            'status': 'PAUSED',
            'special_ad_categories': '[]',
            'buying_type': 'AUCTION',
            'bid_strategy': 'LOWEST_COST_WITHOUT_CAP',
        }
        if is_cbo:
            camp_data['daily_budget'] = daily_budget
        else:
            camp_data['is_adset_budget_sharing_enabled'] = True

        camp_result = _meta_api_post('/' + act_id + '/campaigns', camp_data)
        if 'error' in camp_result:
            raise Exception("Campaign error: " + str(camp_result['error'].get('message', str(camp_result))))
        camp_id = camp_result['id']
        steps.append({'step': 'campaign', 'id': camp_id, 'status': 'ok'})

        # STEP 2: Create Multiple Adsets (ACTIVE)
        adset_ids = []
        for idx, adset_info in enumerate(adsets):
            adset_name = adset_info.get('name', '') or ('Adset ' + str(idx+1))
            age_min = adset_info.get('age_min', 25)
            age_max = adset_info.get('age_max', 50)
            gender = adset_info.get('gender', 'F')
            location = adset_info.get('location', 'no-papua-maluku-barat')
            interests = adset_info.get('interests', [])
            adset_budget = str(adset_info.get('budget', 100000))

            genders = gender_map.get(gender, [2])

            # ⚠️ Subcode 1870189: kalau Advantage+ Audience ON, age_max (batas keras) TIDAK boleh < 65.
            # Tapi Ads Manager tetap bisa tampil "25-45" via age_range (saran usia user).
            # Formula: age_max=65 (dipaksa) + age_range=[min, max_user] (saran, kebaca di UI).
            age_range_suggestion = None
            if advantage_audience and int(age_max) < 65:
                age_range_suggestion = [int(age_min), int(age_max)]
                age_max = 65

            targeting = {
                'age_min': age_min,
                'age_max': age_max,
                'genders': genders,
                'geo_locations': {
                    'countries': ['ID'],
                    'location_types': ['home', 'recent']
                },
                'excluded_geo_locations': excluded_regions if location == 'no-papua-maluku-barat' else {
                    'location_types': ['home', 'recent']
                },
            }

            # Advantage+ Placement: biarkan Meta yang pilih placement otomatis (JANGAN kirim publisher_platforms/positions)
            if not advantage_placement:
                targeting['publisher_platforms'] = ['facebook', 'instagram']
                targeting['facebook_positions'] = ['feed', 'story', 'marketplace']
                targeting['instagram_positions'] = ['stream', 'story', 'reels']
                targeting['device_platforms'] = ['mobile', 'desktop']

            # Advantage+ Audience: 1 = aktif (Meta optimize audience), 0 = manual (default)
            targeting['targeting_automation'] = {'advantage_audience': 1 if advantage_audience else 0}

            # Saran usia user saat Advantage+ Audience ON (age_range dipisah dari age_max 65)
            if age_range_suggestion:
                targeting['age_range'] = age_range_suggestion

            if interests:
                targeting['flexible_spec'] = [{'interests': [{'id': i} for i in interests]}]

            # Targeting custom audience / lookalike (dari Campaign Studio step 2)
            targeting_type = adset_info.get('targeting_type', 'interest')
            ca_id = adset_info.get('custom_audience_id') or adset_info.get('lookalike_id')
            if targeting_type in ('custom', 'lookalike') and ca_id:
                targeting['custom_audiences'] = [{'id': str(ca_id)}]
                # custom audience menggantikan interest — jangan simpan flexible_spec interest
                targeting.pop('flexible_spec', None)
            excl_ca = adset_info.get('exclude_custom_audience_id')
            if excl_ca:
                targeting['excluded_custom_audiences'] = [{'id': str(excl_ca)}]

            adset_data = {
                'name': adset_name,
                'campaign_id': camp_id,
                'status': 'ACTIVE',
                'billing_event': 'IMPRESSIONS',
                'optimization_goal': 'OFFSITE_CONVERSIONS',
                'promoted_object': json.dumps({
                    'pixel_id': acc.get('pixel', PIXEL_ID),
                    'custom_event_type': event,
                }),
                'targeting': json.dumps(targeting),
                'attribution_spec': json.dumps(attribution_spec),
            }
            if objective == 'OUTCOME_SALES':
                adset_data['conversion_strategy'] = 'ALL_CONVERSIONS'
            if not is_cbo:
                adset_data['daily_budget'] = adset_budget

            adset_result = _meta_api_post('/' + act_id + '/adsets', adset_data)
            if 'error' in adset_result:
                err = adset_result['error']
                detail = err.get('message', str(adset_result))
                subcode = err.get('error_subcode', '')
                user_msg = err.get('error_user_msg', '')
                user_title = err.get('error_user_title', '')
                raise Exception("Adset " + str(idx+1) + " error: " + detail + " | subcode: " + str(subcode) + " | user_title: " + str(user_title) + " | user_msg: " + str(user_msg))

            adset_id = adset_result['id']
            adset_ids.append(adset_id)
            steps.append({'step': 'adset_' + str(idx), 'id': adset_id, 'name': adset_name, 'status': 'ok'})

        # STEP 3: Create Ads per Adset (ACTIVE)
        all_ad_ids = []
        for adset_idx, adset_id in enumerate(adset_ids):
            adset_name = adsets[adset_idx].get('name', 'Adset ' + str(adset_idx+1))

            if konten_selected and len(konten_selected) > 0:
                # Mode testing ABO: 1 konten per adset — konten_selected[idx] untuk adset idx
                konten_iter = [(k_idx, konten) for k_idx, konten in enumerate(konten_selected)
                               if (not testing_mode or k_idx == adset_idx)]
                for k_idx, konten in konten_iter:
                    konten_name = konten.get('name', '')
                    konten_id = konten.get('id', '')
                    ad_name = konten.get('ad_name', konten_name)[:50]

                    video_path = _find_video_file(konten_name)

                    # Try GDrive download first (video might not be on server)
                    if konten_id and (not video_path or not os.path.exists(video_path)):
                        try:
                            dl_path = '/tmp/cb_' + str(konten_id) + '_' + str(adset_idx) + '.mp4'
                            import requests as _req
                            import re as _re
                            # Try gdown first (handles Drive virus scan page)
                            try:
                                import gdown as _gd
                                _gd.download(id=konten_id, output=dl_path, quiet=True)
                            except:
                                pass
                            if not os.path.exists(dl_path) or os.path.getsize(dl_path) <= 1024:
                                # Fallback: session-based with confirm cookie
                                _dl_sess = _req.Session()
                                _dl_url1 = 'https://drive.google.com/uc?export=download&id=' + str(konten_id)
                                _dl_resp1 = _dl_sess.get(_dl_url1, timeout=60)
                                if 'confirm=' in _dl_resp1.text:
                                    _dl_c = _re.search(r'confirm=([0-9A-Za-z]+)', _dl_resp1.text)
                                    if _dl_c:
                                        _dl_url2 = 'https://drive.google.com/uc?export=download&confirm=' + _dl_c.group(1) + '&id=' + str(konten_id)
                                        _dl_resp2 = _dl_sess.get(_dl_url2, timeout=300, stream=True)
                                        if _dl_resp2.status_code == 200:
                                            with open(dl_path, 'wb') as _f:
                                                for _chunk in _dl_resp2.iter_content(chunk_size=8192):
                                                    _f.write(_chunk)
                                elif _dl_resp1.status_code == 200:
                                    with open(dl_path, 'wb') as _f:
                                        for _chunk in _dl_resp1.iter_content(chunk_size=8192):
                                            _f.write(_chunk)
                            if os.path.exists(dl_path) and os.path.getsize(dl_path) > 1024:
                                video_path = dl_path
                        except:
                            pass
                            try:
                                import gdown as _gd
                                _gd.download(id=konten_id, output=dl_path, quiet=True)
                                if os.path.exists(dl_path) and os.path.getsize(dl_path) > 1024:
                                    video_path = dl_path
                            except:
                                pass

                    full_ad_name = ad_name[:30] + '-A' + str(adset_idx+1)

                    if video_path and os.path.exists(video_path):
                        try:
                            video_id = _upload_video_meta(act_id, video_path)
                            steps.append({'step': 'video_' + str(adset_idx) + '_' + str(k_idx), 'id': video_id, 'status': 'ok', 'konten': konten_name, 'adset': adset_name})

                            try:
                                image_hash = _gen_upload_thumb(act_id, video_path)
                            except:
                                image_hash = None

                            _video_data = {
                                'video_id': video_id,
                                'title': headline,
                                'message': primary_text,
                                'call_to_action': {'type': cta_type, 'value': {'link': landing_url}},
                            }
                            if image_hash:
                                _video_data['image_hash'] = image_hash
                            else:
                                _video_data['image_url'] = 'https://report.anaksehatgeneros.com/static/thumbnail_generos.jpg'

                            creative_data = {
                                'name': name[:30] + ' - ' + konten_name[:20],
                                'object_story_spec': _oss(acc, video_data=_video_data),
                                'url_tags': url_tags,
                            }
                            cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                            if 'error' in cr_result:
                                raise Exception("Creative error for " + konten_name + ": " + str(cr_result['error'].get('message', str(cr_result))))
                            cr_id = cr_result['id']

                            ad_data = {
                                'name': full_ad_name,
                                'adset_id': adset_id,
                                'creative': json.dumps({'creative_id': cr_id}),
                                'status': 'ACTIVE',
                            }
                            ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                            if 'error' in ad_result:
                                raise Exception("Ad error for " + konten_name + ": " + str(ad_result['error'].get('message', str(ad_result))))
                            all_ad_ids.append(ad_result['id'])
                            steps.append({'step': 'ad_' + str(adset_idx) + '_' + str(k_idx), 'id': ad_result['id'], 'status': 'ok', 'konten': konten_name, 'adset': adset_name})
                        except Exception as e:
                            # Fallback: link ad
                            creative_data = {
                                'name': name[:30] + ' - ' + konten_name[:20],
                                'object_story_spec': _oss(acc, link_data={
                                    'link': landing_url,
                                    'message': primary_text,
                                    'name': headline,
                                    'call_to_action': {'type': cta_type},
                                }),
                                'url_tags': url_tags,
                            }
                            cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                            if 'error' in cr_result:
                                raise Exception("Creative error (fallback) for " + konten_name + ": " + str(cr_result['error'].get('message', str(cr_result))))
                            cr_id = cr_result['id']
                            steps.append({'step': 'creative_' + str(adset_idx) + '_' + str(k_idx), 'id': cr_id, 'status': 'ok_fallback', 'konten': konten_name, 'adset': adset_name})
                            ad_data = {
                                'name': full_ad_name,
                                'adset_id': adset_id,
                                'creative': json.dumps({'creative_id': cr_id}),
                                'status': 'ACTIVE',
                            }
                            ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                            if 'error' in ad_result:
                                raise Exception("Ad error (fallback) for " + konten_name + ": " + str(ad_result['error'].get('message', str(ad_result))))
                            all_ad_ids.append(ad_result['id'])
                            steps.append({'step': 'ad_' + str(adset_idx) + '_' + str(k_idx), 'id': ad_result['id'], 'status': 'ok_fallback', 'konten': konten_name, 'adset': adset_name})
                    else:
                        creative_data = {
                            'name': name[:30] + ' - ' + konten_name[:20],
                            'object_story_spec': _oss(acc, link_data={
                                'link': landing_url,
                                'message': primary_text,
                                'name': headline,
                                'call_to_action': {'type': cta_type},
                            }),
                            'url_tags': url_tags,
                        }
                        cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                        if 'error' in cr_result:
                            raise Exception("Creative error for " + konten_name + ": " + str(cr_result['error'].get('message', str(cr_result))))
                        cr_id = cr_result['id']
                        steps.append({'step': 'creative_' + str(adset_idx) + '_' + str(k_idx), 'id': cr_id, 'status': 'ok_fallback', 'konten': konten_name, 'adset': adset_name})
                        ad_data = {
                            'name': full_ad_name,
                            'adset_id': adset_id,
                            'creative': json.dumps({'creative_id': cr_id}),
                            'status': 'ACTIVE',
                        }
                        ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                        if 'error' in ad_result:
                            raise Exception("Ad error for " + konten_name + ": " + str(ad_result['error'].get('message', str(ad_result))))
                        all_ad_ids.append(ad_result['id'])
                        steps.append({'step': 'ad_' + str(adset_idx) + '_' + str(k_idx), 'id': ad_result['id'], 'status': 'ok', 'konten': konten_name, 'adset': adset_name})

            # Existing posts
            if existing_post_ids and len(existing_post_ids) > 0:
                ep_info = {str(p.get('post_id', '')): p for p in existing_posts}
                for ep_idx, ep_id in enumerate(existing_post_ids):
                    ep_id = ep_id.strip()
                    if not ep_id:
                        continue
                    try:
                        info = ep_info.get(ep_id, {}) or ep_info.get(ep_id.split('_')[-1], {})
                        ep_msg = info.get('message', '') or primary_text or 'Promo spesial Generos!'
                        ep_thumb = info.get('thumbnail', '') or ''
                        ep_video_url = info.get('video_url', '') or ''
                        ep_ad_name = (info.get('ad_name') or '').strip() or f"{name[:45]} - EP {str(ep_idx+1)} - A{str(adset_idx+1)}"
                        ep_created = ''
                        # Mode 'post' = object_story_id (Gunakan Postingan) — CTA/link ikut post asli, TIDAK custom
                        if ep_mode == 'post':
                            ep_full_id = ep_id if '_' in ep_id else f"{acc['page_id']}_{ep_id}"
                            creative_data = {
                                'name': name[:35] + ' - EP ' + str(ep_idx+1) + ' - A' + str(adset_idx+1),
                                'object_story_id': ep_full_id,
                            }
                            cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                            if 'error' in cr_result:
                                steps.append({'step': 'ep_creative_' + str(adset_idx) + '_' + str(ep_idx), 'status': 'error', 'message': cr_result['error'].get('message', '')})
                                continue
                            cr_id = cr_result['id']
                            steps.append({'step': 'ep_creative_' + str(adset_idx) + '_' + str(ep_idx), 'id': cr_id, 'status': 'ok', 'mode': 'object_story_id'})
                            ad_data = {
                                'name': ep_ad_name,
                                'adset_id': adset_id,
                                'creative': json.dumps({'creative_id': cr_id}),
                                'status': 'ACTIVE',
                            }
                            ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                            if 'error' in ad_result:
                                steps.append({'step': 'ep_ad_' + str(adset_idx) + '_' + str(ep_idx), 'status': 'error', 'message': ad_result['error'].get('message', '')})
                                continue
                            all_ad_ids.append(ad_result['id'])
                            steps.append({'step': 'ep_ad_' + str(adset_idx) + '_' + str(ep_idx), 'id': ad_result['id'], 'status': 'ok'})
                            continue
                        # ⚠️ EP creative hybrid: coba reuse video post → video_data + CTA,
                        # kalau gagal/tidak ada video → fallback link_data. JANGAN object_story_id
                        # polos (post organik tanpa CTA → error di campaign sales).
                        video_uploaded_id = ''
                        if ep_video_url:
                            try:
                                import requests as _rq
                                ep_vid_path = f'/tmp/ep_{ep_idx}_{acc_key}.mp4'
                                if os.path.exists(ep_vid_path):
                                    os.remove(ep_vid_path)
                                _r = _rq.get(ep_video_url, timeout=120, stream=True)
                                if _r.status_code == 200 and int(_r.headers.get('content-length', 0) or 0) > 1024:
                                    with open(ep_vid_path, 'wb') as _f:
                                        for _chunk in _r.iter_content(chunk_size=8192):
                                            _f.write(_chunk)
                                if os.path.exists(ep_vid_path) and os.path.getsize(ep_vid_path) > 1024:
                                    video_uploaded_id = _upload_video_meta(act_id, ep_vid_path) or ''
                                    print(f"[CS] EP video reused OK: {video_uploaded_id} for {ep_id}", flush=True)
                                try:
                                    os.remove(ep_vid_path)
                                except:
                                    pass
                            except Exception as _ep_v_e:
                                video_uploaded_id = ''
                                print(f"[CS] EP video reuse failed for {ep_id}: {_ep_v_e}", flush=True)

                        creative_data = None
                        if video_uploaded_id:
                            _thumb_url = ep_thumb or 'https://report.anaksehatgeneros.com/static/thumbnail_generos.jpg'
                            creative_data = {
                                'name': name[:35] + ' - EP ' + str(ep_idx+1) + ' - A' + str(adset_idx+1),
                                'object_story_spec': _oss(acc, video_data={
                                    'video_id': video_uploaded_id,
                                    'message': (ep_msg or primary_text)[:1000],
                                    'title': (headline or 'Generos')[:100],
                                    'image_url': _thumb_url,
                                    'call_to_action': {'type': cta_type, 'value': {'link': landing_url}},
                                }),
                                'url_tags': url_tags,
                            }
                        else:
                            creative_data = {
                                'name': name[:35] + ' - EP ' + str(ep_idx+1) + ' - A' + str(adset_idx+1),
                                'object_story_spec': _oss(acc, link_data={
                                    'link': landing_url,
                                    'message': (ep_msg or primary_text)[:1000],
                                    'name': (headline or 'Generos')[:100],
                                    'call_to_action': {'type': cta_type},
                                }),
                                'url_tags': url_tags,
                            }
                        cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                        if 'error' in cr_result:
                            steps.append({'step': 'ep_creative_' + str(adset_idx) + '_' + str(ep_idx), 'status': 'error', 'message': cr_result['error'].get('message', '')})
                            continue
                        cr_id = cr_result['id']
                        steps.append({'step': 'ep_creative_' + str(adset_idx) + '_' + str(ep_idx), 'id': cr_id, 'status': 'ok', 'mode': 'video' if video_uploaded_id else 'link'})
                        ad_data = {
                            'name': ep_ad_name,
                            'adset_id': adset_id,
                            'creative': json.dumps({'creative_id': cr_id}),
                            'status': 'ACTIVE',
                        }
                        ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                        if 'error' in ad_result:
                            steps.append({'step': 'ep_ad_' + str(adset_idx) + '_' + str(ep_idx), 'status': 'error', 'message': ad_result['error'].get('message', '')})
                            continue
                        all_ad_ids.append(ad_result['id'])
                        steps.append({'step': 'ep_ad_' + str(adset_idx) + '_' + str(ep_idx), 'id': ad_result['id'], 'status': 'ok'})
                    except Exception as ep_e:
                        steps.append({'step': 'ep_' + str(adset_idx) + '_' + str(ep_idx), 'status': 'error', 'message': str(ep_e)})

            # No konten and no existing posts
            if not konten_selected and not existing_post_ids:
                creative_data = {
                    'name': name[:40] + ' - A' + str(adset_idx+1),
                    'object_story_spec': _oss(acc, link_data={
                        'link': landing_url,
                        'message': primary_text,
                        'name': headline,
                        'call_to_action': {'type': cta_type},
                    }),
                    'url_tags': url_tags,
                }
                cr_result = _meta_api_post('/' + act_id + '/adcreatives', creative_data)
                if 'error' in cr_result:
                    raise Exception("Creative error for " + adset_name + ": " + str(cr_result['error'].get('message', str(cr_result))))
                cr_id = cr_result['id']
                steps.append({'step': 'creative_' + str(adset_idx), 'id': cr_id, 'status': 'ok', 'adset': adset_name})
                ad_data = {
                    'name': name[:50] + ' - A' + str(adset_idx+1),
                    'adset_id': adset_id,
                    'creative': json.dumps({'creative_id': cr_id}),
                    'status': 'ACTIVE',
                }
                ad_result = _meta_api_post('/' + act_id + '/ads', ad_data)
                if 'error' in ad_result:
                    raise Exception("Ad error for " + adset_name + ": " + str(ad_result['error'].get('message', str(ad_result))))
                all_ad_ids.append(ad_result['id'])
                steps.append({'step': 'ad_' + str(adset_idx), 'id': ad_result['id'], 'status': 'ok', 'adset': adset_name})

        # Activate campaign now
        _activate = _meta_api_post('/' + camp_id, {'status': 'ACTIVE'})
        if 'error' in _activate:
            raise Exception("Activate error: " + str(_activate['error'].get('message', str(_activate))))
        activated = True
        steps.append({'step': 'activate', 'id': camp_id, 'status': 'ok'})

        # Send Telegram notification
        try:
            _tg_token = ''
            try:
                _cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
                with open(_cfg_path, encoding='utf-8') as _f:
                    _cfg = json.load(_f)
                    _tg_token = _cfg.get('telegram_bot_token', '')
            except:
                pass
            if not _tg_token:
                try:
                    from config import TELEGRAM_BOT_TOKEN as _tgt
                    _tg_token = _tgt
                except:
                    pass
            if _tg_token:
                _mode_label = 'CBO' if is_cbo else 'ABO'
                # Fix budget report: CBO = budget campaign, ABO = total budget semua adset.
                # ABO nggak kirim daily_budget (frontend cuma kirim pas CBO) → fallback 250000 salah.
                _report_budget = int(daily_budget) if is_cbo else sum(int((a.get('budget') or 0) or 0) for a in adsets)
                _notif_text = '🚀 Campaign Baru\n' + '━'*25 + '\n🔥 ' + name + '\n👤 ' + _acc_display(acc_key) + '\n💰 Rp' + str(_report_budget) + '/hari (' + _mode_label + ')\n📦 ' + str(len(adsets)) + ' adset | ' + str(len(all_ad_ids)) + ' iklan (ACTIVE)'
                import urllib.parse as _up
                _api_url = 'https://api.telegram.org/bot' + _tg_token + '/sendMessage'
                _tg_payload = _up.urlencode({
                    'chat_id': '-1003990111670',
                    'message_thread_id': '1958',
                    'text': _notif_text,
                    'parse_mode': 'HTML',
                })
                import subprocess as _sub2
                _sub2.run(['curl', '-s', '-X', 'POST', '-d', _tg_payload,
                          '--connect-timeout', '5', '--max-time', '10', _api_url],
                         capture_output=True, timeout=15)
        except:
            pass

        # Kumpulin error dari steps (creative/ad gagal) biar nggak senyap "0 iklan"
        _err_msgs = [s.get('message', '') for s in steps if s.get('status') == 'error' and s.get('message')]
        _err_summary = ''
        if _err_msgs:
            _uniq = list(dict.fromkeys(_err_msgs))[:3]
            _err_summary = ' | ⚠️ ' + str(len(_err_msgs)) + ' error: ' + '; '.join(_uniq)

        return jsonify({
            'status': 'success',
            'message': 'Campaign ' + name + ' berhasil dibuat (ACTIVE) - ' + str(len(adsets)) + ' adset, ' + str(len(all_ad_ids)) + ' iklan' + _err_summary,
            'campaign_id': camp_id,
            'adset_ids': adset_ids,
            'ad_ids': all_ad_ids,
            'steps': steps,
        })

    except Exception as e:
        # Rollback: hapus campaign baru kalau gagal di tengah (cegah yatim/duplikat)
        try:
            if camp_id and not activated:
                _meta_api_post('/' + str(camp_id), {'status': 'DELETED'})
                steps.append({'step': 'rollback', 'id': camp_id, 'status': 'ok'})
        except Exception:
            pass
        return jsonify({
            'status': 'error',
            'message': str(e),
            'steps': steps,
        })






@app.route('/api/campaign-builder/prepare-video', methods=['POST'])
@admin_required
def api_prepare_video():
    """Upload video to Meta, return video_id + image_hash. Called by Hostinger proxy."""
    body = request.get_json(force=True) or {}
    konten_name = body.get('name', '')
    acc_key = body.get('account', 'G2')
    
    if not konten_name:
        return jsonify({'status': 'error', 'message': 'name required'})
    
    if acc_key not in ACCOUNT_CONFIG:
        return jsonify({'status': 'error', 'message': f'Unknown account {acc_key}'})
    
    act_id = ACCOUNT_CONFIG[acc_key]['act']
    video_path = _find_video_file(konten_name)
    
    if not video_path or not os.path.exists(video_path):
        return jsonify({'status': 'error', 'message': f'Video not found: {konten_name}'})
    
    try:
        video_id = _upload_video_meta(act_id, video_path)
        image_hash = _gen_upload_thumb(act_id, video_path)
        return jsonify({
            'status': 'success',
            'video_id': video_id,
            'image_hash': image_hash,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/page-posts')
@admin_required
def api_page_posts():
    """Fetch posts from a Facebook page using page access token."""
    page_id = request.args.get('page_id', '').strip()
    if not page_id:
        return jsonify({'status': 'error', 'message': 'page_id required'}), 400
    
    token = _get_meta_token()
    if not token:
        return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 401
    
    try:
        # Step 1: Get page access token from /me/accounts
        pages_url = f"https://graph.facebook.com/v26.0/me/accounts?fields=id,name,access_token&access_token={token}"
        with urllib.request.urlopen(pages_url, timeout=10) as resp:
            pages_data = json.loads(resp.read())
        
        # Find the matching page
        page_token = None
        for p in pages_data.get('data', []):
            if p.get('id') == page_id:
                page_token = p.get('access_token')
                break
        
        if not page_token:
            return jsonify({'status': 'error', 'message': 'Page tidak ditemukan atau token tidak memiliki akses ke page ini'}), 403
        
        # Step 2: Fetch posts using page access token
        posts_url = (f"https://graph.facebook.com/v26.0/{page_id}/posts"
                     f"?fields=id,message,created_time,full_picture,permalink_url,attachments{{media_type,media,url,type}}"
                     f"&limit=25&access_token={page_token}")
        with urllib.request.urlopen(posts_url, timeout=15) as resp:
            data = json.loads(resp.read())
        
        posts = []
        for p in data.get('data', []):
            atts = (p.get('attachments') or {}).get('data', []) or []
            video_url = ''
            thumb = p.get('full_picture', '')
            for att in atts:
                media = att.get('media') or {}
                if att.get('media_type') == 'video' or media.get('source'):
                    video_url = media.get('source', '') or video_url
                if not thumb and media.get('image'):
                    thumb = media['image']
            posts.append({
                'post_id': p.get('id', ''),
                'message': p.get('message', ''),
                'created': p.get('created_time', ''),
                'thumbnail': thumb,
                'url': p.get('permalink_url', ''),
                'video_url': video_url,
            })
        
        return jsonify({'status': 'ok', 'posts': posts})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/post-by-id')
@admin_required
def api_post_by_id():
    """Fetch single Facebook post by ID (buat fitur ID Postingan di Campaign Studio)."""
    page_id = request.args.get('page_id', '').strip()
    post_id = request.args.get('post_id', '').strip()
    if not page_id or not post_id:
        return jsonify({'status': 'error', 'message': 'page_id + post_id required'}), 400

    token = _get_meta_token()
    if not token:
        return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 401

    try:
        # Step 1: Get page access token
        pages_url = f"https://graph.facebook.com/v26.0/me/accounts?fields=id,name,access_token&access_token={token}"
        with urllib.request.urlopen(pages_url, timeout=10) as resp:
            pages_data = json.loads(resp.read())
        page_token = None
        for p in pages_data.get('data', []):
            if p.get('id') == page_id:
                page_token = p.get('access_token')
                break
        if not page_token:
            return jsonify({'status': 'error', 'message': 'Page tidak ditemukan atau token tidak memiliki akses ke page ini'}), 403

        # Step 2: Normalize post id — kalau user kasih full "pageid_postid", pakai apa adanya; kalau cuma postid, gabung
        full_id = post_id if '_' in post_id else f"{page_id}_{post_id}"
        post_url = (f"https://graph.facebook.com/v26.0/{full_id}"
                    f"?fields=id,message,created_time,full_picture,permalink_url,attachments{{media_type,media,url,type}}"
                    f"&access_token={page_token}")
        with urllib.request.urlopen(post_url, timeout=15) as resp:
            data = json.loads(resp.read())

        # Post portion buat payload (biar backend bikin object_story_id pageid_postid bener)
        post_portion = full_id.split('_')[-1]
        atts = (data.get('attachments') or {}).get('data', []) or []
        video_url = ''
        thumb = data.get('full_picture', '')
        for att in atts:
            media = att.get('media') or {}
            if att.get('media_type') == 'video' or media.get('source'):
                video_url = media.get('source', '') or video_url
            if not thumb and media.get('image'):
                thumb = media['image']
        return jsonify({
            'status': 'ok',
            'post': {
                'post_id': post_portion,
                'full_id': full_id,
                'message': data.get('message', ''),
                'created': data.get('created_time', ''),
                'thumbnail': thumb,
                'url': data.get('permalink_url', ''),
                'video_url': video_url,
            }
        })
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = body.get('error', {}).get('message', str(e))
        except Exception:
            msg = str(e)
        return jsonify({'status': 'error', 'message': f'Postingan tidak ditemukan: {msg}'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/ads-posts')
@admin_required
def api_ads_posts():
    """Fetch running ads for an account."""
    act_key = request.args.get('act_key', '').strip().upper()
    if not act_key or act_key not in ACCOUNTS:
        return jsonify({'status': 'error', 'message': 'Account key tidak valid'}), 400
    
    account_id = ACCOUNTS[act_key]['id']
    token = _get_meta_token()
    if not token:
        return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 401
    
    try:
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=30)).isoformat()
        until = date.today().isoformat()
        
        url = (f"https://graph.facebook.com/v26.0/{account_id}/ads"
               f"?fields=id,name,status,adset{{name}},"
               f"creative{{id,object_story_id,video_id,thumbnail_url,image_url,title,body,object_story_spec}},"
               f"insights.time_range(%7B%22since%22%3A%22{since}%22%2C%22until%22%3A%22{until}%22%7D)%7Bspend,actions%7D"
               f"&effective_status=%5B%22ACTIVE%22%5D"
               f"&limit=100&access_token={token}")
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        
        ads = []
        for ad in data.get('data', []):
            cr = ad.get('creative', {})
            oss = cr.get('object_story_spec', {})
            link_data = oss.get('link_data', oss.get('video_data', {}))
            
            # Get object_story_id from page post id if exists
            page_post_id = oss.get('page_id', '')
            object_story_id = ""
            if page_post_id and link_data.get('call_to_action', {}).get('value', {}).get('link'):
                pass  # custom link ad
            
            # Check if it uses an existing page post
            is_existing = 'page_post_id' in str(oss) or bool(oss.get('page_id') and not link_data.get('body'))
            
            obj_story_id = cr.get('object_story_id', '')
            is_existing = bool(obj_story_id)
            
            # Skip ads without object_story_id (can't use as existing post)
            if not obj_story_id:
                continue
            
            # Extract spend from insights (last 30 days)
            ins_list = ad.get('insights', {}).get('data', [])
            ins = ins_list[0] if ins_list else {}
            spend = float(ins.get('spend', 0))
            results = 0
            if spend > 0:
                for act in ins.get('actions', []):
                    if act.get('action_type') in ('lead', 'add_to_cart', 'purchase', 'offsite_conversion.fb_pixel_lead', 'offsite_conversion.fb_pixel_add_to_cart'):
                        results = int(act.get('value', 0))
                        break
            
            # Only show ads with spending
            if spend <= 0:
                continue
            
            ads.append({
                'id': ad.get('id', ''),
                'primary_text': link_data.get('body', cr.get('body', ad.get('name', ''))),
                'headline': link_data.get('title', cr.get('title', '')),
                'thumbnail': cr.get('thumbnail_url', cr.get('image_url', '')),
                'adset_name': ad.get('adset', {}).get('name', ''),
                'status': ad.get('status', ''),
                'spend': round(spend, 2),
                'results': results,
                'video_id': cr.get('video_id', ''),
                'object_story_id': obj_story_id,
                'is_existing_post': bool(obj_story_id),
            })
        
        return jsonify({'status': 'ok', 'ads': ads})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/search-interests')
@admin_required
def api_search_interests():
    """Search Meta Ads interests via API."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'data': []})
    
    token = META_TOKEN or ''
    if not token:
        try:
            import json as _json
            _cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
            with open(_cfg_path, encoding='utf-8') as _f:
                _cfg = _json.load(_f)
                token = _cfg.get('meta_token', '') or ''
        except:
            pass
    
    if not token:
        return jsonify({'error': 'Meta token tidak tersedia'}), 401
    
    try:
        url = f"https://graph.facebook.com/v26.0/search?type=adinterest&q={urllib.parse.quote(q)}&limit=10&access_token={token}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== POST FANPAGE — Posting ke Fanspage dari Bank Konten =====

@app.route('/page-post')
@admin_required
def page_post_page():
    """Halaman Post Fanpage (studio-style: pilih page → pilih konten → caption & judul → posting)."""
    return render_template('page_post.html')


@app.route('/api/page-post/pages')
@admin_required
def api_page_post_pages():
    """List fanspage yang bisa diposting (dari /me/accounts)."""
    token = _get_meta_token()
    if not token:
        return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 401
    try:
        url = f'https://graph.facebook.com/v26.0/me/accounts?fields=id,name,access_token,category&access_token={token}'
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        pages = [{'id': p.get('id', ''), 'name': p.get('name', ''), 'category': p.get('category', '')}
                 for p in data.get('data', [])]
        return jsonify({'status': 'ok', 'pages': pages})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/page-post/konten')
@admin_required
def api_page_post_konten():
    """List konten dari bank konten (video + gambar), sesuai fanpage yang dipilih."""
    try:
        page_id = request.args.get('page_id', '') or ''
        page_name = request.args.get('page_name', '') or ''
        path, brand = _bank_konten_file_for_page(page_id, page_name)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            files = data.get('files', [])
            for fi in files:
                name = fi.get('name', '')
                ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
                fi['content_type'] = 'image' if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif') else 'video'
            return jsonify({'status': 'ok', 'files': files, 'brand': brand})
        return jsonify({'status': 'error', 'message': 'Data belum tersedia', 'files': [], 'brand': brand})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'files': []})


@app.route('/api/page-post/history')
@admin_required
def api_page_post_history():
    """Riwayat posting fanspage — paginated (default 10 per halaman), optional filter page_id."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        page_id = request.args.get('page_id', '') or None
        if page < 1:
            page = 1
        if per_page < 1 or per_page > 100:
            per_page = 10
        rows, total = get_page_posts(page=page, per_page=per_page, page_id=page_id)
        # Fix timezone: DB simpan naive WIB, Flask serialize jadi GMT → JS +7 lagi.
        # Konversi ke ISO WIB eksplisit biar tampil jam yang benar.
        rows = [dict(r) for r in rows]
        for r in rows:
            ca = r.get('created_at')
            if hasattr(ca, 'strftime'):
                r['created_at'] = ca.strftime('%Y-%m-%dT%H:%M:%S+07:00')
        pages = max(1, (total + per_page - 1) // per_page)
        return jsonify({'status': 'ok', 'posts': rows, 'total': total,
                        'page': page, 'per_page': per_page, 'pages': pages})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/page-post/history-pages')
@admin_required
def api_page_post_history_pages():
    """Distinct fanspage list dari riwayat — buat dropdown filter."""
    try:
        rows = get_page_post_pages()
        return jsonify({'status': 'ok', 'pages': rows})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/page-post/publish', methods=['POST'])
@admin_required
def api_page_post_publish():
    """Publish post ke fanspage: download Drive → upload Meta → simpan riwayat."""

    def _notify_success(page_name, content_name, title, permalink, kind):
        """Kirim notifikasi ke grup Telegram saat posting berhasil."""
        import subprocess as _sp, urllib.parse as _up
        _gid = '-1003990111670'
        _txt = (
            f"✅ <b>Post Fanpage Berhasil</b>\n"
            f"📄 Fanspage: <b>{page_name}</b>\n"
            f"🎬 Konten: {content_name}\n"
            f"📝 Judul: {title or '-'}\n"
            f"🔗 <a href='{permalink}'>Lihat Postingan</a>"
        )
        _url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        _payload = _up.urlencode({'chat_id': _gid, 'text': _txt, 'parse_mode': 'HTML', 'message_thread_id': '1958'})
        try:
            _r = _sp.run(['curl', '-s', '-X', 'POST', '-d', _payload, '--connect-timeout', '5', '--max-time', '10', _url],
                         capture_output=True, text=True, timeout=15)
            _ok = '"ok":true' in _r.stdout
            print(f"[PP-NOTIF] {kind} -> {'OK' if _ok else 'FAIL'} | {_r.stdout[:80]}", flush=True)
        except Exception as _e:
            print(f"[PP-NOTIF] Error: {_e}", flush=True)

    body = request.get_json() or {}
    page_id = body.get('page_id', '')
    page_name = body.get('page_name', '')
    content_id = body.get('content_id', '')
    content_name = body.get('content_name', '')
    content_type = body.get('content_type', 'video')
    title = (body.get('title') or '').strip()
    caption = (body.get('caption') or '').strip()
    link = (body.get('link') or '').strip()
    cta = (body.get('cta') or '').strip()
    thumbnail = (body.get('thumbnail') or '').strip()
    # Link opsional — nempel di post biar bisa diklik (foto: link attachment, video: di description)
    caption_final = caption + (('\n\n' + link) if link else '')

    if not page_id or not content_id:
        return jsonify({'status': 'error', 'message': 'Page dan konten wajib diisi'}), 400
    if content_type == 'video' and not title:
        return jsonify({'status': 'error', 'message': 'Judul video wajib diisi'}), 400
    if not caption:
        return jsonify({'status': 'error', 'message': 'Caption wajib diisi'}), 400
    if cta and not link:
        return jsonify({'status': 'error', 'message': 'Link URL wajib diisi kalau Call to Action dipilih'}), 400

    token = _get_meta_token()
    if not token:
        return jsonify({'status': 'error', 'message': 'Meta token tidak tersedia'}), 401

    try:
        # Step 1: Ambil page token dari /me/accounts
        pages_url = f'https://graph.facebook.com/v26.0/me/accounts?fields=id,name,access_token&access_token={token}'
        with urllib.request.urlopen(pages_url, timeout=15) as resp:
            pages_data = json.loads(resp.read())
        page_token = ''
        for p in pages_data.get('data', []):
            if p.get('id') == page_id:
                page_token = p.get('access_token', '')
                break
        if not page_token:
            return jsonify({'status': 'error', 'message': 'Page token tidak ditemukan'}), 400

        # Jalur LINK POST + CTA — post baru langsung punya tombol CTA (buat object_story_id "Gunakan Postingan")
        if cta and link:
            import subprocess as _sp
            _title_file = f'/tmp/pagepost_{content_id}_title.txt'
            _cap_file = f'/tmp/pagepost_{content_id}_caption.txt'
            with open(_title_file, 'w', encoding='utf-8') as _f:
                _f.write(title[:100])
            with open(_cap_file, 'w', encoding='utf-8') as _f:
                _f.write(caption_final[:2000])
            _cta_map = {
                'Ambil Promo': 'GET_OFFER_VIEW',
                'Dapatkan Penawaran': 'GET_OFFER_VIEW',
                'Beli Sekarang': 'SHOP_NOW',
                'Pelajari Lebih Lanjut': 'LEARN_MORE',
                'Daftar': 'SIGN_UP',
                'Pesan Sekarang': 'ORDER_NOW',
            }
            _cta_type = _cta_map.get(cta, 'GET_OFFER_VIEW')
            _cmd = [
                'curl', '-s', '-X', 'POST',
                f'https://graph.facebook.com/v26.0/{page_id}/feed',
                '-F', f'access_token={page_token}',
                '-F', f'message=<{_cap_file}',
                '-F', f'link={link}',
                '-F', 'published=true',
            ]
            if thumbnail:
                _cmd += ['-F', f'picture={thumbnail}']
            if title:
                _cmd += ['-F', f'name=<{_title_file}']
            _cmd += ['-F', f'call_to_action={json.dumps({"type": _cta_type, "value": {"link": link}})}']
            _up_resp = _sp.run(_cmd, capture_output=True, timeout=300)
            _up_json = json.loads(_up_resp.stdout.decode() or '{}')
            _err_msg = _up_json.get('error', {}).get('message', '') if isinstance(_up_json.get('error'), dict) else str(_up_json.get('error', ''))
            # Meta tolak custom picture/name kalau domain URL tidak terverifikasi ownership —
            # retry dengan command TANPA override (picture & name), biarkan Meta scrape og: tags dari link.
            if 'error' in _up_json and 'Only owners of the URL' in _err_msg and (thumbnail or title):
                _cmd_no_override = [
                    'curl', '-s', '-X', 'POST',
                    f'https://graph.facebook.com/v26.0/{page_id}/feed',
                    '-F', f'access_token={page_token}',
                    '-F', f'message=<{_cap_file}',
                    '-F', f'link={link}',
                    '-F', 'published=true',
                    '-F', f'call_to_action={json.dumps({"type": _cta_type, "value": {"link": link}})}',
                ]
                _up_resp = _sp.run(_cmd_no_override, capture_output=True, timeout=300)
                _up_json = json.loads(_up_resp.stdout.decode() or '{}')
                _err_msg = _up_json.get('error', {}).get('message', '') if isinstance(_up_json.get('error'), dict) else str(_up_json.get('error', ''))
            if 'error' in _up_json:
                raise Exception(_err_msg or str(_up_json['error']))
            _post_id = str(_up_json.get('id', '') or '').strip()
            if not _post_id or 'error' in _post_id.lower():
                raise Exception('Meta balas tanpa ID post — postingan tidak terbit (response: %s)' % str(_up_json)[:200])
            _pid_short = _post_id.split('_')[-1] if '_' in _post_id else _post_id
            _permalink = f'https://www.facebook.com/{page_id}/posts/{_pid_short}'
            save_page_post(page_id, page_name, content_id, content_name, 'link_cta',
                           title, caption_final, _post_id, _permalink, 'published',
                           '', session.get('admin', 'admin'))
            for _tmp in (_title_file, _cap_file):
                try:
                    os.remove(_tmp)
                except:
                    pass
            _notify_success(page_name, content_name, title, _permalink, 'link_cta')
            return jsonify({'status': 'ok', 'message': 'Post link + CTA berhasil dipublish', 'meta_post_id': _post_id, 'permalink': _permalink})

        # Step 2: Download dari Google Drive
        import requests as _req
        import re as _re
        ext = content_name.rsplit('.', 1)[-1].lower() if '.' in content_name else 'mp4'
        if ext not in ('mp4', 'jpg', 'jpeg', 'png', 'webp', 'gif'):
            ext = 'mp4'
        dl_path = f'/tmp/pagepost_{content_id}.{ext}'
        if os.path.exists(dl_path):
            os.remove(dl_path)
        _dl_ok = False
        try:
            import gdown as _gd
            _gd.download(id=content_id, output=dl_path, quiet=True)
            if os.path.exists(dl_path) and os.path.getsize(dl_path) > 1024:
                _dl_ok = True
        except:
            pass
        if not _dl_ok:
            _dl_sess = _req.Session()
            _dl_url1 = f'https://drive.google.com/uc?export=download&id={content_id}'
            _dl_resp1 = _dl_sess.get(_dl_url1, timeout=60)
            if 'confirm=' in _dl_resp1.text:
                _dl_c = _re.search(r'confirm=([0-9A-Za-z]+)', _dl_resp1.text)
                if _dl_c:
                    _dl_resp2 = _dl_sess.get(
                        f'https://drive.google.com/uc?export=download&confirm={_dl_c.group(1)}&id={content_id}',
                        timeout=300, stream=True)
                    with open(dl_path, 'wb') as _f:
                        for _chunk in _dl_resp2.iter_content(chunk_size=8192):
                            _f.write(_chunk)
            elif _dl_resp1.status_code == 200:
                with open(dl_path, 'wb') as _f:
                    for _chunk in _dl_resp1.iter_content(chunk_size=8192):
                        _f.write(_chunk)
        if not os.path.exists(dl_path) or os.path.getsize(dl_path) <= 1024:
            raise Exception('Download dari Google Drive gagal')
        print(f"[PP] Downloaded {content_name} ({os.path.getsize(dl_path)} bytes)", flush=True)

        # Step 3: Upload ke Meta (page videos / page photos)
        import subprocess
        # Tulis title/caption ke file temp (utf-8) — hindari UnicodeEncodeError ascii
        # di subprocess args (locale Hostinger = ascii). curl baca via "fieldname=<file".
        _title_file = f'/tmp/pagepost_{content_id}_title.txt'
        _cap_file = f'/tmp/pagepost_{content_id}_caption.txt'
        _link_file = f'/tmp/pagepost_{content_id}_link.txt'
        with open(_title_file, 'w', encoding='utf-8') as _f:
            _f.write(title[:100])
        with open(_cap_file, 'w', encoding='utf-8') as _f:
            _f.write(caption_final[:2000])
        if link:
            with open(_link_file, 'w', encoding='utf-8') as _f:
                _f.write(link[:500])
        if content_type == 'video':
            cmd = [
                'curl', '-s', '-X', 'POST',
                f'https://graph.facebook.com/v26.0/{page_id}/videos',
                '-F', f'access_token={page_token}',
                '-F', f'source=@{dl_path}',
                '-F', f'title=<{_title_file}',
                '-F', f'description=<{_cap_file}',
            ]
            upload_resp = subprocess.run(cmd, capture_output=True, timeout=600)
            upload_json = json.loads(upload_resp.stdout.decode() or '{}')
            if 'error' in upload_json:
                raise Exception(upload_json['error'].get('message', str(upload_json['error'])))
            meta_post_id = str(upload_json.get('id', '') or '').strip()
            if not meta_post_id:
                raise Exception('Meta balas tanpa ID video — video tidak terbit (response: %s)' % str(upload_json)[:200])
            permalink = f'https://www.facebook.com/{page_id}/videos/{meta_post_id}'
            status = 'published'
        else:
            cmd = [
                'curl', '-s', '-X', 'POST',
                f'https://graph.facebook.com/v26.0/{page_id}/photos',
                '-F', f'access_token={page_token}',
                '-F', f'source=@{dl_path}',
                '-F', f'caption=<{_cap_file}',
            ]
            if link:
                cmd += ['-F', f'link=<{_link_file}']
            upload_resp = subprocess.run(cmd, capture_output=True, timeout=300)
            upload_json = json.loads(upload_resp.stdout.decode() or '{}')
            if 'error' in upload_json:
                raise Exception(upload_json['error'].get('message', str(upload_json['error'])))
            meta_post_id = str(upload_json.get('id', '') or '').strip()
            if not meta_post_id:
                raise Exception('Meta balas tanpa ID foto — foto tidak terbit (response: %s)' % str(upload_json)[:200])
            _post_id = meta_post_id.split('_')[-1] if '_' in meta_post_id else meta_post_id
            permalink = f'https://www.facebook.com/{page_id}/posts/{_post_id}'
            status = 'published'

        # Step 4: Simpan riwayat
        save_page_post(page_id, page_name, content_id, content_name, content_type,
                       title, caption_final, meta_post_id, permalink, status,
                       '', session.get('admin', 'admin'))
        for _tmp in (dl_path, _title_file, _cap_file, _link_file):
            try:
                os.remove(_tmp)
            except:
                pass
        _notify_success(page_name, content_name, title, permalink, content_type)
        return jsonify({'status': 'ok', 'message': 'Post berhasil dipublish', 'meta_post_id': meta_post_id, 'permalink': permalink})

    except Exception as e:
        save_page_post(page_id, page_name, content_id, content_name, content_type,
                       title, caption_final, '', '', 'failed', str(e)[:2000], session.get('admin', 'admin'))
        return jsonify({'status': 'error', 'message': str(e)}), 500


# =============================================================================
# VO GENERATOR — Edge TTS (gratis) + ElevenLabs (premium)
# Menu: Creator -> Generator VO   |   Halaman: /vo-generator
# Konversi dijalankan sebagai background job (file-based store) supaya tahan
# dokumen panjang: gunicorn timeout 300s, Cloudflare tunnel ~100s per request.
# =============================================================================
import asyncio as _asyncio
import re as _re
import shutil as _shutil
import subprocess as _subprocess
import threading as _threading
import time as _time
import uuid as _uuid

import edge_tts as _edge_tts

try:
    import babel as _babel
except ImportError:  # pragma: no cover
    _babel = None

try:
    import pymupdf as _pymupdf
except ImportError:  # pragma: no cover
    _pymupdf = None
try:
    import docx as _docx
except ImportError:  # pragma: no cover
    _docx = None

VO_DIR = os.path.dirname(os.path.abspath(__file__))
VO_CACHE_DIR = os.path.join(VO_DIR, 'static', 'vo_cache')
VO_OUT_DIR = os.path.join(VO_DIR, 'static', 'vo_output')
VO_JOB_DIR = os.path.join(VO_DIR, 'vo_jobs')
VO_TMP_DIR = os.path.join(VO_DIR, 'vo_tmp')
VO_VOICES_CACHE = os.path.join(VO_DIR, 'vo_voices_cache.json')
for _d in (VO_CACHE_DIR, VO_OUT_DIR, VO_JOB_DIR, VO_TMP_DIR):
    os.makedirs(_d, exist_ok=True)

VO_EL_KEY = (_cfg.get('elevenlabs_api_key') or '').strip()
VO_EL_MODEL = 'eleven_multilingual_v2'
VO_EL_CHUNK = 2400          # batas aman di bawah limit 5000/request
VO_EDGE_CHUNK = 1200
VO_MAX_CHARS = 60000        # batas atas per konversi
VO_MAX_UPLOAD_MB = 8
VO_EL_PRICE_PER_CHAR = 1    # free tier: 10.000 karakter/bulan

# --- OpenAI TTS ---
VO_OAI_KEY = (_cfg.get('openai_api_key') or '').strip()
VO_OAI_MODEL = 'tts-1'          # default model (ada juga tts-1-hd)
VO_OAI_CHUNK = 2000              # OpenAI TTS max ~4096 char per request
VO_OAI_VOICES = [
    {'voice': 'alloy',   'gender': 'Neutral',  'desc': 'Netral, cocok untuk narasi umum'},
    {'voice': 'echo',    'gender': 'Male',     'desc': 'Pria, tenang dan profesional'},
    {'voice': 'fable',   'gender': 'Neutral',  'desc': 'Netral, storytelling hangat'},
    {'voice': 'onyx',    'gender': 'Male',     'desc': 'Pria, dalam dan authoritative'},
    {'voice': 'nova',    'gender': 'Female',   'desc': 'Wanita, cerah dan energik'},
    {'voice': 'shimmer', 'gender': 'Female',   'desc': 'Wanita, lembut dan menenangkan'},
]

# --- Gemini TTS via 9router (voice Indonesia paling natural, gratis lewat gateway) ---
VO_9R_BASE = (_cfg.get('ninerouter_base_url') or '').strip().rstrip('/')
VO_9R_KEY = (_cfg.get('ninerouter_api_key') or '').strip()
VO_GEMINI_MODEL = 'gemini/gemini-2.5-flash-preview-tts'
VO_GEMINI_CHUNK = 1500      # jaga-jaga: makin pendek makin kecil risiko 'high demand'
VO_GEMINI_RETRY = 4         # percobaan ulang saat 502 'high demand'
VO_GEMINI_WAIT = 30         # detik jeda antar percobaan (sesuai reset window Gemini)
VO_GEMINI_VOICES = [
    {'voice': 'Kore', 'gender': 'Female', 'desc': 'Wanita, tegas dan jelas'},
    {'voice': 'Aoede', 'gender': 'Female', 'desc': 'Wanita, santai dan mengalir'},
    {'voice': 'Leda', 'gender': 'Female', 'desc': 'Wanita, muda dan ceria'},
    {'voice': 'Charon', 'gender': 'Male', 'desc': 'Pria, informatif dan mantap'},
    {'voice': 'Puck', 'gender': 'Male', 'desc': 'Pria, ceria dan bersemangat'},
    {'voice': 'Fenrir', 'gender': 'Male', 'desc': 'Pria, ekspresif dan bertenaga'},
]

# --- Pemoles naskah VO (buang simbol storyboard, rapikan tanda baca) ---
try:
    sys.path.insert(0, os.path.expanduser('~/.hermes/scripts'))
    from vo_script_clean import bersihkan_teks_vo as _vo_bersihkan, \
        statistik as _vo_clean_stat
    VO_CLEAN_READY = True
except Exception as _e:
    VO_CLEAN_READY = False
    def _vo_bersihkan(t, **kw):
        return t
    def _vo_clean_stat(a, b):
        return {}
    print('[vo] cleaner tidak tersedia:', _e)

VO_LANG_NAMES = {
    'id-ID': 'Indonesia', 'en-US': 'Inggris (Amerika)', 'en-GB': 'Inggris (British)',
    'en-AU': 'Inggris (Australia)', 'en-IN': 'Inggris (India)', 'ms-MY': 'Melayu (Malaysia)',
    'zh-CN': 'Mandarin (China)', 'zh-TW': 'Mandarin (Taiwan)', 'ja-JP': 'Jepang', 'ko-KR': 'Korea',
    'ar-SA': 'Arab (Saudi)', 'ar-EG': 'Arab (Mesir)', 'hi-IN': 'Hindi', 'th-TH': 'Thailand',
    'vi-VN': 'Vietnam', 'fil-PH': 'Filipino', 'es-ES': 'Spanyol (Spanyol)', 'es-MX': 'Spanyol (Meksiko)',
    'pt-BR': 'Portugis (Brasil)', 'fr-FR': 'Prancis', 'de-DE': 'Jerman', 'it-IT': 'Italia',
    'nl-NL': 'Belanda', 'ru-RU': 'Rusia', 'tr-TR': 'Turki', 'pl-PL': 'Polandia', 'sv-SE': 'Swedia',
    'da-DK': 'Denmark', 'nb-NO': 'Norwegia', 'fi-FI': 'Finlandia', 'cs-CZ': 'Ceko', 'uk-UA': 'Ukraina',
    'ro-RO': 'Rumania', 'hu-HU': 'Hungaria', 'el-GR': 'Yunani', 'he-IL': 'Ibrani', 'fa-IR': 'Persia',
    'ta-IN': 'Tamil', 'te-IN': 'Telugu', 'bn-IN': 'Bengali', 'ur-PK': 'Urdu', 'sw-KE': 'Swahili',
}

VO_PREVIEW_TEXT = {
    'id': 'Halo, ini contoh suara saya. Semoga harimu menyenangkan.',
    'ms': 'Helo, ini contoh suara saya.',
    'en': 'Hello, this is a sample of my voice. Have a great day.',
    'default': 'Hello, this is a sample of my voice.',
}


VO_POPULAR_LANGS = ['id-ID', 'en-US', 'en-GB', 'ms-MY', 'zh-CN', 'ja-JP', 'ko-KR', 'ar-SA',
                    'hi-IN', 'th-TH', 'vi-VN', 'fil-PH', 'es-ES', 'pt-BR', 'fr-FR', 'de-DE']
_VO_LABEL_CACHE = {}


def _vo_lang_label(code):
    """Nama bahasa dalam Bahasa Indonesia. Pakai babel biar 142 locale kebaca semua."""
    if code in _VO_LABEL_CACHE:
        return _VO_LABEL_CACHE[code]
    name = VO_LANG_NAMES.get(code)
    if not name and _babel is not None and code and code != 'multi':
        try:
            loc = _babel.Locale.parse(code, sep='-')
            lang = (loc.get_language_name('id') or loc.get_display_name('id') or '').strip()
            terr = (loc.get_territory_name('id') or '').strip()
            if lang and terr and lang.lower() != terr.lower():
                name = f'{lang} ({terr})'
            else:
                name = lang or terr or code
            name = name[:1].upper() + name[1:]
        except Exception:
            name = code
    name = name or code
    _VO_LABEL_CACHE[code] = name
    return name


def _vo_is_multilingual(v):
    return 'Multilingual' in (v.get('voice_id') or '')


def _vo_safe(name):
    return _re.sub(r'[^A-Za-z0-9_.-]', '_', str(name))[:60] or 'file'


def _vo_chunks(text, size):
    """Potong teks di batas kalimat/baris, maksimal `size` karakter per potongan."""
    text = _re.sub(r'[ \t]+', ' ', (text or '').replace('\r\n', '\n').replace('\r', '\n')).strip()
    if not text:
        return []
    parts = []
    for para in text.split('\n'):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            parts.append(para)
            continue
        buf = ''
        for sentence in _re.split(r'(?<=[.!?;:])\s+', para):
            if len(sentence) > size:  # kalimat kepanjangan: potong paksa
                for i in range(0, len(sentence), size):
                    chunk = sentence[i:i + size]
                    if buf:
                        parts.append(buf)
                        buf = ''
                    parts.append(chunk)
                continue
            if len(buf) + len(sentence) + 1 <= size:
                buf = (buf + ' ' + sentence).strip()
            else:
                parts.append(buf)
                buf = sentence
        if buf:
            parts.append(buf)
    return parts


def _vo_concat(parts, out_path):
    """Gabung banyak MP3 jadi satu file. Coba stream-copy dulu, fallback re-encode."""
    if not parts:
        raise RuntimeError('Tidak ada audio yang dihasilkan')
    if len(parts) == 1:
        _shutil.move(parts[0], out_path)
        return out_path
    list_file = out_path + '.txt'
    with open(list_file, 'w', encoding='utf-8') as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    for codec_args in (['-c', 'copy'], ['-c:a', 'libmp3lame', '-b:a', '128k']):
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
               '-i', list_file] + codec_args + [out_path]
        try:
            r = _subprocess.run(cmd, capture_output=True, timeout=600)
            if r.returncode == 0 and os.path.getsize(out_path) > 1000:
                break
        except Exception:
            continue
    try:
        os.remove(list_file)
    except OSError:
        pass
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError('Gagal menggabungkan potongan audio (ffmpeg)')
    return out_path


def _vo_audio_duration(path):
    try:
        r = _subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=nw=1:nk=1', path], capture_output=True, timeout=30)
        return round(float(r.stdout.decode().strip()), 1)
    except Exception:
        return None


def _vo_job_path(job_id):
    return os.path.join(VO_JOB_DIR, _vo_safe(job_id) + '.json')


def _vo_job_write(job_id, **data):
    path = _vo_job_path(job_id)
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.update(data)
    state['updated_at'] = datetime.now().isoformat(timespec='seconds')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)
    return state


def _vo_job_read(job_id):
    try:
        with open(_vo_job_path(job_id), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------- daftar voice ------------------------------------

def _vo_edge_voices():
    voices = _asyncio.run(_edge_tts.list_voices())
    out = []
    for v in voices:
        locale = v.get('Locale', '')
        short = v.get('ShortName', '')
        multi = 'Multilingual' in short
        out.append({
            'engine': 'edge',
            'voice_id': short,
            'name': (v.get('FriendlyName') or short or '').replace('Microsoft ', '').split(' - ')[0].strip(),
            'gender': v.get('Gender', ''),
            'locale': locale,
            'language': 'Multilingual — bisa Bahasa Indonesia' if multi else _vo_lang_label(locale),
            'multilingual': multi,
            'accent': (v.get('VoiceTag') or {}).get('VoicePersonalities', [''])[0] if v.get('VoiceTag') else '',
            'preview_url': '',
        })
    return sorted(out, key=lambda x: (x['locale'] != 'id-ID', x['locale'], x['name']))


def _vo_el_voices():
    if not VO_EL_KEY:
        return []
    req = urllib.request.Request('https://api.elevenlabs.io/v1/voices',
                                 headers={'xi-api-key': VO_EL_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    out = []
    plan = _vo_el_plan()
    for v in data.get('voices', []):
        labels = v.get('labels') or {}
        lang = labels.get('language', 'en')
        is_indonesia = lang == 'id'
        is_premade = (v.get('category') or '') == 'premade'
        # Voice Indonesia (Mila, Zaak, Yudo, Ami) = professional, butuh plan berbayar
        # tapi tetap ditampilkan supaya user tahu kalau di-upgrade bisa langsung pakai
        paid_only = not is_premade
        out.append({
            'engine': 'elevenlabs',
            'voice_id': v.get('voice_id', ''),
            'name': v.get('name', ''),
            'gender': (labels.get('gender') or '').capitalize(),
            'locale': 'multi',
            'language': 'Bahasa Indonesia' if is_indonesia else 'Multibahasa (29 bahasa)',
            'accent': labels.get('accent', '') or labels.get('description', ''),
            'category': v.get('category', ''),
            'paid_only': paid_only,
            'is_indonesia': is_indonesia,
            'preview_url': v.get('preview_url', ''),
        })
    # Sort: Indonesia first, then premade, then by name
    out.sort(key=lambda x: (not x.get('is_indonesia'), x.get('category') != 'premade', x['name']))
    return out


def _vo_all_voices(refresh=False):
    """Gabungan voice Edge + ElevenLabs, di-cache 12 jam (ElevenLabs selalu live)."""
    cache = {}
    if os.path.exists(VO_VOICES_CACHE) and not refresh:
        try:
            with open(VO_VOICES_CACHE, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    ts = cache.get('ts', 0)
    if refresh or not cache.get('edge') or (time.time() - ts) > 43200:
        try:
            cache['edge'] = _vo_edge_voices()
            cache['ts'] = time.time()
            with open(VO_VOICES_CACHE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as e:
            print(f'[vo] gagal ambil voice Edge: {e}')
    if not cache.get('edge'):
        cache['edge'] = []
    try:
        cache['elevenlabs'] = _vo_el_voices()
    except Exception as e:
        print(f'[vo] gagal ambil voice ElevenLabs: {e}')
        cache['elevenlabs'] = []
    return cache


def _vo_languages(voices):
    """Ringkasan bahasa dari voice Edge (dipakai buat dropdown).

    Voice 'Multilingual' nggak dihitung ke locale asalnya (en-US dsb) — dipisah,
    soalnya mereka bisa dipakai lintas bahasa (termasuk Bahasa Indonesia).
    """
    agg = {}
    for v in voices:
        loc = v.get('locale') or ''
        if not loc or loc == 'multi' or v.get('multilingual'):
            continue
        item = agg.setdefault(loc, {'code': loc, 'name': _vo_lang_label(loc), 'male': 0, 'female': 0})
        g = (v.get('gender') or '').lower()
        if g.startswith('m'):
            item['male'] += 1
        elif g.startswith('f'):
            item['female'] += 1
    rows = list(agg.values())
    pop = {c: i for i, c in enumerate(VO_POPULAR_LANGS)}
    rows.sort(key=lambda x: (0 if x['code'] == 'id-ID' else 1,
                             pop.get(x['code'], 99), x['name']))
    for r in rows:
        r['total'] = r['male'] + r['female']
        r['popular'] = r['code'] in pop
    return rows


# --------------------------- quota ElevenLabs ---------------------------------

def _vo_el_quota():
    if not VO_EL_KEY:
        return {'ok': False, 'message': 'API key ElevenLabs belum di-set di config.json'}
    try:
        req = urllib.request.Request('https://api.elevenlabs.io/v1/user',
                                     headers={'xi-api-key': VO_EL_KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        sub = d.get('subscription') or {}
        limit = int(sub.get('character_limit') or 0)
        used = int(sub.get('character_count') or 0)
        return {
            'ok': True,
            'tier': sub.get('tier', ''),
            'used': used,
            'limit': limit,
            'remaining': max(limit - used, 0),
            'reset': sub.get('next_character_count_reset_unix'),
        }
    except Exception as e:
        return {'ok': False, 'message': f'Gagal cek kuota ElevenLabs: {str(e)[:200]}'}


def _vo_el_plan():
    """Plan ElevenLabs aktif. Dipakai buat nandain voice yang butuh plan bayar."""
    q = _vo_el_quota()
    tier = (q.get('tier') or '').lower() if q.get('ok') else ''
    return {'ok': bool(q.get('ok')), 'tier': tier,
            'paid': bool(tier) and tier not in ('free', 'trial')}


def _vo_el_voice_blocked(voice_id):
    """Cek voice ElevenLabs ini ke-blokir plan nggak. Balikin alasan kalau ke-blokir."""
    if not VO_EL_KEY:
        return ''
    plan = _vo_el_plan()
    if plan['paid']:
        return ''
    for v in (_vo_all_voices().get('elevenlabs') or []):
        if v['voice_id'] == voice_id and v.get('paid_only'):
            return (f"Voice \"{v.get('name') or voice_id}\" kelas library — di plan "
                    f"ElevenLabs {plan['tier'] or 'free'} cuma voice <b>premade</b> yang bisa "
                    f"dipakai lewat API (bakal ditolak 402). Pilih voice lain, atau pakai Edge.")
    return ''


# --------------------------- sintesis -----------------------------------------

def _vo_edge_synth(text, voice_id, out_path, rate=0, progress=None):
    chunks = _vo_chunks(text, VO_EDGE_CHUNK)
    if not chunks:
        raise RuntimeError('Teks kosong')
    parts = []
    rate_str = f'{int(rate):+d}%' if rate else '+0%'
    for i, chunk in enumerate(chunks, 1):
        part = os.path.join(VO_TMP_DIR, f'{_uuid.uuid4().hex}.mp3')

        async def _run(c=chunk, p=part):
            await _edge_tts.Communicate(c, voice_id, rate=rate_str).save(p)
        _asyncio.run(_run())
        if not os.path.exists(part) or os.path.getsize(part) < 200:
            raise RuntimeError(f'Edge TTS gagal pada bagian {i}')
        parts.append(part)
        if progress:
            progress(int(i / len(chunks) * 100), f'Bagian {i}/{len(chunks)}')
    return _vo_concat(parts, out_path)


def _vo_el_synth(text, voice_id, out_path, progress=None):
    if not VO_EL_KEY:
        raise RuntimeError('API key ElevenLabs belum di-set di config.json')
    chunks = _vo_chunks(text, VO_EL_CHUNK)
    if not chunks:
        raise RuntimeError('Teks kosong')
    total_chars = len(text)
    quota = _vo_el_quota()
    if quota.get('ok') and quota.get('limit') and total_chars > quota.get('remaining', 0):
        raise RuntimeError(
            f'Kuota ElevenLabs nggak cukup: butuh {total_chars:,} karakter, sisa {quota.get("remaining", 0):,} '
            f'(limit {quota.get("limit", 0):,}/bulan). Pakai Edge dulu atau upgrade plan.')
    parts = []
    for i, chunk in enumerate(chunks, 1):
        url = (f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
               f'?output_format=mp3_44100_128')
        body = json.dumps({'text': chunk, 'model_id': VO_EL_MODEL}).encode()
        req = urllib.request.Request(url, data=body, headers={
            'xi-api-key': VO_EL_KEY, 'Content-Type': 'application/json'})
        part = os.path.join(VO_TMP_DIR, f'{_uuid.uuid4().hex}.mp3')
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, 'wb') as f:
                _shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:300]
            raise RuntimeError(f'ElevenLabs error {e.code}: {detail}')
        if os.path.getsize(part) < 500:
            raise RuntimeError(f'ElevenLabs mengembalikan audio kosong pada bagian {i}')
        parts.append(part)
        if progress:
            progress(int(i / len(chunks) * 100), f'Bagian {i}/{len(chunks)}')
    return _vo_concat(parts, out_path)


def _vo_oai_synth(text, voice_id, out_path, model='tts-1', progress=None):
    """Sintesis TTS via OpenAI API (tts-1 / tts-1-hd). Voice: alloy, echo, fable, onyx, nova, shimmer."""
    if not VO_OAI_KEY:
        raise RuntimeError('API key OpenAI belum di-set di config.json')
    chunks = _vo_chunks(text, VO_OAI_CHUNK)
    if not chunks:
        raise RuntimeError('Teks kosong')
    parts = []
    for i, chunk in enumerate(chunks, 1):
        url = 'https://api.openai.com/v1/audio/speech'
        body = json.dumps({
            'model': model,
            'input': chunk,
            'voice': voice_id,
            'response_format': 'mp3',
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            'Authorization': f'Bearer {VO_OAI_KEY}',
            'Content-Type': 'application/json',
        })
        part = os.path.join(VO_TMP_DIR, f'{_uuid.uuid4().hex}.mp3')
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, 'wb') as f:
                _shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:300]
            raise RuntimeError(f'OpenAI TTS error {e.code}: {detail}')
        if os.path.getsize(part) < 500:
            raise RuntimeError(f'OpenAI TTS mengembalikan audio kosong pada bagian {i}')
        parts.append(part)
        if progress:
            progress(int(i / len(chunks) * 100), f'Bagian {i}/{len(chunks)}')
    return _vo_concat(parts, out_path)


def _vo_gemini_synth(text, voice_id, out_path, progress=None):
    """Sintesis TTS via Gemini TTS lewat 9router. Balikannya WAV -> dikonversi ke MP3.

    Gemini TTS sering jawab 502 'high demand... reset after 30s' — jadi ada retry.
    """
    if not VO_9R_BASE or not VO_9R_KEY:
        raise RuntimeError('9router belum dikonfigurasi (ninerouter_base_url / ninerouter_api_key)')
    chunks = _vo_chunks(text, VO_GEMINI_CHUNK)
    if not chunks:
        raise RuntimeError('Teks kosong')
    parts = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        body = json.dumps({
            'model': VO_GEMINI_MODEL,
            'input': chunk,
            'voice': voice_id,
            'response_format': 'mp3',
        }).encode()
        raw_wav = os.path.join(VO_TMP_DIR, f'{_uuid.uuid4().hex}.wav')
        part = os.path.join(VO_TMP_DIR, f'{_uuid.uuid4().hex}.mp3')
        last_err = None
        for attempt in range(1, VO_GEMINI_RETRY + 1):
            req = urllib.request.Request(VO_9R_BASE + '/v1/audio/speech', data=body, headers={
                'Authorization': f'Bearer {VO_9R_KEY}',
                'Content-Type': 'application/json',
            })
            try:
                with urllib.request.urlopen(req, timeout=180) as r, open(raw_wav, 'wb') as f:
                    _shutil.copyfileobj(r, f)
                if os.path.getsize(raw_wav) < 2000:
                    last_err = 'audio kosong'
                    raise RuntimeError('audio kosong')
                last_err = None
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', 'replace')[:300]
                last_err = f'HTTP {e.code}: {detail}'
                # Kuota habis: nunggu nggak nolong, langsung stop biar user nggak nunggu lama
                if 'quota' in detail.lower() or 'billing' in detail.lower():
                    raise RuntimeError(
                        'Kuota harian Gemini TTS habis (reset besok). '
                        'Sementara pakai engine Edge TTS dulu — gratis dan voice Indonesia asli.'
                    )
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
            if attempt < VO_GEMINI_RETRY:
                if progress:
                    progress(int((i - 1) / total * 100),
                             f'Bagian {i}/{total} — server sibuk, coba lagi ({attempt}/{VO_GEMINI_RETRY})')
                _time.sleep(VO_GEMINI_WAIT)
        if last_err:
            raise RuntimeError(f'Gemini TTS gagal di bagian {i}/{total}: {last_err}')
        # WAV -> MP3
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', raw_wav,
               '-codec:a', 'libmp3lame', '-b:a', '128k', part]
        r = _subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) < 1000:
            raise RuntimeError('Gagal konversi WAV ke MP3 (ffmpeg)')
        try:
            os.remove(raw_wav)
        except OSError:
            pass
        parts.append(part)
        if progress:
            progress(int(i / total * 100), f'Bagian {i}/{total}')
    return _vo_concat(parts, out_path)


def _vo_run_job(job_id, engine, voice_id, text, rate=0, out_name=None):
    label = {'edge': 'Edge TTS', 'elevenlabs': 'ElevenLabs', 'openai': 'OpenAI TTS',
             'gemini': 'Gemini TTS'}.get(engine, engine)
    try:
        _vo_job_write(job_id, status='running', progress=2, message=f'Menyiapkan {label}...')
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = _vo_safe(out_name) if out_name else 'vo'
        fname = f'{base}_{stamp}.mp3'
        out_path = os.path.join(VO_OUT_DIR, fname)

        def progress(pct, msg):
            _vo_job_write(job_id, status='running', progress=min(max(pct, 3), 98), message=msg)

        if engine == 'elevenlabs':
            _vo_el_synth(text, voice_id, out_path, progress=progress)
        elif engine == 'gemini':
            _vo_gemini_synth(text, voice_id, out_path, progress=progress)
        elif engine == 'openai':
            model = 'tts-1-hd' if voice_id.endswith('__hd') else 'tts-1'
            vid = voice_id.replace('__hd', '')
            _vo_oai_synth(text, vid, out_path, model=model, progress=progress)
        else:
            _vo_edge_synth(text, voice_id, out_path, rate=rate, progress=progress)

        size = os.path.getsize(out_path)
        _vo_job_write(job_id, status='done', progress=100,
                      message='Selesai', url=f'/static/vo_output/{fname}',
                      filename=fname, size=size, chars=len(text),
                      duration=_vo_audio_duration(out_path), engine=engine, voice_id=voice_id)
    except Exception as e:
        _vo_job_write(job_id, status='error', progress=0, message=str(e)[:500])
    finally:
        for f in os.listdir(VO_TMP_DIR):
            p = os.path.join(VO_TMP_DIR, f)
            try:
                if time.time() - os.path.getmtime(p) > 3600:
                    os.remove(p)
            except OSError:
                pass


# --------------------------- ekstraksi dokumen --------------------------------

def _vo_docx_blocks(doc):
    """Iterator isi .docx URUT dokumen: ('p', Paragraph) dan ('tbl', Table).

    ⚠️ `doc.paragraphs` bawaan python-docx NGGAK baca isi tabel, makanya jalan
    langsung ke body XML.
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for el in doc.element.body.iterchildren():
        tag = el.tag.split('}')[-1]
        if tag == 'p':
            yield 'p', Paragraph(el, doc)
        elif tag == 'tbl':
            yield 'tbl', Table(el, doc)


def _vo_docx_text(doc):
    """Teks .docx apa adanya (paragraf + SEMUA kolom tabel), urut sesuai dokumen."""
    lines = []
    for kind, obj in _vo_docx_blocks(doc):
        if kind == 'p':
            txt = obj.text.strip()
            if txt:
                lines.append(txt)
            continue
        for row in obj.rows:
            cells, seen = [], set()
            for c in row.cells:                    # sel gabungan muncul berulang → dedupe
                if id(c._tc) in seen:
                    continue
                seen.add(id(c._tc))
                cells.append(' '.join(p.text.strip() for p in c.paragraphs if p.text.strip()))
            row_txt = ' | '.join(cells).strip(' |')
            if row_txt:
                lines.append(row_txt)
        lines.append('')
    return '\n'.join(lines).strip()


def _vo_docx_columns(doc):
    """Daftar kolom tabel .docx (dari tabel pertama) + jumlah baris yang ada isinya.

    Dipakai buat dropdown "ambil kolom mana": narasi VO storyboard biasanya cuma di
    satu kolom (`VO / Narasi`), kolom durasi/visual/text-on-screen cuma jadi noise.
    """
    for kind, tbl in _vo_docx_blocks(doc):
        if kind != 'tbl' or not tbl.rows:
            continue
        cols, seen = [], set()
        for i, cell in enumerate(tbl.rows[0].cells):
            if id(cell._tc) in seen:               # sel gabungan → jangan dobel
                continue
            seen.add(id(cell._tc))
            name = ' '.join(t.strip() for t in cell.text.split('\n') if t.strip()) or f'Kolom {i + 1}'
            n = 0
            for row in tbl.rows[1:]:
                if i >= len(row.cells):
                    continue
                try:
                    if row.cells[i].text.strip():
                        n += 1
                except Exception:
                    pass
            cols.append({'index': i, 'name': name, 'rows': n})
        return cols
    return []


def _vo_docx_suggest_column(cols):
    """Tebak kolom narasi dari nama kolom: 'VO', 'Narasi', atau 'Voice Over'.

    Sengaja tanpa regex — app.py nggak import `re`.
    """
    for c in cols:
        words = ''.join(ch if ch.isalnum() else ' ' for ch in (c['name'] or '').lower()).split()
        if 'narasi' in words or 'vo' in words or ('voice' in words and 'over' in words):
            return c['index']
    return None


def _vo_docx_text_col(doc, col_index):
    """Teks .docx CUMA 1 kolom tabel (baris header dibuang) + paragraf body.

    Dipakai biar yang kebaca cuma narasi VO-nya, bukan deskripsi visual + text on screen.
    """
    lines = []
    for kind, obj in _vo_docx_blocks(doc):
        if kind == 'p':
            txt = obj.text.strip()
            if txt:
                lines.append(txt)
            continue
        for ri, row in enumerate(obj.rows):
            if ri == 0:                            # baris header tabel → skip
                continue
            if col_index >= len(row.cells):
                continue
            val = ' '.join(p.text.strip() for p in row.cells[col_index].paragraphs if p.text.strip())
            if val:
                lines.append(val)
    return '\n'.join(lines).strip()


def _vo_extract_upload(file_storage, column=None):
    """Ekstrak teks upload. Balikin (teks, ext, meta).

    `column` cuma berlaku buat .docx:
      - `None`          → auto: kalau ada kolom "VO"/"Narasi", pakai kolom itu
      - `'all'`         → semua kolom tabel (format ' | ')
      - angka (str/int) → cuma kolom tabel index itu
    """
    fname = (file_storage.filename or '').strip()
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    raw = file_storage.read()
    if not raw:
        raise RuntimeError('File kosong')
    if ext == 'txt':
        for enc in ('utf-8', 'utf-16', 'latin-1'):
            try:
                return raw.decode(enc), ext, {}
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', 'ignore'), ext, {}
    if ext == 'docx':
        if _docx is None:
            raise RuntimeError('python-docx belum terpasang di server')
        import io
        doc = _docx.Document(io.BytesIO(raw))
        cols = _vo_docx_columns(doc)
        suggested = _vo_docx_suggest_column(cols)
        used = None
        want = (column or '').strip() if isinstance(column, str) else column
        if want in ('all', 'semua', '0all'):
            used = None
        elif want not in (None, '', 'auto'):
            try:
                used = int(want)
            except (TypeError, ValueError):
                used = None
            if used is not None and not any(c['index'] == used for c in cols):
                used = None
            # kolom cuma 1 baris isinya (kemungkinan salah kolom) → balik ke semua kolom
            if used is not None and next((c['rows'] for c in cols if c['index'] == used), 0) == 0:
                used = None
        elif suggested is not None:
            used = suggested
        text = _vo_docx_text_col(doc, used) if used is not None else _vo_docx_text(doc)
        if used is not None and not text.strip():
            text = _vo_docx_text(doc)
            used = None
        meta = {'columns': cols, 'column': used, 'suggested_column': suggested}
        return text, ext, meta
    if ext == 'pdf':
        if _pymupdf is None:
            raise RuntimeError('pymupdf belum terpasang di server')
        doc = _pymupdf.open(stream=raw, filetype='pdf')
        pages = [page.get_text() for page in doc]
        doc.close()
        return '\n'.join(pages), ext, {}
    raise RuntimeError('Format file harus .txt, .docx, atau .pdf')


# --------------------------- routes -------------------------------------------

@app.route('/vo-generator')
@admin_required
def vo_generator_page():
    return render_template('vo_generator.html', active_page='vo_generator')


# ==========================================================================
# Bank Kreatif (Storyboard + VO) — sync dari Google Drive (service account, read-only)
# ==========================================================================
@app.route('/bank-kreatif')
@admin_required
def bank_kreatif_page():
    return render_template('bank_kreatif.html', active_page='bank_kreatif')


@app.route('/api/bank/index')
@admin_required
def api_bank_index():
    try:
        import bank_kreatif as _bk
        if os.path.exists(_bk.INDEX_PATH):
            with open(_bk.INDEX_PATH, encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({'folder_id': '', 'synced_at': '', 'paket': [],
                        'jumlah': {'paket': 0, 'vo': 0, 'storyboard': 0}})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:300]}), 500


@app.route('/api/bank/upload', methods=['POST'])
@admin_required
def api_bank_upload():
    """Upload paket kreatif LANGSUNG dari web — 2 kolom: storyboard & VO.

    Otomatis dibungkus jadi 1 file ZIP paket. Nggak perlu lewat Google Drive.
    """
    try:
        import bank_kreatif as _bk
        nama = (request.form.get('nama') or '').strip()
        kolom_sb = request.files.getlist('storyboard')
        kolom_vo = request.files.getlist('vo')
        # kompatibilitas: kalau front-end lama kirim 'files'
        if not kolom_sb and not kolom_vo:
            leg = request.files.getlist('files')
            kolom_sb, kolom_vo = leg, []
        ada = any((f.filename or '').strip() for f in (kolom_sb + kolom_vo))
        if not ada:
            return jsonify({'status': 'error',
                            'message': 'Belum ada file storyboard atau VO yang dipilih'}), 400
        folder_id = (_cfg.get('google_drive_folder_id') or '').strip()
        if not folder_id:
            return jsonify({'status': 'error', 'message': 'Folder Google Drive belum diatur di config.json'}), 400
        r = _bk.oauth_upload_paket(nama, kolom_sb, kolom_vo, folder_id)
        if r.get('status') == 'ok':
            r['nama'] = nama or 'Paket Tanpa Nama'
            r['source'] = 'oauth_drive'
            r['folder_url'] = (r.get('folder') or {}).get('webViewLink', '')
            r['file_count'] = len(r.get('files') or [])
            # Refresh index agar folder OAuth langsung muncul sebagai card.
            try:
                _bk.sync(folder_id)
            except Exception as sync_err:
                r['sync_warning'] = str(sync_err)[:300]
        if r.get('status') == 'ok' and r.get('source') != 'oauth_drive':
            r.setdefault('nama', nama)
            try:
                idx = _bk._muat_index()
                for _p in idx.get('paket', []):
                    if _p.get('slug') == r.get('slug'):
                        r['nama'] = _p.get('nama', nama)
                        for _f in _p.get('files', []):
                            if str(_f.get('nama', '')).lower().endswith('.zip'):
                                r['zip'] = _f['nama']
                                break
                        break
            except Exception:
                pass
        return jsonify(r), (200 if r.get('status') == 'ok' else 400)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:400]}), 500


@app.route('/api/bank/hapus', methods=['POST'])
@admin_required
def api_bank_hapus():
    """Hapus paket hasil upload web (paket Drive ditolak — hapus di Drive)."""
    try:
        import bank_kreatif as _bk
        slug = ((request.get_json(silent=True) or {}).get('slug') or '').strip()
        if not slug:
            return jsonify({'status': 'error', 'message': 'Slug paket kosong'}), 400
        r = _bk.hapus_paket(slug)
        return jsonify(r), (200 if r.get('status') == 'ok' else 400)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:400]}), 500


@app.route('/api/bank/rename', methods=['POST'])
@admin_required
def api_bank_rename():
    """Ganti nama paket upload web (nama ZIP ikut berubah)."""
    try:
        import bank_kreatif as _bk
        body = request.get_json(silent=True) or {}
        slug = (body.get('slug') or '').strip()
        nama = (body.get('nama') or '').strip()
        if not slug or not nama:
            return jsonify({'status': 'error', 'message': 'Slug & nama wajib diisi'}), 400
        r = _bk.rename_paket(slug, nama)
        if r.get('status') == 'ok' and r.get('drive_id'):
            try:
                drive = _bk.oauth_drive_client()
                drive.files().update(fileId=r['drive_id'], body={'name': nama},
                                     fields='id,name,webViewLink', supportsAllDrives=True).execute()
                r['drive_renamed'] = True
            except Exception as drive_err:
                r['drive_renamed'] = False
                r['drive_warning'] = str(drive_err)[:300]
        return jsonify(r), (200 if r.get('status') == 'ok' else 400)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:400]}), 500


@app.route('/api/bank/setor', methods=['POST'])
@admin_required
def api_bank_setor():
    """Toggle checklist status 'setor ke tim kreatif' per paket."""
    try:
        import bank_kreatif as _bk
        body = request.get_json(silent=True) or {}
        slug = (body.get('slug') or '').strip()
        setor = bool(body.get('setor', False))
        if not slug:
            return jsonify({'status': 'error', 'message': 'Slug wajib diisi'}), 400
        
        idx = _bk._muat_index()
        target = None
        for p in idx.get('paket', []):
            if p.get('slug') == slug:
                p['setor'] = setor
                target = p
                break
        if not target:
            return jsonify({'status': 'error', 'message': 'Paket tidak ditemukan'}), 404
            
        _bk._simpan_index(idx)
        return jsonify({'status': 'ok', 'slug': slug, 'setor': setor})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:400]}), 500


@app.route('/bank-kreatif/zip/<slug>')
@admin_required
def bank_kreatif_zip(slug):
    """Download ZIP paket — nama file SELALU ikut nama paket terbaru."""
    try:
        import bank_kreatif as _bk
        idx = _bk._muat_index()
        target = [p for p in idx.get('paket', []) if p.get('slug') == slug]
        if not target:
            return 'Paket nggak ketemu', 404
        p = target[0]
        kerja = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'bank', slug)
        zip_nama = ''
        for f in p.get('files', []):
            if str(f.get('nama', '')).lower().endswith('.zip'):
                zip_nama = f['nama']
                break
        if not zip_nama or not os.path.exists(os.path.join(kerja, zip_nama)):
            return 'File ZIP nggak ketemu', 404
        unduh = _bk.nama_zip_dari_paket(p.get('nama', ''))
        return send_file(os.path.join(kerja, zip_nama),
                         as_attachment=True, download_name=unduh,
                         mimetype='application/zip')
    except Exception as e:
        return f'Gagal download: {str(e)[:200]}', 500


@app.route('/api/bank/sync', methods=['POST'])
@admin_required
def api_bank_sync():
    try:
        import bank_kreatif as _bk
        fid = (_cfg.get('google_drive_folder_id') or '').strip()
        if not fid:
            return jsonify({'status': 'error',
                            'message': 'Folder Google Drive belum diatur di config.json'}), 400
        r = _bk.sync(fid)
        return jsonify(r)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:400]}), 500


@app.route('/api/bank/info')
@admin_required
def api_bank_info():
    """Diagnosa: service account mana yang dipakai + folder mana yang dibaca."""
    try:
        import bank_kreatif as _bk
        return jsonify({'status': 'ok',
                        'sa_file': _bk.sa_path(),
                        'sa_email': _bk.sa_email(),
                        'folder_id': (_cfg.get('google_drive_folder_id') or '').strip(),
                        'store_dir': _bk.STORE_DIR,
                        'index_ada': os.path.exists(_bk.INDEX_PATH)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)[:300]}), 500


@app.route('/api/vo/meta')
@admin_required
def api_vo_meta():
    cache = _vo_all_voices()
    edge = cache.get('edge') or []
    el = cache.get('elevenlabs') or []
    plan = _vo_el_plan() if VO_EL_KEY else {'ok': False, 'tier': '', 'paid': False}
    for v in el:
        v['blocked'] = bool(v.get('paid_only') and not plan['paid'])
        v['block_reason'] = (f"Voice \"{v.get('name')}\" kelas {v.get('category','library')} — plan ElevenLabs "
                             f"{plan['tier'] or 'free'} cuma bisa voice premade lewat API "
                             f"(bakal ditolak 402). Upgrade ke plan berbayar untuk pakai voice ini."
                             ) if v['blocked'] else ''
    # OpenAI TTS voices
    oai = []
    if VO_OAI_KEY:
        for vinfo in VO_OAI_VOICES:
            oai.append({
                'engine': 'openai',
                'voice_id': vinfo['voice'],
                'name': f"OpenAI {vinfo['voice'].capitalize()}",
                'gender': vinfo['gender'],
                'locale': 'multi',
                'language': 'Multibahasa (termasuk Indonesia)',
                'accent': vinfo['desc'],
                'category': 'openai',
                'paid_only': False,
                'is_indonesia': True,  # bisa bahasa Indonesia
                'preview_url': '',
            })
        # Tambah variant HD
        for vinfo in VO_OAI_VOICES:
            oai.append({
                'engine': 'openai',
                'voice_id': vinfo['voice'] + '__hd',
                'name': f"OpenAI {vinfo['voice'].capitalize()} HD",
                'gender': vinfo['gender'],
                'locale': 'multi',
                'language': 'Multibahasa (HD — kualitas lebih tinggi)',
                'accent': vinfo['desc'] + ' (HD)',
                'category': 'openai',
                'paid_only': False,
                'is_indonesia': True,
                'preview_url': '',
            })
    # --- Gemini TTS lewat 9router ---
    gem = []
    if VO_9R_BASE and VO_9R_KEY:
        for vinfo in VO_GEMINI_VOICES:
            gem.append({
                'engine': 'gemini',
                'voice_id': vinfo['voice'],
                'name': f"Gemini {vinfo['voice']}",
                'gender': vinfo['gender'],
                'locale': 'multi',
                'language': 'Indonesia (natural)',
                'accent': vinfo['desc'],
                'category': 'gemini',
                'paid_only': False,
                'is_indonesia': True,
                'preview_url': '',
            })
    return jsonify({
        'status': 'ok',
        'languages': _vo_languages(edge),
        'voices': edge + el + oai + gem,
        'counts': {'edge': len(edge), 'elevenlabs': len(el),
                   'openai': len(oai), 'gemini': len(gem),
                   'multilingual': len([v for v in edge if v.get('multilingual')]),
                   'el_blocked': len([v for v in el if v.get('blocked')]),
                   'id_voices': len([v for v in el if v.get('is_indonesia')])},
        'multilingual_ids': [v['voice_id'] for v in edge if v.get('multilingual')],
        'default_language': 'id-ID',
        'max_chars': VO_MAX_CHARS,
        'max_upload_mb': VO_MAX_UPLOAD_MB,
        'el_ready': bool(VO_EL_KEY),
        'el_tier': plan['tier'],
        'el_paid': plan['paid'],
        'oai_ready': bool(VO_OAI_KEY),
        'gemini_ready': bool(VO_9R_BASE and VO_9R_KEY),
        'clean_ready': bool(VO_CLEAN_READY),
    })


@app.route('/api/vo/clean', methods=['POST'])
@admin_required
def api_vo_clean():
    """Bersihkan naskah VO dari artefak storyboard/markdown (simbol, titik banyak, label)."""
    data = request.get_json(silent=True) or {}
    teks = (data.get('text') or '').strip()
    if not teks:
        return jsonify({'status': 'error', 'message': 'Naskah kosong'}), 400
    if not VO_CLEAN_READY:
        return jsonify({'status': 'error', 'message': 'Modul cleaner tidak tersedia'}), 500
    try:
        bersih = _vo_bersihkan(teks)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Gagal membersihkan: {e}'}), 500
    return jsonify({'status': 'ok', 'bersih': bersih,
                    'statistik': _vo_clean_stat(teks, bersih)})


@app.route('/api/vo/quota')
@admin_required
def api_vo_quota():
    q = _vo_el_quota()
    q['status'] = 'ok' if q.get('ok') else 'error'
    return jsonify(q)


@app.route('/api/vo/preview')
@admin_required
def api_vo_preview():
    """Sample suara. ElevenLabs != kuota (pakai preview_url resmi mereka)."""
    engine = (request.args.get('engine') or 'edge').lower()
    voice_id = (request.args.get('voice_id') or '').strip()
    lang = (request.args.get('language') or 'id-ID').strip()
    if not voice_id:
        return jsonify({'status': 'error', 'message': 'voice_id kosong'}), 400

    if engine == 'elevenlabs':
        cache = _vo_all_voices()
        for v in cache.get('elevenlabs') or []:
            if v['voice_id'] == voice_id:
                if v.get('preview_url'):
                    return jsonify({'status': 'ok', 'url': v['preview_url'], 'cached': True})
                break
        return jsonify({'status': 'error', 'message': 'Voice ini nggak punya sample resmi'}), 404

    if engine == 'gemini':
        # Generate sample via Gemini TTS (lewat 9router). Cache biar nggak boros request.
        key = f'preview_gemini_{_vo_safe(voice_id)}.mp3'
        path = os.path.join(VO_CACHE_DIR, key)
        if not os.path.exists(path) or os.path.getsize(path) < 500:
            text = VO_PREVIEW_TEXT.get('id', VO_PREVIEW_TEXT['default'])
            try:
                _vo_gemini_synth(text, voice_id, path)
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Gagal bikin sample: {str(e)[:200]}'}), 500
        return jsonify({'status': 'ok', 'url': f'/static/vo_cache/{key}', 'cached': True})

    if engine == 'openai':
        # Generate preview via OpenAI TTS (pakai teks pendek Indonesia)
        vid = voice_id.replace('__hd', '')
        model = 'tts-1-hd' if voice_id.endswith('__hd') else 'tts-1'
        key = f'preview_oai_{_vo_safe(vid)}_{model}.mp3'
        path = os.path.join(VO_CACHE_DIR, key)
        if not os.path.exists(path) or os.path.getsize(path) < 500:
            text = VO_PREVIEW_TEXT.get('id', VO_PREVIEW_TEXT['default'])
            try:
                _vo_oai_synth(text, vid, path, model=model)
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Gagal bikin sample: {str(e)[:200]}'}), 500
        return jsonify({'status': 'ok', 'url': f'/static/vo_cache/{key}', 'cached': True})

    key = f'preview_edge_{_vo_safe(voice_id)}_{_vo_safe(lang)}.mp3'
    path = os.path.join(VO_CACHE_DIR, key)
    if not os.path.exists(path) or os.path.getsize(path) < 500:
        text = VO_PREVIEW_TEXT.get((lang or '')[:2], VO_PREVIEW_TEXT['default'])
        try:

            async def _run():
                await _edge_tts.Communicate(text, voice_id).save(path)
            _asyncio.run(_run())
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'Gagal bikin sample: {str(e)[:200]}'}), 500
    return jsonify({'status': 'ok', 'url': f'/static/vo_cache/{key}', 'cached': True})


@app.route('/api/vo/extract', methods=['POST'])
@admin_required
def api_vo_extract():
    f = request.files.get('file')
    if not f:
        return jsonify({'status': 'error', 'message': 'File belum dipilih'}), 400
    column = (request.form.get('column') or '').strip()
    try:
        text, ext, meta = _vo_extract_upload(f, column=column or None)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    text = (text or '').strip()
    if not text:
        return jsonify({'status': 'error', 'message': 'Isi file kosong / nggak ada teks yang kebaca '
                                                     '(kalau PDF hasil scan, teksnya nggak bisa diambil)'}), 400
    if len(text) > VO_MAX_CHARS:
        return jsonify({'status': 'error',
                        'message': f'Isi file {len(text):,} karakter, batas {VO_MAX_CHARS:,} karakter'}), 400
    return jsonify({'status': 'ok', 'text': text, 'chars': len(text),
                    'words': len(text.split()), 'type': ext,
                    'filename': f.filename,
                    'columns': meta.get('columns', []),
                    'column': meta.get('column'),
                    'suggested_column': meta.get('suggested_column')})


@app.route('/api/vo/convert', methods=['POST'])
@admin_required
def api_vo_convert():
    data = request.get_json(silent=True) or {}
    engine = (data.get('engine') or 'edge').lower()
    voice_id = (data.get('voice_id') or '').strip()
    text = (data.get('text') or '').strip()
    rate = int(data.get('rate') or 0)
    out_name = (data.get('out_name') or '').strip()
    auto_clean = bool(data.get('auto_clean', True))   # default: bersihkan otomatis
    if not voice_id:
        return jsonify({'status': 'error', 'message': 'Voice belum dipilih'}), 400
    if not text:
        return jsonify({'status': 'error', 'message': 'Teks masih kosong'}), 400

    # Bersihkan naskah dari artefak storyboard sebelum masuk TTS
    cleaned = False
    if auto_clean and VO_CLEAN_READY:
        try:
            hasil = _vo_bersihkan(text)
            if hasil.strip():
                if hasil.strip() != text:
                    cleaned = True
                text = hasil.strip()
        except Exception:
            pass

    if len(text) > VO_MAX_CHARS:
        return jsonify({'status': 'error', 'message': f'Teks maksimal {VO_MAX_CHARS:,} karakter'}), 400
    if engine == 'elevenlabs':
        blocked = _vo_el_voice_blocked(voice_id)
        if blocked:
            return jsonify({'status': 'error', 'message': blocked}), 400
        q = _vo_el_quota()
        if q.get('ok') and q.get('limit') and len(text) > q.get('remaining', 0):
            return jsonify({'status': 'error',
                            'message': f'Kuota ElevenLabs nggak cukup: butuh {len(text):,} karakter, '
                                       f'sisa {q.get("remaining", 0):,} dari {q.get("limit", 0):,}/bulan'}), 400

    if engine == 'openai' and not VO_OAI_KEY:
        return jsonify({'status': 'error', 'message': 'API key OpenAI belum di-set di config.json'}), 400

    if engine == 'gemini' and not (VO_9R_BASE and VO_9R_KEY):
        return jsonify({'status': 'error', 'message': '9router belum dikonfigurasi di config.json'}), 400

    job_id = 'vo_' + _uuid.uuid4().hex[:12]
    _vo_job_write(job_id, status='queued', progress=0, message='Masuk antrian...',
                  engine=engine, voice_id=voice_id, chars=len(text))
    _threading.Thread(target=_vo_run_job, args=(job_id, engine, voice_id, text, rate, out_name),
                      daemon=True).start()
    return jsonify({'status': 'ok', 'job_id': job_id, 'cleaned': cleaned, 'chars': len(text)})


@app.route('/api/vo/job/<job_id>')
@admin_required
def api_vo_job(job_id):
    state = _vo_job_read(job_id)
    if not state:
        return jsonify({'status': 'error', 'message': 'Job nggak ketemu'}), 404
    state.setdefault('status', 'unknown')
    return jsonify(state)


@app.route('/api/vo/history')
@admin_required
def api_vo_history():
    rows = []
    try:
        for fn in os.listdir(VO_OUT_DIR):
            if not fn.lower().endswith('.mp3'):
                continue
            p = os.path.join(VO_OUT_DIR, fn)
            rows.append({'filename': fn, 'url': f'/static/vo_output/{fn}',
                         'size': os.path.getsize(p),
                         'mtime': datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')})
    except OSError:
        pass
    rows.sort(key=lambda r: r['mtime'], reverse=True)
    return jsonify({'status': 'ok', 'files': rows[:40]})


@app.route('/api/vo/delete', methods=['POST'])
@admin_required
def api_vo_delete():
    data = request.get_json(silent=True) or {}
    fn = os.path.basename((data.get('filename') or '').strip())
    if not fn:
        return jsonify({'status': 'error', 'message': 'filename kosong'}), 400
    p = os.path.join(VO_OUT_DIR, fn)
    if os.path.exists(p):
        os.remove(p)
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'File nggak ketemu'}), 404

# ===== DATA PRODUK (katalog produk + dropdown produk di generator) =====
# Diadaptasi dari GIGAS (`_product_context` / `/produk`), tapi KAC single-tenant:
# nggak ada tenant_id, dan promo ikut nempel di produk (bukan di profil brand terpisah).

def _safe_products():
    """Ambil daftar produk untuk dropdown. Kalau DB error → list kosong (halaman tetap jalan)."""
    try:
        return get_products()
    except Exception as e:
        print(f'WARN get_products gagal: {e}')
        return []


def _generator_product_ctx(product_id):
    """Blok konteks produk buat prompt generator (storyboard/copywriting/adset).

    Return dict: product / brand / desc / benefits / promo / block.
    Kalau product_id kosong atau nggak ketemu → product=None + block='' artinya
    prompt TETAP pakai teks lama apa adanya (backward compatible, nol perubahan perilaku).
    """
    if product_id in (None, '', 0, '0', 'null', 'undefined'):
        return {'product': None, 'brand': None, 'desc': None, 'benefits': [], 'promo': '', 'block': ''}
    _p = get_product(product_id)
    if not _p:
        return {'product': None, 'brand': None, 'desc': None, 'benefits': [], 'promo': '', 'block': ''}
    brand = (_p.get('name') or '').strip()
    desc = (_p.get('description') or '').strip()
    benefits = [b.strip() for b in (_p.get('benefits') or '').split('\n') if b.strip()]
    promo = (_p.get('promo') or '').strip()
    lines = ['', 'KONTEKS PRODUK (WAJIB dipakai, jangan ngarang di luar ini):', f'Produk: {brand}']
    if desc:
        lines.append(f'Deskripsi: {desc}')
    if promo:
        lines.append(f'PENAWARAN/PROMO AKTIF (WAJIB jadi isi BENEFIT / CTA): {promo}')
    if benefits:
        lines.append('Keunggulan/manfaat utama:')
        lines += [f'- {b}' for b in benefits]
    return {
        'product': _p, 'brand': brand, 'desc': desc,
        'benefits': benefits, 'promo': promo,
        'block': '\n'.join(lines) + '\n',
    }


@app.route('/produk')
@admin_required
def produk_page():
    """Halaman Data Produk — katalog produk yang dipakai generator."""
    return render_template('produk.html', products=get_products())


@app.route('/api/products')
@admin_required
def api_products():
    return jsonify({'status': 'ok', 'products': get_products()})


@app.route('/api/products/save', methods=['POST'])
@admin_required
def api_products_save():
    body = request.get_json(silent=True) or {}
    ok, pid, err = save_product(
        body.get('name'), body.get('description', ''), body.get('benefits', ''),
        body.get('promo', ''), product_id=body.get('id')
    )
    if not ok:
        return jsonify({'status': 'error', 'message': err}), 400
    return jsonify({'status': 'ok', 'id': pid, 'products': get_products()})


@app.route('/api/products/delete', methods=['POST'])
@admin_required
def api_products_delete():
    body = request.get_json(silent=True) or {}
    ok, err = delete_product(body.get('id'))
    if not ok:
        return jsonify({'status': 'error', 'message': err}), 400
    return jsonify({'status': 'ok', 'products': get_products()})


TREN_DATA_FILE = os.path.join(os.path.dirname(__file__), 'tren_data.json')


def read_tren_data():
    """Baca tren_data.json — hasil cron tren_riset.py."""
    try:
        with open(TREN_DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'generated_at': None, 'window_days': 30, 'count': 0, 'topics': []}


@app.route('/tren')
@admin_required
def tren_page():
    """Halaman Tren Kreatif — topik parenting 30 hari + angle copywriting."""
    return render_template('tren.html', active_page='tren')


@app.route('/api/tren')
@admin_required
def api_tren():
    """API data tren — dibaca halaman /tren via fetch."""
    return jsonify(read_tren_data())


def init_app():
    """Initialize database tables."""
    try:
        init_db()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Database init error: {e}")


# Initialize on startup
init_app()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
