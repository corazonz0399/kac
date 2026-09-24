#!/usr/bin/env python3
"""Terapkan logo baru ke topbar + sidebar semua template aktif."""
import os, glob

SKIP = ['_backup', '_live_backup', 'before_aggregate', 'backup_']

topbar_new = '<img src="/static/kowalski-logo.png" alt="KS" style="height:26px;width:auto;object-fit:contain;border-radius:6px"> <span class="logo">Generos Ads Center</span>'
brand_new = '<img src="/static/kowalski-logo.png" alt="KS" class="brand-logo-img" style="width:30px;height:30px;object-fit:contain;border-radius:8px">'

patched = 0
skipped = []
for path in sorted(glob.glob('/home/ubuntu/report_generos/templates/*.html')):
    base = os.path.basename(path)
    if any(s in base for s in SKIP):
        continue
    html = open(path, 'r', encoding='utf-8').read()
    orig = html
    # Topbar: logo text -> img + text
    if '<span class="logo">Generos Ads Center</span>' in html:
        html = html.replace('<span class="logo">Generos Ads Center</span>', topbar_new, 1)
    else:
        skipped.append(base + ' (no topbar logo)')
    # Sidebar: brand-icon -> img
    if '<span class="brand-icon">' in html:
        # ganti seluruh span brand-icon sampai </span> penutup pertama yang cocok (svg selesai)
        start = html.index('<span class="brand-icon">')
        end = html.index('</span>', start) + len('</span>')
        # brand-icon span berisi satu svg yang selesai di </svg></span> — cari </svg> dulu
        svg_end = html.index('</svg>', start) + len('</svg>')
        # span ditutup setelah </svg> — cari </span> setelah svg_end
        span_end = html.index('</span>', svg_end) + len('</span>')
        html = html[:start] + brand_new + html[span_end:]
    if html != orig:
        open(path, 'w', encoding='utf-8').write(html)
        patched += 1
        print('  patched:', base)

print(f'patched={patched}')
for s in skipped:
    print('  skip:', s)
