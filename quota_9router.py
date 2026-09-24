# -*- coding: utf-8 -*-
"""Sync kuota/credit provider 9router -> KAC.
Ambil data /api/usage/{connectionId} untuk SEMUA koneksi provider (read-only,
tidak pernah menyentuh credential). Dipakai halaman Kuota 9Router di KAC.
"""
import hashlib, json, os, shutil, sqlite3, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = "http://127.0.0.1:20128"
SNAP = "/tmp/9r_kac_snap"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Prefix model alias -> nama provider di DB (untuk display)
ALIAS = {"ag": "antigravity", "cbai": "codebuddy-intl", "cl": "cline",
         "cx": "codex", "ds": "deepseek", "gemini": "gemini", "openai": "openai"}


def _snapshot():
    shutil.rmtree(SNAP, ignore_errors=True)
    os.makedirs(SNAP, exist_ok=True)
    for f in os.listdir("/home/ubuntu/.9router/db"):
        if f.startswith("data.sqlite"):
            shutil.copy2(f"/home/ubuntu/.9router/db/{f}", f"{SNAP}/{f}")


def _token():
    mid = open('/home/ubuntu/.9router/machine-id').read().strip()
    cli = open('/home/ubuntu/.9router/auth/cli-secret').read().strip()
    return hashlib.sha256((mid + "9r-cli-auth" + cli).encode()).hexdigest()[:16]


def _usage(cid, token):
    req = urllib.request.Request(f"{BASE}/api/usage/{cid}",
                                 headers={"x-9r-cli-token": token})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_err": f"HTTP {e.code}"}
    except Exception as e:
        return {"_err": str(e)[:60]}


def sync():
    _snapshot()
    token = _token()
    db = sqlite3.connect(f"{SNAP}/data.sqlite")
    rows = db.execute("SELECT id, provider, name, isActive, authType FROM providerConnections").fetchall()
    db.close()

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        usage_map = dict(zip([r[0] for r in rows], ex.map(lambda r: _usage(r[0], token), rows)))

    providers = []
    for cid, provider, name, isActive, authType in rows:
        u = usage_map[cid]
        q = u.get("quotas", {}) if isinstance(u, dict) else {}
        quotas = []
        warn = False
        for key, v in q.items():
            if not isinstance(v, dict) or "total" not in v:
                continue
            used = float(v.get("used", 0))
            total = float(v.get("total", 0))
            remaining = v.get("remaining")
            if remaining is None and "remainingPercentage" in v:
                remaining = round(total * float(v["remainingPercentage"]) / 100, 2)
            if remaining is None:
                remaining = max(0, total - used)
            row = {
                "key": key, "used": used, "total": total,
                "remaining": remaining,
                "resetAt": v.get("resetAt"),
                "isCreditBalance": bool(v.get("isCreditBalance")),
                "unlimited": bool(v.get("unlimited")),
            }
            quotas.append(row)
            if total and used >= total and not v.get("unlimited"):
                warn = True
        record = {
            "provider": provider,
            "prefix": next((p for p, pr in ALIAS.items() if pr == provider), None),
            "name": name,
            "authType": authType,
            "isActive": bool(isActive),
            "plan": u.get("plan") if isinstance(u, dict) else None,
            "quotas": quotas,
            "warn": warn,
        }
        if isinstance(u, dict) and u.get("limitReached"):
            record["warn"] = True
        if isinstance(u, dict) and "_err" in u:
            record["err"] = u["_err"]
            record["plan"] = None
        providers.append(record)

    out = {"ok": True,
           "synced_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "providers": providers}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "quota_9router.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return out


if __name__ == "__main__":
    r = sync()
    print(json.dumps(r, indent=1, ensure_ascii=False)[:3000])