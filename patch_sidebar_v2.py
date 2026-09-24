#!/usr/bin/env python3
"""Ganti sidebar di semua template report_generos dengan struktur baru:
Dashboard | Analisis▾(Kampanye,Adset,Iklan) | Creator▾(Storyboard,Copywriting,Adset) admin
Tools▾(Campaign Studio,Daily Leveling,Cron Management) admin | Report▾(ROAS,Bank Konten)
"""
import os, re, glob

TEMPLATES = '/home/ubuntu/report_generos/templates'

ICON_DASH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>'
ICON_SEARCH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
ICON_PLAY = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="18" rx="3" ry="3"/><polygon points="10 8 16 12 10 16 10 8"/></svg>'
ICON_GEAR = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
ICON_REPORT = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>'
ICON_ARROW = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>'
ICON_BULLET = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>'
ICON_PEN = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>'
ICON_LAYER = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>'
ICON_ROCKET = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.06-2.91a2.2 2.2 0 0 0-2.94-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></svg>'
ICON_CAL = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'
ICON_BANK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
ICON_TREND = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>'

def sidebar_html(p):
    """p = dict placeholder -> 'active'/'open'/''"""
    return f'''<aside class="sidebar collapsed" id="sidebar">
    <div class="sidebar-brand">
        <span class="brand-icon">{ICON_DASH}</span>
        <div>
            <div class="brand-name">Generos Ads Center</div>
            <div class="brand-sub">Meta Ads Report</div>
        </div>
    </div>
    <nav class="sidebar-nav">
        <a class="nav-item {p['dash']}" href="/dashboard"><span>{ICON_DASH}</span> <span>Dashboard</span></a>

        <div class="nav-section">Analisis</div>
        <a class="nav-item" href="#" onclick="toggleSubmenu(event, 'analisisSubs')">
            <span>{ICON_SEARCH}</span> <span>Analisis</span>
            <span class="nav-arrow {p['analisis_open']}">{ICON_ARROW}</span>
        </a>
        <div class="nav-subs {p['analisis_open']}" id="analisisSubs">
            <a href="/analisis/kampanye" class="nav-sub {p['kampanye']}"><span>{ICON_BULLET}</span> <span>Kampanye</span></a>
            <a href="/analisis/adset" class="nav-sub {p['adset']}"><span>{ICON_LAYER}</span> <span>Adset</span></a>
            <a href="/analisis/iklan" class="nav-sub {p['iklan']}"><span>{ICON_PEN}</span> <span>Iklan</span></a>
        </div>

        <div class="nav-section admin-only">Creator</div>
        <a class="nav-item admin-only" href="#" onclick="toggleSubmenu(event, 'creatorSubs')">
            <span>{ICON_PLAY}</span> <span>Creator</span>
            <span class="nav-arrow {p['creator_open']}">{ICON_ARROW}</span>
        </a>
        <div class="nav-subs {p['creator_open']}" id="creatorSubs">
            <a href="/storyboard" class="nav-sub admin-only {p['storyboard']}"><span>{ICON_PEN}</span> <span>Storyboard</span></a>
            <a href="/copywriting-generator" class="nav-sub admin-only {p['copywriting']}"><span>{ICON_BULLET}</span> <span>Copywriting</span></a>
            <a href="/adset-generator" class="nav-sub admin-only {p['adsetgen']}"><span>{ICON_LAYER}</span> <span>Adset</span></a>
        </div>

        <div class="nav-section admin-only">Tools</div>
        <a class="nav-item admin-only" href="#" onclick="toggleSubmenu(event, 'toolsSubs')">
            <span>{ICON_GEAR}</span> <span>Tools</span>
            <span class="nav-arrow {p['tools_open']}">{ICON_ARROW}</span>
        </a>
        <div class="nav-subs {p['tools_open']}" id="toolsSubs">
            <a href="/dev/campaign-studio" class="nav-sub admin-only {p['studio']}"><span>{ICON_ROCKET}</span> <span>Campaign Studio</span></a>
            <a href="/dailyleveling" class="nav-sub admin-only {p['dl']}"><span>{ICON_TREND}</span> <span>Daily Leveling</span></a>
            <a href="/cron-management" class="nav-sub admin-only {p['cron']}"><span>{ICON_CAL}</span> <span>Cron Management</span></a>
        </div>

        <div class="nav-section">Report</div>
        <a class="nav-item" href="#" onclick="toggleSubmenu(event, 'reportSubs')">
            <span>{ICON_REPORT}</span> <span>Report</span>
            <span class="nav-arrow {p['report_open']}">{ICON_ARROW}</span>
        </a>
        <div class="nav-subs {p['report_open']}" id="reportSubs">
            <a href="/roas" class="nav-sub {p['roas']}"><span>{ICON_TREND}</span> <span>ROAS Report</span></a>
            <a href="/bank-konten" class="nav-sub admin-only {p['bank']}"><span>{ICON_BANK}</span> <span>Bank Konten</span></a>
        </div>
    </nav>
    <div class="sidebar-user">
        <div class="user-avatar">GA</div>
        <div style="flex:1;min-width:0">
            <div class="user-name">Gilang Aji</div>
            <div class="user-role">Administrator</div>
        </div>
        <button class="btn-logout" onclick="logout()" title="Logout"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg></button>
    </div>
</aside>'''

