#!/usr/bin/env python3
"""
backup_live.py — Download current live version from Hostinger & archive.
"""
import ftplib, io, zipfile, os, datetime

FTP_HOST = 'ftp.generosindo.com'
FTP_USER = 'u1734629'
FTP_PASS = 'Sisirkuning123!'
BASE = '/public_html/report.anaksehatgeneros.com'
BACKUP_DIR = '/home/ubuntu/Live Backup'

def main():
    now = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f'{BACKUP_DIR}/live_{now}'
    os.makedirs(backup_path, exist_ok=True)

    ftp = ftplib.FTP(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.encoding = 'utf-8'

    files_to_download = [
        ('app.py', f'{BASE}/app.py'),
        ('config.py', f'{BASE}/config.py'),
    ]

    # Download templates
    ftp.cwd(f'{BASE}/templates')
    tmpl_files = ftp.nlst()
    for f in tmpl_files:
        if f in ('.', '..', 'tmp'):
            continue
        files_to_download.append((f'templates/{f}', f'{BASE}/templates/{f}'))

    # Download static
    try:
        ftp.cwd(f'{BASE}/static')
        static_files = ftp.nlst()
        for f in static_files:
            if f in ('.', '..'):
                continue
            files_to_download.append((f'static/{f}', f'{BASE}/static/{f}'))
    except:
        pass

    # Download all
    for local_name, remote_path in files_to_download:
        local_file = f'{backup_path}/{local_name}'
        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        try:
            with open(local_file, 'wb') as fh:
                ftp.retrbinary(f'RETR {remote_path}', fh.write)
            print(f'  ✅ {local_name}')
        except Exception as e:
            print(f'  ❌ {local_name}: {e}')

    ftp.quit()

    # Create zip
    zip_path = f'/home/ubuntu/Live Backup/live_{now}.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(backup_path):
            for fn in files:
                abs_path = os.path.join(root, fn)
                arc_name = os.path.relpath(abs_path, backup_path)
                zf.write(abs_path, arc_name)

    print(f'\n📦 Backup selesai: {zip_path}')
    print(f'   ({len(files_to_download)} files)')

if __name__ == '__main__':
    main()
