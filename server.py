#!/usr/bin/env python3
"""固定支出 — 后端：页面 + 密码登录 + 数据保存到 data/expenses.json

改密码：  python3 server.py --hash   （输入新密码，把输出替换 config/app.env 里的 GZ_PASSWORD_HASH，然后
          systemctl restart guding-zhichu）
"""
import argparse
import datetime
import getpass
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "public"
DATA = BASE / "data" / "expenses.json"
HISTORY = BASE / "data" / "history"
HISTORY_KEEP = 30
MAX_BODY = 1_000_000
COOKIE = "gz_session"
SESSION_TTL = 30 * 24 * 3600
FAIL_WINDOW, FAIL_MAX = 900, 6

PASSWORD_HASH = os.environ.get("GZ_PASSWORD_HASH", "")
SECRET = os.environ.get("GZ_SECRET", "").encode()

_fails: dict = {}
_lock = threading.Lock()
_data_lock = threading.Lock()

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def _read_data():
    if not DATA.exists():
        return None
    return json.loads(DATA.read_text("utf-8"))


def _rev_of(obj):
    rev = obj.get("_rev") if isinstance(obj, dict) else 0
    return rev if isinstance(rev, int) and not isinstance(rev, bool) else 0


def _snapshot():
    """每天第一次写入前，把旧数据存一份到 data/history/，保留最近 30 天。"""
    if not DATA.exists():
        return
    try:
        HISTORY.mkdir(parents=True, exist_ok=True)
        dst = HISTORY / f"expenses-{datetime.date.today():%Y%m%d}.json"
        if not dst.exists():
            dst.write_bytes(DATA.read_bytes())
        for old in sorted(HISTORY.glob("expenses-*.json"))[:-HISTORY_KEEP]:
            old.unlink()
    except Exception:
        pass


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ---- 汇率：1 单位外币 = 多少人民币，缓存 6 小时 ----
CURRENCIES = {"CNY", "USD", "EUR", "HKD", "GBP", "JPY", "SGD", "AUD", "CAD", "KRW", "TWD", "CHF"}
RATES_FILE = BASE / "data" / "rates.json"
RATES_TTL = 6 * 3600
_rates_lock = threading.Lock()
_rates_cache = None


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "guding-zhichu/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def _fetch_rates():
    try:
        j = _get_json("https://open.er-api.com/v6/latest/CNY")
        if j.get("result") != "success":
            raise ValueError("bad result")
        raw, updated, source = j["rates"], int(j.get("time_last_update_unix") or time.time()), "open.er-api.com"
    except Exception:
        j = _get_json("https://api.frankfurter.app/latest?from=CNY")
        raw, source = j["rates"], "frankfurter.app"
        updated = int(time.time())
    rates = {k: round(1 / float(v), 8) for k, v in raw.items() if k in CURRENCIES and float(v) > 0}
    rates["CNY"] = 1.0
    if len(rates) < 3:
        raise ValueError("too few rates")
    return {"rates": rates, "updated": updated * 1000, "fetched": time.time(), "source": source}


def get_rates():
    global _rates_cache
    with _rates_lock:
        if _rates_cache is None and RATES_FILE.exists():
            try:
                _rates_cache = json.loads(RATES_FILE.read_text("utf-8"))
            except Exception:
                _rates_cache = None
        if _rates_cache and time.time() - _rates_cache.get("fetched", 0) < RATES_TTL:
            return {**_rates_cache, "stale": False}
        try:
            fresh = _fetch_rates()
        except Exception:
            return {**_rates_cache, "stale": True} if _rates_cache else None
        _rates_cache = fresh
        try:
            _write_json(RATES_FILE, fresh)
        except Exception:
            pass
        return {**fresh, "stale": False}


def hash_password(pw: str, iters: int = 600_000) -> str:
    salt = secrets.token_hex(16)
    d = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iters).hex()
    return f"pbkdf2_sha256${iters}${salt}${d}"


def verify_password(pw: str) -> bool:
    try:
        algo, iters, salt, digest = PASSWORD_HASH.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    d = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters)).hex()
    return hmac.compare_digest(d, digest)