TOGGLE_SCRIPT = '''<script>
if (typeof window.toggleSubmenu !== 'function') {
    window.toggleSubmenu = function(e, id) {
        e.preventDefault();
        const el = document.getElementById(id);
        if (el) el.classList.toggle('open');
    };
}
</script>'''

def page_map(name):
    m = {'dash':'','analisis_open':'','kampanye':'','adset':'','iklan':'',
         'creator_open':'','storyboard':'','copywriting':'','adsetgen':'',
         'tools_open':'','studio':'','dl':'','cron':'',
         'report_open':'','roas':'','bank':''}
    if name == 'dashboard.html': m['dash'] = 'active'
    elif name == 'analisis_kampanye.html': m['analisis_open']='open'; m['kampanye']='active'
    elif name == 'analisis_adset.html': m['analisis_open']='open'; m['adset']='active'
    elif name == 'analisis_iklan.html': m['analisis_open']='open'; m['iklan']='active'
    elif name == 'storyboard_generator.html': m['creator_open']='open'; m['storyboard']='active'
    elif name == 'copywriting_generator.html': m['creator_open']='open'; m['copywriting']='active'
    elif name == 'adset_generator.html': m['creator_open']='open'; m['adsetgen']='active'
    elif name in ('cb_studio.html','dev_campaign_builder.html'): m['tools_open']='open'; m['studio']='active'
    elif name == 'dailyleveling.html': m['tools_open']='open'; m['dl']='active'
    elif name in ('cron_list.html','cron_detail.html','cron_log.html','cron_remake.html','demo_cron.html'): m['tools_open']='open'; m['cron']='active'
    elif name in ('roas.html','roas_dashboard.html'): m['report_open']='open'; m['roas']='active'
    elif name == 'bank_konten.html': m['report_open']='open'; m['bank']='active'
    return m

SKIP = ['backup', 'before_aggregate', 'live_backup']
patched, skipped = [], []
for path in sorted(glob.glob(f'{TEMPLATES}/*.html')):
    base = os.path.basename(path)
    if any(s in base for s in SKIP):
        continue
    html = open(path, encoding='utf-8').read()
    m = re.search(r'<aside class="sidebar[^"]*" id="sidebar">.*?</aside>', html, re.S)
    if not m:
        skipped.append(base + ' (no sidebar)')
        continue
    new_sidebar = sidebar_html(page_map(base))
    html = html[:m.start()] + new_sidebar + html[m.end():]
    # Fallback toggleSubmenu kalau belum ada (idempotent)
    if "typeof window.toggleSubmenu" not in html:
        html = html.replace('</aside>', '</aside>\n' + TOGGLE_SCRIPT, 1)
    open(path, 'w', encoding='utf-8').write(html)
    patched.append(base)
    print('patched:', base)
print(f'\npatched={len(patched)}')
for s in skipped:
    print('skip:', s)
