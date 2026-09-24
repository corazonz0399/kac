import ftplib

FTP_HOST = 'ftp.generosindo.com'
FTP_USER = 'u1734629'
FTP_PASS = 'Sisirkuning123!'

ftp = ftplib.FTP(FTP_HOST)
ftp.login(FTP_USER, FTP_PASS)

# Cek apakah templates/ ada
base = '/public_html/report.anaksehatgeneros.com'
ftp.cwd(base)

# List semua isi
items = []
ftp.retrlines('LIST -a', items.append)
print(f"Items in {base}:")
for i in items:
    print(f"  {i}")

print(f"\nPWD: {ftp.pwd()}")

# Coba cek kalo ada dir templates
try:
    ftp.cwd('templates')
    print("\ntemplates/ EXISTS!")
    items = []
    ftp.retrlines('LIST', items.append)
    for i in items[:10]:
        print(f"  {i}")
except Exception as e:
    print(f"\ntemplates/ ERROR: {e}")

ftp.quit()
