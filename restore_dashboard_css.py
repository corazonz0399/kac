import re

with open('/home/ubuntu/report_generos_backup_20260717_204033_pre_universal_theme/templates/dashboard.html') as f:
    backup = f.read()
with open('/home/ubuntu/report_generos/templates/dashboard.html') as f:
    current = f.read()

m = re.search(r'<style>(.*?)</style>', backup, re.DOTALL)
if not m:
    print("No inline style in backup!")
    exit(1)

style = m.group(1)

# Hapus :root, [data-theme] blocks
style = re.sub(r':root\s*\{[^}]*\}', '', style)
style = re.sub(r'\[data-theme="light"\]\s*\{[^}]*\}', '', style)
style = re.sub(r'\[data-theme="light"\]\s*\.topbar\s*\{[^}]*\}', '', style)

# Hapus reset & body
style = re.sub(r'\*\s*\{[^}]*\}', '', style)
style = re.sub(r'body\s*\{[^}]*\}', '', style)

# Hapus topbar section & components
style = re.sub(r'/\* Topbar \*/.*?(?=/\*|$)', '', style, flags=re.DOTALL)
for sel in ['.topbar-left', '.topbar-left .logo', '.topbar-right',
            '.theme-toggle', '.theme-toggle:hover', '.topbar-date',
            '.sidebar-toggle', '.sidebar-toggle:hover']:
    style = re.sub(re.escape(sel) + r'\s*\{[^}]*\}', '', style)

# Hapus main section
style = re.sub(r'/\* Main \*/.*?(?=/\*|$)', '', style, flags=re.DOTALL)
style = re.sub(r'\.main\s*\{[^}]*\}', '', style)
style = re.sub(r'\.main\.expanded\s*\{[^}]*\}', '', style)

# Hapus cpr-bagus/mahal yg udah di style.css
style = re.sub(r'\.cpr-bagus\s*\{[^}]*\}', '', style)
style = re.sub(r'\.cpr-mahal\s*\{[^}]*\}', '', style)

# Hapus sidebar-backdrop
style = re.sub(r'\.sidebar-backdrop[^}]*\}', '', style)

# Hapus tabel duplikat
style = re.sub(r'\.table-wrap\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table th\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table th:hover\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table td\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table tbody tr:hover\s*\{[^}]*\}', '', style)
style = re.sub(r'\.table \.num\s*\{[^}]*\}', '', style)

# Hapus toast duplikat
style = re.sub(r'\.toast[^}]*\}', '', style)
style = re.sub(r'\.toast\.toast-success[^}]*\}', '', style)
style = re.sub(r'\.toast\.toast-error[^}]*\}', '', style)

# Hapus sidebar mobile media query duplikat
style = re.sub(r'@media \(max-width: 768px\)\s*\{[^}]*\.sidebar[^}]*\}[^}]*\}', '', style)

# Bersihin baris kosong
style = re.sub(r'\n{3,}', '\n\n', style)
style = style.strip()

print(f"Style length: {len(style)}")
print("=== PREVIEW ===")
print(style[:600])
print("...")
print(style[-300:])

# Replace di current
current_new = re.sub(
    r'<style>.*?</style>',
    f'<style>{style}</style>',
    current,
    count=1,
    flags=re.DOTALL
)

if current_new != current:
    with open('/home/ubuntu/report_generos/templates/dashboard.html', 'w') as f:
        f.write(current_new)
    print("\n✅ WRITTEN!")
else:
    print("\n⚠️ No changes")
