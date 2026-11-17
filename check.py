"""Real availability checks for solowerkarmil.com.

Runs every 5 minutes on GitHub Actions (see .github/workflows/check.yml) or by hand:
    python check.py
Appends the result to data/status.json: per monitor a daily tally (90+ days) and
the raw points of the last 24 hours. Nothing here is invented: a day without
checks stays empty and the page shows it as "no data".
"""
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = "solowerkarmil.com"
BASE = f"https://{HOST}"
SLOW_MS = 2500          # answered, but slower than this -> degraded
SSL_WARN_DAYS = 14      # certificate expiring sooner -> degraded
KEEP_DAYS = 120
KEEP_RECENT_S = 26 * 3600
UA = "Mozilla/5.0 (compatible; StatusCheck/1.0)"

MONITORS = [
    {"id": "site", "checks": [{"url": BASE + "/", "expect": "Solower Karmil"}]},
    {"id": "en", "checks": [{"url": BASE + "/en/", "expect": "Solower Karmil"}]},
    {"id": "form", "checks": [
        # GET only: send.php answers with a redirect, no lead is ever sent.
        {"url": BASE + "/send.php", "ok_below": 500, "no_redirect": True},
        {"url": BASE + "/js/form.js"},
    ]},
    {"id": "contact", "checks": [{"url": BASE + "/contact/"}]},
    {"id": "ssl", "ssl": True},
]

DATA = Path(__file__).resolve().parent / "data" / "status.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(check):
    """Return (ok, ms). One HTTP request, 20 s timeout."""
    url = check["url"]
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + f"{sep}_sc={int(time.time())}", headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    opener = urllib.request.build_opener(_NoRedirect) if check.get("no_redirect") else urllib.request.build_opener()
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=20) as r:
            body = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code, body = e.code, b""
    except Exception:
        return False, int((time.perf_counter() - t0) * 1000)
    ms = int((time.perf_counter() - t0) * 1000)
    if "ok_below" in check:
        ok = code < check["ok_below"]
    else:
        ok = 200 <= code < 300
    if ok and check.get("expect"):
        ok = check["expect"].encode() in body
    return ok, ms


def run_http(mon):
    total, ok_all = 0, True
    for c in mon["checks"]:
        ok, ms = fetch(c)
        if not ok:                       # one retry before calling it down
            time.sleep(3)
            ok, ms = fetch(c)
        ok_all = ok_all and ok
        total += ms
    state = "down" if not ok_all else ("degraded" if total > SLOW_MS else "up")
    return state, total, {}


def run_ssl(_mon):
    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as s:
                cert = s.getpeercert()
        ms = int((time.perf_counter() - t0) * 1000)
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (exp - datetime.now(timezone.utc)).days
        state = "down" if days < 0 else ("degraded" if days < SSL_WARN_DAYS else "up")
        return state, ms, {"ssl_days": days, "ssl_expires": exp.strftime("%Y-%m-%d")}
    except Exception:
        return "down", int((time.perf_counter() - t0) * 1000), {"ssl_days": None}


def main():
    data = json.loads(DATA.read_text("utf-8")) if DATA.exists() else {"monitors": {}}
    now = int(time.time())
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    data.setdefault("since", day)
    idx = {"up": 0, "degraded": 1, "down": 2}
    for mon in MONITORS:
        state, ms, extra = run_ssl(mon) if mon.get("ssl") else run_http(mon)
        m = data["monitors"].setdefault(mon["id"], {"days": {}, "recent": []})
        tally = m["days"].setdefault(day, [0, 0, 0, 0, 0])   # up, degraded, down, ms sum, ms count
        tally[idx[state]] += 1
        if state != "down":
            tally[3] += ms
            tally[4] += 1
        m["recent"].append([now, ms, state[0]])
        m["recent"] = [p for p in m["recent"] if p[0] >= now - KEEP_RECENT_S]
        m["days"] = dict(sorted(m["days"].items())[-KEEP_DAYS:])
        m["state"] = state
        m.update(extra)
        print(f"{mon['id']:8} {state:9} {ms} ms {extra or ''}")
    data["updated"] = now
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(data, separators=(",", ":")), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
