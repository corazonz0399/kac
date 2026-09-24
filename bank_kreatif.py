"""
bank_kreatif.py — Bank Storyboard + VO dari Google Drive (read-only service account).

Struktur Drive yang didukung (dua-duanya jalan):

  Folder Root (Bank Kreatif)
  ├── Paket 1 - Hook Susah Makan/        <- 1 paket = 1 SUBFOLDER
  │   ├── storyboard.txt (atau .docx/.md/.pdf/.json)
  │   └── vo-Kore.mp3
  ├── Paket 2 - Testimoni/               <- boleh ada banyak file VO
  │   ├── naskah.docx
  │   ├── vo-A.mp3
  │   └── vo-B.mp3
  └── paket-3.zip                        <- ATAU 1 ZIP berisi storyboard + VO

Aturan main:
- Service account kita cuma punya izin VIEWER → modul ini READ-ONLY ke Drive.
- Semua file diunduh ke static/bank/<slug>/ supaya bisa diputar di dashboard.
- Index disimpan di bank_data/index.json (dipakai halaman /bank-kreatif).
"""

import io
import json
import os
import re
import shutil
import zipfile
from datetime import datetime

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
    _HAS_GOOGLE = True
    _HAS_OAUTH = True
except ImportError:  # pragma: no cover
    _HAS_GOOGLE = False
    _HAS_OAUTH = False

OAUTH_TOKEN_PATH = '/home/ubuntu/.hermes/google_token.json'
DRIVE_WRITE_SCOPES = ['https://www.googleapis.com/auth/drive']

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
SA_CANDIDATES = [
    '/home/ubuntu/.hermes/secrets/drive-service-account.json',
    '/home/ubuntu/.hermes/credentials/google-service-account.json',
    '/home/ubuntu/.hermes/google-service-account.json',
]

BANK_ROOT = '/home/ubuntu/report_generos'
STORE_DIR = os.path.join(BANK_ROOT, 'static', 'bank')
INDEX_PATH = os.path.join(BANK_ROOT, 'bank_data', 'index.json')

MAX_FILES = 300                 # jaga-jaga biar sync nggak kelamaan
MAX_FILE_MB = 60                # lewati file raksasa (video mentah biasanya nggak perlu)
ZIP_MAX_MEMBERS = 200

STORYBOARD_EXT = {'.txt', '.md', '.docx', '.pdf', '.json', '.csv', '.rtf'}
VO_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.opus', '.aac', '.flac'}
VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv', '.avi'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
DOC_EXT = {'.xlsx', '.doc', '.pptx', '.zip'}

FOLDER_MIME = 'application/vnd.google-apps.folder'
GOOGLE_DOC_EXPORT = {
    'application/vnd.google-apps.document': ('text/plain', '.txt'),
    'application/vnd.google-apps.spreadsheet': ('text/csv', '.csv'),
}


# --------------------------------------------------------------------------
# Helper dasar
# --------------------------------------------------------------------------
def _slug(s, maxlen=60):
    s = (s or 'paket').strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return (s or 'paket')[:maxlen]


def _ext(name):
    return os.path.splitext(name or '')[1].lower()


def _jenis(name):
    base = (name or '').strip().lower()
    e = _ext(name)
    # file instruksi (bukan naskah iklan) jangan dianggap storyboard
    if re.match(r'^(cara|readme|petunjuk|panduan|catatan|keterangan)[-_. ]', base) or base in (
            'cara-pakai.txt', 'readme.txt', 'readme.md'):
        return 'doc'
    if e in VO_EXT:
        return 'vo'
    if e in STORYBOARD_EXT:
        return 'storyboard'
    if e in VIDEO_EXT:
        return 'video'
    if e in IMAGE_EXT:
        return 'image'
    return 'lain'


def _prioritas_naskah(nama):
    """Skor prioritas pemilihan naskah utama dalam satu paket (kecil = lebih diprioritaskan)."""
    b = (nama or '').lower()
    if re.search(r'storyboard', b):
        return 0
    if re.search(r'naskah|script|skrip|narasi|vo', b):
        return 1
    return 2


