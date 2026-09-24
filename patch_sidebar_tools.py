#!/usr/bin/env python3
"""Tambah nav link Dailyleveling di section Tools semua template aktif."""
import os, glob

SKIP = ['_backup', '_live_backup', 'before_aggregate', 'dailyleveling.html']
link = '''        <a class="nav-item admin-only" href="/dailyleveling"><span><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg></span> <span>Dailyleveling</span></a>
'''

patched = 0
skipped = []
for path in sorted(glob.glob('/home/ubuntu/report_generos/templates/*.html')):
    base = os.path.basename(path)
    if any(s in base for s in SKIP):
        continue
    html = open(path, 'r', encoding='utf-8').read()
    if 'href="/dailyleveling"' in html:
        skipped.append(base + ' (sudah ada)')
        continue
    anchor = '        <div class="nav-section">Sistem</div>'
    if anchor not in html:
        # coba anchor lain: Tools section
        if 'nav-section">Tools' not in html:
            skipped.append(base + ' (no Tools section)')
            continue
        # Tools ada tapi Sistem nggak — sisipin sebelum nav-subs Tools item terakhir? fallback: skip
        skipped.append(base + ' (no Sistem anchor)')
        continue
    html = html.replace(anchor, link + anchor, 1)
    open(path, 'w', encoding='utf-8').write(html)
    patched += 1
    print('  patched:', base)

print(f'patched={patched}')
for s in skipped:
    print('  skip:', s)
