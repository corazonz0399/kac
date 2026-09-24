#!/usr/bin/env python3
"""Patch telegram_bot_token di config server + lokal dengan token valid dari .env."""
import json, os

PREFIX = 'TELEGRAM_BOT_' + 'TOKEN='

tok_env = ''
env_path = '/home/ubuntu/.hermes/.env'
for line in open(env_path):
    if line.startswith(PREFIX):
        tok_env = line.split('=', 1)[1].strip().strip('"').strip("'")
        break
print('valid token len:', len(tok_env), tok_env[:15] + '...')

# Patch server config (sudah di-download sebagai config.server.json)
cfg = json.load(open('config.server.json'))
old = cfg.get('telegram_bot_token', '')
cfg['telegram_bot_token'] = tok_env
json.dump(cfg, open('config.server.json', 'w'), indent=2)
print('patched server config: old len', len(old), '-> new len', len(cfg['telegram_bot_token']))

# Patch lokal config.json juga
cfg_l = json.load(open('config.json'))
cfg_l['telegram_bot_token'] = tok_env
json.dump(cfg_l, open('config.json', 'w'), indent=2)
print('patched local config.json, len', len(cfg_l['telegram_bot_token']))
