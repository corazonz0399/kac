#!/usr/bin/env python3
"""Kelola API key KAC (akses eksternal) — CLI.

Pakai:
    python3 manage_api_keys.py create "Website Atasan (read + studio create)"
    python3 manage_api_keys.py list
    python3 manage_api_keys.py revoke <id>
    python3 manage_api_keys.py log [jumlah]

Key mentah (plaintext) cuma muncul SEKALI waktu create — DB cuma nyimpen hash-nya.
"""
import sys

sys.path.insert(0, '/home/ubuntu/report_generos')

import database as db


def cmd_create(args):
    label = ' '.join(args) if args else 'Tanpa label'
    ok, raw, key_id = db.create_api_key(label, scope='read+studio_create')
    if not ok:
        print(f'GAGAL: {raw}')
        return 1
    print(f'✅ Key dibuat (id={key_id}, label={label})')
    print(f'\n   {raw}\n')
    print('   ⚠️  Simpan sekarang — plaintext-nya nggak bisa ditampilkan lagi.')
    return 0


def cmd_list(_args):
    keys = db.list_api_keys()
    if not keys:
        print('(belum ada key)')
        return 0
    print(f'{"ID":>3}  {"STATUS":<8} {"PREFIX":<13} {"PAKAI":>6}  {"LAST USED":<20} LABEL')
    for k in keys:
        status = 'AKTIF' if k.get('active') else 'REVOKED'
        last = str(k.get('last_used_at') or '-')[:19]
        print(f'{k["id"]:>3}  {status:<8} {k.get("key_prefix",""):<13} {k.get("use_count",0):>6}  {last:<20} {k.get("label","")}')
    return 0


def cmd_revoke(args):
    if not args or not args[0].isdigit():
        print('Pakai: revoke <id>')
        return 1
    ok, err = db.revoke_api_key(int(args[0]))
    print('✅ Key di-revoke' if ok else f'GAGAL: {err}')
    return 0 if ok else 1


def cmd_log(args):
    limit = int(args[0]) if args and args[0].isdigit() else 40
    rows = db.get_api_access_log(limit)
    if not rows:
        print('(log kosong)')
        return 0
    print(f'{"WAKTU":<20} {"STATUS":>6}  {"METHOD":<5} {"PATH":<34} {"IP":<16} NOTE')
    for r in rows:
        ts = str(r.get('created_at'))[:19]
        print(f'{ts:<20} {r.get("status",0):>6}  {r.get("method",""):<5} {str(r.get("path",""))[:34]:<34} {str(r.get("ip",""))[:16]:<16} {r.get("note","")}')
    return 0


COMMANDS = {'create': cmd_create, 'list': cmd_list, 'revoke': cmd_revoke, 'log': cmd_log}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
