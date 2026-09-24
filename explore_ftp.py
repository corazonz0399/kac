import ftplib

FTP_HOST = 'ftp.generosindo.com'
FTP_USER = 'u1734629'
FTP_PASS = 'Sisirkuning123!'

ftp = ftplib.FTP(FTP_HOST)
ftp.login(FTP_USER, FTP_PASS)
print(f"Root PWD: {ftp.pwd()}")

# Cari struktur
def ls(dir_path):
    try:
        ftp.cwd(dir_path)
        items = []
        ftp.retrlines('LIST', items.append)
        print(f"\n--- {dir_path} ---")
        for i in items[:30]:
            print(f"  {i}")
        return ftp.pwd()
    except Exception as e:
        print(f"  Error cwd {dir_path}: {e}")
        return None

# Coba beberapa path
base = 'public_html/report.anaksehatgeneros.com'
ls(base)
ls(f'{base}/static')
ls(f'{base}/templates')

# Cek path adjusment
ftp.cwd(f'/{base}')
print(f"\nAfter cwd /{base}: {ftp.pwd()}")
try:
    ftp.cwd('static')
    print("  static FOUND!")
    items = []
    ftp.retrlines('LIST', items.append)
    for i in items:
        print(f"  {i}")
except Exception as e:
    print(f"  static ERROR: {e}")

ftp.quit()
