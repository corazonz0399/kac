#!/usr/bin/env python3
"""Patch kwSend di semua template: sinkron -> async polling."""
import glob, os

OLD = """function kwSend() {
    const input = document.getElementById('kwInput');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    kwAddMsg('user', msg);
    kwTyping(true);
    document.getElementById('kwSendBtn').disabled = true;
    fetch('/api/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg, history: kwHistory.slice(0, -1)})
    })
    .then(r => r.json())
    .then(d => {
        kwTyping(false);
        kwAddMsg('assistant', d.reply || d.message || 'Gak dapet jawaban, coba lagi ya.');
    })
    .catch(err => {
        kwTyping(false);
        kwAddMsg('assistant', '⚠️ Gagal: ' + err.message);
    })
    .finally(() => { document.getElementById('kwSendBtn').disabled = false; });
}"""

NEW = """function kwSend() {
    const input = document.getElementById('kwInput');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    kwAddMsg('user', msg);
    kwTyping(true);
    document.getElementById('kwSendBtn').disabled = true;
    fetch('/api/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg, history: kwHistory.slice(0, -1)})
    })
    .then(r => r.json())
    .then(d => {
        if (!d.message_id) {
            kwTyping(false);
            kwAddMsg('assistant', d.reply || d.message || 'Gak dapet jawaban, coba lagi ya.');
            return;
        }
        pollChatResult(d.message_id);
    })
    .catch(err => {
        kwTyping(false);
        kwAddMsg('assistant', '⚠️ Gagal: ' + err.message);
    })
    .finally(() => { document.getElementById('kwSendBtn').disabled = false; });
}

function pollChatResult(message_id) {
    let tries = 0;
    const timer = setInterval(() => {
        tries++;
        if (tries > 100) {
            clearInterval(timer);
            kwTyping(false);
            kwAddMsg('assistant', 'Maap, prosesnya kelamaan. Coba tanya ulang ya.');
            return;
        }
        fetch('/api/chat/result/' + message_id)
        .then(r => r.json())
        .then(d => {
            if (d.status === 'done') {
                clearInterval(timer);
                kwTyping(false);
                kwAddMsg('assistant', d.reply || 'Gak dapet jawaban, coba lagi ya.');
            } else if (d.status === 'error') {
                clearInterval(timer);
                kwTyping(false);
                kwAddMsg('assistant', d.reply || 'Error, coba lagi ya.');
            }
        })
        .catch(() => {});
    }, 3000);
}"""

patched = 0
skipped = []
for path in sorted(glob.glob('/home/ubuntu/report_generos/templates/*.html')):
    base = os.path.basename(path)
    if 'backup' in base or '_live_backup' in base or 'before_aggregate' in base:
        continue
    html = open(path, 'r', encoding='utf-8').read()
    if 'fetch(\'/api/chat\'' not in html:
        continue
    if 'pollChatResult' in html:
        skipped.append(base + ' (sudah async)')
        continue
    if OLD not in html:
        skipped.append(base + ' (pola beda)')
        continue
    html = html.replace(OLD, NEW, 1)
    open(path, 'w', encoding='utf-8').write(html)
    patched += 1
    print('  patched:', base)

print(f'patched={patched}')
for s in skipped:
    print('  skip:', s)