def _sign(exp: int) -> str:
    return hmac.new(SECRET, f"gz:{exp}".encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{_sign(exp)}"


def valid_token(tok: str) -> bool:
    try:
        exp_s, sig = tok.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(exp))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(PUBLIC), **k)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        if not getattr(self, "_is_json", False):
            # 页面每次都向服务器确认是否更新，避免更新后还看到旧版本
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ---- helpers ----
    def _route(self):
        return self.path.split("?", 1)[0]

    def _ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v and valid_token(v):
                return True
        return False

    def _cookie(self, value, max_age):
        prefix = (self.headers.get("X-Forwarded-Prefix") or "").rstrip("/")
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{COOKIE}={value}; Path={prefix}/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _json(self, code, obj, cookie=None):
        self._is_json = True
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return None, 415
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return None, 413
        try:
            return json.loads(self.rfile.read(n)), 200
        except Exception:
            return None, 400

    # ---- routes ----
    def do_GET(self):
        r = self._route()
        if r == "/api/session":
            return self._json(200, {"authed": self._authed()})
        if r == "/api/data":
            if not self._authed():
                return self._json(401, {"error": "请先登录"})
            try:
                with _data_lock:
                    obj = _read_data()
            except Exception:
                return self._json(500, {"error": "数据文件损坏"})
            return self._json(200, obj)
        if r == "/api/rates":
            if not self._authed():
                return self._json(401, {"error": "请先登录"})
            rates = get_rates()
            if not rates:
                return self._json(503, {"error": "汇率暂不可用"})
            return self._json(200, rates)
        if r.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        r = self._route()
        if r == "/api/login":
            return self._login()
        if r == "/api/logout":
            return self._json(200, {"ok": True}, cookie=self._cookie("", 0))
        return self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self._route() != "/api/data":
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return self._json(401, {"error": "请先登录"})
        obj, code = self._read_json()
        if code != 200:
            return self._json(code, {"error": "请求不合法"})
        if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
            return self._json(400, {"error": "格式不对"})
        # 乐观锁：客户端必须基于服务器当前版本修改，防止多台设备互相覆盖
        with _data_lock:
            try:
                cur_rev = _rev_of(_read_data())
            except Exception:
                return self._json(500, {"error": "数据文件损坏"})
            if "_rev" not in obj or _rev_of(obj) != cur_rev:
                return self._json(409, {"error": "数据已在其他地方更新", "rev": cur_rev})
            obj["_rev"] = cur_rev + 1
            _snapshot()
            _write_json(DATA, obj)
        return self._json(200, {"ok": True, "rev": cur_rev + 1})

    def _login(self):
        ip, now = self._ip(), time.time()
        with _lock:
            if len(_fails) > 5000:
                _fails.clear()
            recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
            _fails[ip] = recent
            if len(recent) >= FAIL_MAX:
                return self._json(429, {"error": "尝试次数太多，请 15 分钟后再试"})
        obj, code = self._read_json()
        if code != 200:
            return self._json(code, {"error": "请求不合法"})
        pw = obj.get("password") if isinstance(obj, dict) else None
        if isinstance(pw, str) and 0 < len(pw) <= 200 and PASSWORD_HASH and verify_password(pw):
            with _lock:
                _fails.pop(ip, None)
            return self._json(200, {"ok": True}, cookie=self._cookie(make_token(), SESSION_TTL))
        with _lock:
            _fails.setdefault(ip, []).append(now)
        time.sleep(0.6)
        return self._json(401, {"error": "密码不对"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8130)
    p.add_argument("--hash", action="store_true", help="输入密码，输出 GZ_PASSWORD_HASH")
    a = p.parse_args()
    if a.hash:
        pw = getpass.getpass("新密码: ")
        if pw != getpass.getpass("再输一次: ") or not pw:
            raise SystemExit("两次不一致或为空")
        print("GZ_PASSWORD_HASH=" + hash_password(pw))
        return
    if not PASSWORD_HASH or len(SECRET) < 32:
        raise SystemExit("缺少 GZ_PASSWORD_HASH / GZ_SECRET（见 config/app.env）")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
