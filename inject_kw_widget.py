#!/usr/bin/env python3
"""Inject Kowalski chat widget ke semua template aktif (sebelum </body>)."""
import os, glob

SKIP = ['_backup', '_live_backup', 'before_aggregate', 'backup_']
widget = open('/home/ubuntu/report_generos/kw_widget.html', encoding='utf-8').read()

patched = 0
skipped = []
for path in sorted(glob.glob('/home/ubuntu/report_generos/templates/*.html')):
    base = os.path.basename(path)
    if any(s in base for s in SKIP):
        continue
    html = open(path, 'r', encoding='utf-8').read()
    if 'kwFab' in html:
        skipped.append(base + ' (sudah ada)')
        continue
    if '</body>' not in html:
        skipped.append(base + ' (no body)')
        continue
    html = html.replace('</body>', widget + '\n</body>', 1)
    open(path, 'w', encoding='utf-8').write(html)
    patched += 1
    print('  injected:', base)

print(f'injected={patched}')
for s in skipped:
    print('  skip:', s)