def sa_path():
    for p in SA_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def drive_client():
    """Balikin objek Drive API pakai service account (read-only)."""
    if not _HAS_GOOGLE:
        raise RuntimeError('google-api-python-client belum keinstall di venv ini')
    p = sa_path()
    if not p:
        raise RuntimeError('File service account nggak ketemu')
    creds = service_account.Credentials.from_service_account_file(
        p, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def oauth_drive_client():
    """Drive client write menggunakan OAuth user token Hermes."""
    if not (_HAS_GOOGLE and _HAS_OAUTH):
        raise RuntimeError('Google OAuth client belum tersedia')
    if not os.path.exists(OAUTH_TOKEN_PATH):
        raise RuntimeError('OAuth Google belum terautentikasi')
    creds = OAuthCredentials.from_authorized_user_file(OAUTH_TOKEN_PATH, DRIVE_WRITE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(OAUTH_TOKEN_PATH, 'w', encoding='utf-8') as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError('OAuth Google token tidak valid')
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def oauth_upload_paket(paket_nama, storyboard_files, vo_files, parent_id):
    """Buat folder paket di Drive OAuth lalu upload file storyboard/VO."""
    drive = oauth_drive_client()
    nama = (paket_nama or 'Paket Tanpa Nama').strip()
    folder = drive.files().create(body={'name': nama, 'mimeType': FOLDER_MIME,
                                        'parents': [parent_id]},
                                  fields='id,name,webViewLink', supportsAllDrives=True).execute()
    uploaded = []
    for kategori, items in (('storyboard', storyboard_files), ('vo', vo_files)):
        for fs in items or []:
            filename = os.path.basename(getattr(fs, 'filename', '') or '')
            if not filename:
                continue
            tmp = os.path.join('/tmp', 'kac-oauth-' + re.sub(r'[^A-Za-z0-9_.-]', '_', filename))
            fs.save(tmp)
            try:
                media = MediaFileUpload(tmp, resumable=True)
                f = drive.files().create(body={'name': filename, 'parents': [folder['id']]},
                                         media_body=media,
                                         fields='id,name,mimeType,size,webViewLink,parents',
                                         supportsAllDrives=True).execute()
                f['jenis'] = kategori
                uploaded.append(f)
            finally:
                try: os.remove(tmp)
                except OSError: pass
    return {'status': 'ok', 'folder': folder, 'files': uploaded}


def sa_email():
    p = sa_path()
    if not p:
        return ''
    try:
        with open(p) as f:
            return json.load(f).get('client_email', '')
    except Exception:
        return ''


# --------------------------------------------------------------------------
# Drive: list & download
# --------------------------------------------------------------------------
def list_children(drive, folder_id):
    """Semua anak langsung (file + folder) dari sebuah folder Drive."""
    out, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id,name,mimeType,size,modifiedTime,webViewLink)',
            pageSize=100, pageToken=token, supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        out.extend(resp.get('files', []))
        token = resp.get('nextPageToken')
        if not token or len(out) >= MAX_FILES:
            break
    return out[:MAX_FILES]


def folder_meta(drive, folder_id):
    return drive.files().get(fileId=folder_id, fields='id,name',
                             supportsAllDrives=True).execute()


def download_file(drive, file_id, dest_path, mime=''):
    """Unduh file Drive ke dest_path. Google-native file diekspor ke teks."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    if mime in GOOGLE_DOC_EXPORT:
        export_mime, ext = GOOGLE_DOC_EXPORT[mime]
        dest_path = os.path.splitext(dest_path)[0] + ext
        data = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
        with open(dest_path, 'wb') as f:
            f.write(data)
        return dest_path
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(dest_path, 'wb') as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest_path


# --------------------------------------------------------------------------
# Pembacaan naskah
# --------------------------------------------------------------------------
def baca_docx(path):
    """Ambil teks .docx termasuk isi tabel (python-docx nggak sentuh tabel)."""
    try:
        import docx
    except ImportError:
        return ''
    out = []
    try:
        doc = docx.Document(path)
    except Exception:
        return ''
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            t = Paragraph(child, doc).text.strip()
            if t:
                out.append(t)
        elif tag == 'tbl':
            tbl = Table(child, doc)
            for row in tbl.rows:
                cells = [c.text.strip().replace('\n', ' ') for c in row.cells]
                if any(cells):
                    out.append(' | '.join(cells))
    return '\n'.join(out)


def baca_pdf(path):
    try:
        import fitz  # pymupdf
    except ImportError:
        return ''
    try:
        with fitz.open(path) as d:
            return '\n'.join(p.get_text() for p in d)
    except Exception:
        return ''


def baca_teks(path):
    """Baca naskah dari file apa pun yang didukung. Balikin '' kalau nggak bisa."""
    e = _ext(path)
    if e in ('.txt', '.md', '.csv', '.json', '.rtf'):
        for enc in ('utf-8', 'utf-8-sig', 'latin-1'):
            try:
                with open(path, encoding=enc) as f:
                    return f.read()
            except Exception:
                continue
        return ''
    if e == '.docx':
        return baca_docx(path)
    if e == '.pdf':
        return baca_pdf(path)
    return ''


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------
def _muat_index():
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'folder_id': '', 'synced_at': '', 'paket': []}


def _simpan_index(idx):
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    tmp = INDEX_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX_PATH)


def _ekstrak_zip(zip_path, out_dir):
    """Ekstrak zip dengan proteksi path traversal + jumlah member."""
    hasil = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            for i, nm in enumerate(z.namelist()):
                if i >= ZIP_MAX_MEMBERS:
                    break
                if nm.endswith('/') or nm.startswith('__MACOSX'):
                    continue
                target = os.path.normpath(os.path.join(out_dir, nm))
                if not target.startswith(os.path.normpath(out_dir) + os.sep):
                    continue          # zip slip → lewati
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(nm) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                hasil.append({'path': target, 'nama': os.path.basename(nm)})
    except zipfile.BadZipFile:
        return []
    return hasil


def _kumpulkan_file(lokal, nama_drive, mime, size):
    """Bikin entri file buat index + baca naskahnya kalau storyboard."""
    jenis = _jenis(nama_drive)
    entri = {
        'nama': os.path.basename(nama_drive),
        'jenis': jenis,
        'mime': mime,
        'ukuran': int(size or 0),
        'ukuran_mb': round(int(size or 0) / 1048576, 2),
        'path': os.path.relpath(lokal, BANK_ROOT).replace(os.sep, '/'),
        'drive_id': '',
    }
    if jenis == 'storyboard':
        entri['teks'] = baca_teks(lokal)[:80000]
    return entri


def _refresh_jumlah(idx):
    """Hitung ulang total paket/vo/storyboard di index."""
    pk = idx.get('paket', [])
    idx['jumlah'] = {
        'paket': len([p for p in pk if p.get('jumlah', {}).get('total')]),
        'vo': sum(p.get('jumlah', {}).get('vo', 0) for p in pk),
        'storyboard': sum(p.get('jumlah', {}).get('storyboard', 0) for p in pk),
    }
    return idx


def simpan_upload(paket_nama, file_list):
    """Backward compat untuk upload universal."""
    return simpan_upload_2kolom(paket_nama, file_list, [])


def simpan_upload_2kolom(paket_nama, storyboard_files, vo_files):
    """Upload dengan 2 kolom terpisah (Storyboard + VO).

    Otomatis:
    1. Simpan semua file
    2. Buat ZIP bundel paket (<slug>.zip) di folder
    3. Index file & ekstrak teks naskah storyboard
    """
    slug = 'web-' + _slug(paket_nama or 'paket-web')
    kerja = os.path.join(STORE_DIR, slug)
    files, dilewati = [], []

    all_inputs = [('storyboard', f) for f in (storyboard_files or []) if getattr(f, 'filename', '')] + \
                 [('vo', f) for f in (vo_files or []) if getattr(f, 'filename', '')]

    if not all_inputs:
        return {'status': 'error', 'message': 'Belum ada file storyboard atau VO yang dipilih'}

    # Nama paket: kalau user nggak nulis, pakai nama file yang di-upload
    # (biar ZIP-nya nggak pernah lagi bernama generik "Paket_Upload_Web")
    _nama_final = (paket_nama or '').strip()
    if not _nama_final:
        _acuan = None
        for _kat, _fs in all_inputs:
            if _kat == 'storyboard':
                _acuan = getattr(_fs, 'filename', '') or ''
                if _acuan:
                    break
        if not _acuan:
            _acuan = getattr(all_inputs[0][1], 'filename', '') or ''
        _nama_final = os.path.splitext(os.path.basename(_acuan))[0].replace('_', ' ').strip()
        if not _nama_final:
            _nama_final = 'Paket Tanpa Nama'
        slug = 'web-' + _slug(_nama_final)
        kerja = os.path.join(STORE_DIR, slug)
        os.makedirs(kerja, exist_ok=True)

    # ⚠️ JANGAN PERNAH nimpa paket yang sudah ada. Kalau slug sudah dipakai,
    # bikin slug unik (-2, -3, ...). Ini mencegah data loss.
    try:
        _ada = {p.get('slug') for p in _muat_index().get('paket', [])}
        if slug in _ada:
            _akar, _n = slug, 2
            while f'{_akar}-{_n}' in _ada:
                _n += 1
            slug = f'{_akar}-{_n}'
            kerja = os.path.join(STORE_DIR, slug)
            _nama_final = _nama_final + f' ({_n})'
    except Exception:
        pass

    os.makedirs(kerja, exist_ok=True)

    for kategori, fs in all_inputs:
        nama = os.path.basename(getattr(fs, 'filename', '') or '')
        if not nama:
            continue
        aman = re.sub(r'[^\w\-. ()\u00c0-\u024f]', '_', nama)
        dest = os.path.join(kerja, aman)
        try:
            fs.save(dest)
        except Exception as e:
            dilewati.append({'nama': nama, 'alasan': str(e)[:100]})
            continue
        ukuran = os.path.getsize(dest)
        if ukuran > MAX_FILE_MB * 1048576:
            os.remove(dest)
            dilewati.append({'nama': nama, 'alasan': f'lebih dari {MAX_FILE_MB} MB'})
            continue
        
        # Kalau yang di-upload file ZIP → buka isinya, jangan simpan ZIP mentahnya.
        # Ini yang bikin paket ZIP lama bisa di-upload ulang dan isinya kebaca normal.
        if _ext(nama) == '.zip':
            _isi = _ekstrak_zip(dest, os.path.join(kerja, '_zip'))
            for z in _isi:
                zp, zn = z['path'], os.path.basename(z['path'])
                try:
                    zsize = os.path.getsize(zp)
                except OSError:
                    continue
                if zsize > MAX_FILE_MB * 1048576:
                    continue
                ent = _kumpulkan_file(zp, zn, '', zsize)
                # ikuti kolom tempat ZIP-nya ditaruh, tapi jangan lawan ekstensi
                if kategori == 'vo' and _ext(zn) in VO_EXT:
                    ent['jenis'] = 'vo'
                elif kategori == 'storyboard' and _ext(zn) in STORYBOARD_EXT:
                    ent['jenis'] = 'storyboard'
                files.append(ent)
            if _isi:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            # ZIP kosong / gagal dibuka → simpan apa adanya
            files.append(_kumpulkan_file(dest, nama, '', ukuran))
            continue

        # force kategori sesuai kolom kalau ekstensi masuk akal, atau pakai _kumpulkan_file
        entri = _kumpulkan_file(dest, nama, '', ukuran)
        if kategori == 'storyboard' and entri['jenis'] not in ('storyboard', 'doc', 'lain'):
            entri['jenis'] = 'storyboard' # override kolom
        elif kategori == 'vo' and entri['jenis'] != 'vo':
            entri['jenis'] = 'vo' # override kolom
        files.append(entri)

    if not files:
        return {'status': 'error', 'message': 'Nggak ada file yang berhasil diproses', 'dilewati': dilewati}

    # Bikin ZIP bundel untuk didownload — namanya DITURUNKAN DARI NAMA PAKET
    zip_nama = nama_zip_dari_paket(_nama_final)
    zip_path = os.path.join(kerja, zip_nama)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            lokal = os.path.join(kerja, f['nama'])
            if os.path.exists(lokal):
                zf.write(lokal, f['nama'])
    # Masukin ZIP ke daftar file juga (sebagai tipe zip / lain)
    files.append(_kumpulkan_file(zip_path, zip_nama, '', os.path.getsize(zip_path)))

    vo = [f for f in files if f.get('jenis') == 'vo']
    sb = [f for f in files if f.get('jenis') == 'storyboard']
    naskah = ''
    for f in sorted(sb, key=lambda x: _prioritas_naskah(x.get('nama', ''))):
        if (f.get('teks') or '').strip():
            naskah = f['teks']
            break

    idx = _muat_index()
    idx.setdefault('paket', [])
    idx['paket'] = [p for p in idx['paket'] if p.get('slug') != slug]
    idx['paket'].append({
        'slug': slug, 'nama': _nama_final, 'sumber': 'web',
        'drive_id': '', 'diperbarui': datetime.now().isoformat(timespec='seconds'),
        'jumlah': {'total': len([f for f in files if f.get('jenis') != 'zip']), 'vo': len(vo), 'storyboard': len(sb)},
        'files': files, 'naskah': naskah[:80000],
        'zip_bundle': f"static/bank/{slug}/{zip_nama}",
    })
    _refresh_jumlah(idx)
    _simpan_index(idx)
    return {'status': 'ok', 'slug': slug, 'nama': paket_nama,
            'jumlah': {'total': len(files), 'vo': len(vo), 'storyboard': len(sb)},
            'zip_url': f"/static/bank/{slug}/{zip_nama}",
            'dilewati': dilewati}


def hapus_paket(slug):
    """Hapus paket yang di-upload dari web (paket Drive nggak boleh dihapus di sini)."""
    idx = _muat_index()
    target = [p for p in idx.get('paket', []) if p.get('slug') == slug]
    if not target:
        return {'status': 'error', 'message': 'Paket nggak ketemu'}
    if target[0].get('sumber') != 'web':
        return {'status': 'error',
                'message': 'Paket ini dari Google Drive — hapus di Drive-nya, bukan di sini'}
    idx['paket'] = [p for p in idx['paket'] if p.get('slug') != slug]
    _refresh_jumlah(idx)
    _simpan_index(idx)
    kerja = os.path.join(STORE_DIR, slug)
    if os.path.isdir(kerja):
        shutil.rmtree(kerja, ignore_errors=True)
    return {'status': 'ok', 'slug': slug}


def nama_zip_dari_paket(nama_paket):
    """Nama file ZIP yang rapi, diturunkan dari nama paket."""
    rapi = re.sub(r'[^\w\- ]+', '', nama_paket or '').strip()
    rapi = re.sub(r'\s+', '_', rapi)
    return (rapi or 'Paket') + '.zip'


def rename_paket(slug, nama_baru):
    """Ganti nama paket upload web + ikut ganti nama file ZIP-nya."""
    nama_baru = (nama_baru or '').strip()
    if not nama_baru:
        return {'status': 'error', 'message': 'Nama baru nggak boleh kosong'}
    idx = _muat_index()
    target = [p for p in idx.get('paket', []) if p.get('slug') == slug]
    if not target:
        return {'status': 'error', 'message': 'Paket nggak ketemu'}
    p = target[0]
    if p.get('drive_id'):
        p['nama'] = nama_baru
        p['diperbarui'] = datetime.now().isoformat(timespec='seconds')
        _simpan_index(idx)
        return {'status': 'ok', 'slug': slug, 'nama': nama_baru,
                'drive_id': p['drive_id']}
    if p.get('sumber') != 'web':
        return {'status': 'error', 'message': 'Paket tidak mendukung rename'}

    kerja = os.path.join(STORE_DIR, slug)
    zip_lama = ''
    for f in p.get('files', []):
        if str(f.get('nama', '')).lower().endswith('.zip'):
            zip_lama = f['nama']
            break

    zip_baru = nama_zip_dari_paket(nama_baru)
    if zip_lama and zip_lama != zip_baru:
        src = os.path.join(kerja, zip_lama)
        dst = os.path.join(kerja, zip_baru)
        try:
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
            for f in p.get('files', []):
                if str(f.get('nama', '')).lower().endswith('.zip'):
                    f['nama'] = zip_baru
        except Exception as e:
            return {'status': 'error', 'message': f'Gagal rename file ZIP: {e}'}

    p['nama'] = nama_baru
    p['diperbarui'] = datetime.now().isoformat(timespec='seconds')
    if zip_baru:
        p['zip_bundle'] = f"static/bank/{slug}/{zip_baru}"
    _simpan_index(idx)
    return {'status': 'ok', 'slug': slug, 'nama': nama_baru, 'zip': zip_baru}


def sync(folder_id, progress=None):
    """Tarik semua paket dari folder Drive. Balikin ringkasan."""
    def log(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if not folder_id:
        return {'status': 'error', 'message': 'Folder ID kosong'}
    drive = drive_client()
    log('Baca struktur folder Drive...')
    root = list_children(drive, folder_id)
    folders = [f for f in root if f.get('mimeType') == FOLDER_MIME]
    loose = [f for f in root if f.get('mimeType') != FOLDER_MIME]

    idx = _muat_index()
    lama = {p['slug']: p for p in idx.get('paket', [])}
    paket_baru = []
    diproses = 0

    def proses_satu(slug, nama, drive_id, anak):
        """anak: list item Drive (file) buat satu paket."""
        nonlocal diproses
        kerja_dir = os.path.join(STORE_DIR, slug)
        os.makedirs(kerja_dir, exist_ok=True)
        # bersihkan isi lama supaya nggak ada aset yatim
        for f in os.listdir(kerja_dir):
            fp = os.path.join(kerja_dir, f)
            try:
                shutil.rmtree(fp) if os.path.isdir(fp) else os.remove(fp)
            except Exception:
                pass

        files = []
        for it in anak:
            if diproses >= MAX_FILES:
                break
            size_mb = int(it.get('size') or 0) / 1048576
            if size_mb > MAX_FILE_MB:
                files.append({'nama': it['name'], 'jenis': 'dilewati',
                              'alasan': f'terlalu besar ({size_mb:.0f} MB)', 'drive_id': it['id']})
                continue
            nama_aman = re.sub(r'[^\w\-. ()\u00c0-\u024f]', '_', it['name'])
            dest = os.path.join(kerja_dir, nama_aman)
            log(f'Unduh {nama} → {it["name"]}')
            try:
                real = download_file(drive, it['id'], dest, it.get('mimeType', ''))
            except Exception as e:
                files.append({'nama': it['name'], 'jenis': 'gagal',
                              'alasan': str(e)[:120], 'drive_id': it['id']})
                continue
            diproses += 1
            files.append(_kumpulkan_file(real, it['name'], it.get('mimeType', ''), it.get('size')))
            # ZIP → ekstrak, isinya ikut diklasifikasi
            if _ext(it['name']) == '.zip':
                log(f'Buka zip {it["name"]}...')
                for z in _ekstrak_zip(real, os.path.join(kerja_dir, '_zip')):
                    files.append(_kumpulkan_file(z['path'], z['nama'], '', os.path.getsize(z['path'])))
                try:
                    os.remove(real)
                except Exception:
                    pass
        vo = [f for f in files if f.get('jenis') == 'vo']
        sb = [f for f in files if f.get('jenis') == 'storyboard']
        naskah = ''
        for f in sorted(sb, key=lambda x: _prioritas_naskah(x.get('nama', ''))):
            if (f.get('teks') or '').strip():
                naskah = f['teks']
                break
        res = {
            'slug': slug, 'nama': nama, 'drive_id': drive_id, 'sumber': 'drive',
            'diperbarui': datetime.now().isoformat(timespec='seconds'),
            'jumlah': {'total': len(files), 'vo': len(vo), 'storyboard': len(sb)},
            'files': files, 'naskah': naskah[:80000],
        }
        if slug in lama and 'setor' in lama[slug]:
            res['setor'] = bool(lama[slug]['setor'])
        return res

    # --- paket berupa subfolder ---
    for fd in folders:
        anak = [f for f in list_children(drive, fd['id'])]
        paket_baru.append(proses_satu(_slug(fd['name']), fd['name'], fd['id'], anak))
        log(f"Paket '{fd['name']}' selesai")

    # --- paket berupa file lepas / zip di root ---
    if loose:
        paket_baru.append(proses_satu('_root', 'Paket Lepas (di luar subfolder)',
                                      folder_id, loose))

    # Paket hasil upload web TIDAK boleh kehapus waktu sync Drive
    web_sebelumnya = [p for p in _muat_index().get('paket', []) if p.get('sumber') == 'web']

    idx = {
        'folder_id': folder_id,
        'folder_nama': folder_meta(drive, folder_id).get('name', ''),
        'sa_email': sa_email(),
        'synced_at': datetime.now().isoformat(timespec='seconds'),
        'paket': paket_baru + web_sebelumnya,
    }
    _refresh_jumlah(idx)
    _simpan_index(idx)
    log('Selesai')
    return {'status': 'ok', 'ringkasan': idx['jumlah'], 'synced_at': idx['synced_at'],
            'paket': [{'nama': p['nama'], 'jumlah': p.get('jumlah')} for p in paket_baru]}
