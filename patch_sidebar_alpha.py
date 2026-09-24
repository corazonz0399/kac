#!/usr/bin/env python3
"""Tambah label Alpha Testing di sidebar Campaign Studio — semua template aktif."""
import glob, os

BADGE = '<span>Campaign Studio <span class="badge" style="margin-left:4px;font-size:0.55rem;padding:1px 6px;background:linear-gradient(135deg,#6c5ce7,#fd79a8);color:#fff;border-radius:4px">Alpha Testing</span></span></a>'

patched = 0
for path in sorted(glob.glob('/home/ubuntu/report_generos/templates/*.html')):
    base = os.path.basename(path)
    if 'backup' in base or '_live_backup' in base or 'before_aggregate' in base:
        continue
    html = open(path, encoding='utf-8').read()
    if '<span>Campaign Studio</span></a>' not in html:
        continue
    n = html.count('<span>Campaign Studio</span></a>')
    html = html.replace('<span>Campaign Studio</span></a>', BADGE)
    open(path, 'w', encoding='utf-8').write(html)
    patched += 1
    print(f'  patched {base} ({n}x)')
print(f'total patched: {patched}')
