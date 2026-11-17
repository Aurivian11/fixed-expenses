"""固定支出 — 扣款提醒：每天定时检查一次，通过 Telegram / Bark 推送。

规则与前端 public/index.html 保持一致（扣款日、试用期、到期、暂停、自动扣款）。
"""
import calendar
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("GZ_TZ", "Asia/Shanghai"))
# 推送渠道：页面里保存的设置（CONFIG_FILE）优先，没有则用环境变量（config/app.env）作为默认值
CONFIG_FILE = None  # 由 server.py 设置为 data/notify-config.json
TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,200}$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,64})$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_cfg_lock = threading.Lock()

STEP = {"monthly": 1, "quarterly": 3, "yearly": 12}
SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€", "HKD": "HK$", "GBP": "£", "JPY": "JP¥", "SGD": "S$",
          "AUD": "A$", "CAD": "C$", "KRW": "₩", "TWD": "NT$", "CHF": "CHF "}
FALLBACK = {"CNY": 1, "USD": 7.1, "EUR": 8.2, "HKD": 0.91, "GBP": 9.3, "JPY": 0.047, "SGD": 5.4,
            "AUD": 4.6, "CAD": 5.1, "KRW": 0.0051, "TWD": 0.22, "CHF": 8.8}
DEFAULTS = {"telegram": True, "bark": True, "hour": 9, "lead": 1, "autoLead": 3, "bigLead": 7,
            "monthly": True, "late": True, "trial": True}
LIMITS = {"hour": (0, 23), "lead": (0, 15), "autoLead": (0, 15), "bigLead": (0, 60)}
SECTIONS = [("today", "📌 今天要付"), ("late", "⚠️ 已过扣款日还没付"), ("soon", "⏰ 即将扣款"),
            ("big", "🗓 年付 / 季付"), ("auto", "🔁 即将自动续费（不需要的话记得提前取消）"),
            ("trial", "🎁 试用即将结束"), ("end", "📄 即将到期")]


def _stored():
    try:
        d = json.loads(CONFIG_FILE.read_text("utf-8")) if CONFIG_FILE and CONFIG_FILE.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def conf():
    c = {"tg_token": os.environ.get("GZ_TG_TOKEN", "").strip(), "tg_chat": os.environ.get("GZ_TG_CHAT", "").strip(),
         "bark_base": os.environ.get("GZ_BARK_BASE", "").strip().rstrip("/"), "bark_key": os.environ.get("GZ_BARK_KEY", "").strip(),
         "tg_source": "server", "bark_source": "server"}
    st = _stored()
    t, b = st.get("telegram"), st.get("bark")
    if isinstance(t, dict):
        c.update(tg_token=str(t.get("token") or ""), tg_chat=str(t.get("chat") or ""), tg_source="page")
    if isinstance(b, dict):
        c.update(bark_base=str(b.get("base") or "").rstrip("/"), bark_key=str(b.get("key") or ""), bark_source="page")
    return c


def channels():
    c = conf()
    return {"telegram": bool(c["tg_token"] and c["tg_chat"]), "bark": bool(c["bark_base"] and c["bark_key"])}


def settings_of(state):
    s = (state or {}).get("settings") or {}
    n = s.get("notify") or {}
    out = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        if k not in n:
            continue
        if isinstance(v, bool):
            out[k] = bool(n[k])
        else:
            try:
                lo, hi = LIMITS[k]
                out[k] = min(hi, max(lo, int(n[k])))
            except (TypeError, ValueError):
                pass
    try:
        out["fxFee"] = min(10.0, max(0.0, float(s.get("fxFee") or 0)))
    except (TypeError, ValueError):
        out["fxFee"] = 0.0
    return out


# ---------- 扣款规则（与前端一致） ----------
def _date(s):
    try:
        return dt.date.fromisoformat(s) if isinstance(s, str) and len(s) == 10 else None
    except ValueError:
        return None


def _key(y, m):
    return f"{y:04d}-{m:02d}"


def _int(v, dft):
    try:
        return int(v)
    except (TypeError, ValueError):
        return dft


def _due_in(it, y, m):
    step = STEP.get(it.get("cycle"), 1)
    return step == 1 or (m - _int(it.get("dueMonth"), 1)) % step == 0


def _due_day(it, y, m):
    return min(max(_int(it.get("dueDay"), 1), 1), calendar.monthrange(y, m)[1])


def _in_pause(it, d):
    for p in it.get("pauses") or []:
        if isinstance(p, dict):
            f, t = _date(p.get("from")), _date(p.get("to"))
            if f and f <= d and (t is None or d < t):
                return True
    return False


def paused_now(it):
    return any(isinstance(p, dict) and not p.get("to") for p in it.get("pauses") or [])


def occurrence(it, y, m):
    """某月的实际扣款日；不扣则返回 None。"""
    if not _due_in(it, y, m):
        return None
    added = it.get("added") or ""
    if added and _key(y, m) < added:
        return None
    d = dt.date(y, m, _due_day(it, y, m))
    tr, en = _date(it.get("trialEnd")), _date(it.get("endDate"))
    if (tr and d < tr) or (en and d > en) or _in_pause(it, d):
        return None
    return d


def is_paid(state, it, key, today):
    iid = it.get("id")
    if iid in ((state.get("paid") or {}).get(key) or []):
        return True
    if not it.get("auto") or iid in ((state.get("unpaid") or {}).get(key) or []):
        return False
    y, m = map(int, key.split("-"))
    occ = occurrence(it, y, m)
    return bool(occ and occ <= today)


# ---------- 文案 ----------
def _money(amount, cur):
    dec = 0 if cur in ("JPY", "KRW") or float(amount).is_integer() else 2
    return f"{SYMBOL.get(cur, '¥')}{amount:,.{dec}f}"


def _cny(amount, cur, rates, fee):
    if cur == "CNY":
        return amount
    return amount * (rates.get(cur) or FALLBACK.get(cur) or 1) * (1 + fee / 100)


def _label(it, rates, fee):
    amt, cur = float(it.get("amount") or 0), it.get("currency") or "CNY"
    s = f"{it.get('name', '')} {_money(amt, cur)}"
    if cur != "CNY":
        s += f"（≈¥{_cny(amt, cur, rates, fee):,.0f}）"
    return s


def _when(days):
    return "今天" if days == 0 else "明天" if days == 1 else f"{days} 天后"


def build(state, today, sent, rates):
    """计算今天要推送的内容。返回 (标题, 正文, 去重键列表)；正文为空表示无需推送。"""
    s, state = settings_of(state), state or {}
    fee = s["fxFee"]
    items = [it for it in state.get("items") or [] if isinstance(it, dict) and not paused_now(it)]
    groups = {g: [] for g, _ in SECTIONS}
    keys = []

    def add(group, key, text, when):
        if key not in sent:
            groups[group].append((when, text))
            keys.append(key)

    horizon = max(s["lead"], s["autoLead"], s["bigLead"])
    for it in items:
        iid, big = it.get("id"), STEP.get(it.get("cycle"), 1) > 1
        for i in range(3):
            y, m = today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1
            occ = occurrence(it, y, m)
            if not occ:
                continue
            days = (occ - today).days
            if days < 0 or days > horizon or iid in ((state.get("paid") or {}).get(_key(y, m)) or []):
                continue
            tag = f"{_when(days)} {occ.month}/{occ.day}："
            if it.get("auto"):
                if 0 < days <= max(s["autoLead"], s["bigLead"] if big else 0):
                    add("auto", f"auto:{iid}:{occ}", tag + _label(it, rates, fee), occ)
            elif days == 0:
                add("today", f"today:{iid}:{occ}", _label(it, rates, fee), occ)
            elif big and days <= s["bigLead"]:
                add("big", f"big:{iid}:{occ}", tag + _label(it, rates, fee) + ("（年付）" if it.get("cycle") == "yearly" else "（季付）"), occ)
            elif not big and days <= s["lead"]:
                add("soon", f"soon:{iid}:{occ}", tag + _label(it, rates, fee), occ)
        if s["late"] and not it.get("auto"):
            occ = occurrence(it, today.year, today.month)
            if occ and 1 <= (today - occ).days <= 3 and not is_paid(state, it, _key(today.year, today.month), today):
                add("late", f"late:{iid}:{occ}", f"{_label(it, rates, fee)}（{occ.month}/{occ.day}，已过 {(today - occ).days} 天）", occ)
        if s["trial"]:
            tr = _date(it.get("trialEnd"))
            if tr and 0 <= (tr - today).days <= 3:
                add("trial", f"trial:{iid}:{tr}", f"{it.get('name', '')}：试用 {tr.month}/{tr.day} 结束（{_when((tr - today).days)}），之后开始扣费 {_money(float(it.get('amount') or 0), it.get('currency') or 'CNY')}", tr)
            en = _date(it.get("endDate"))
            if en and 0 <= (en - today).days <= 7:
                add("end", f"end:{iid}:{en}", f"{it.get('name', '')}：{en.month}/{en.day} 到期（{_when((en - today).days)}）", en)

    parts = []
    skey = f"summary:{_key(today.year, today.month)}"
    if s["monthly"] and today.day == 1 and skey not in sent:
        due = [(it, occurrence(it, today.year, today.month)) for it in items]
        due = [(it, d) for it, d in due if d]
        if due:
            total = sum(_cny(float(it.get("amount") or 0), it.get("currency") or "CNY", rates, fee) for it, _ in due)
            autos = sum(1 for it, _ in due if it.get("auto"))
            parts.append(f"📊 {today.month} 月共 {len(due)} 笔固定支出，合计约 ¥{total:,.0f}" + (f"，其中 {autos} 笔自动扣款" if autos else ""))
            keys.append(skey)
    for g, title in SECTIONS:
        rows = sorted(groups[g], key=lambda r: r[0])
        if rows:
            parts.append(title + "\n" + "\n".join("• " + t for _, t in rows))
    return f"固定支出提醒 · {today.month}月{today.day}日", "\n\n".join(parts), keys


# ---------- 发送 ----------
def _post_json(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "guding-zhichu/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(65536) or b"{}")


def _err(e):
    code = getattr(e, "code", None)
    return f"HTTP {code}" if code else type(e).__name__


def _tg(token, method, params=None, timeout=10):
    """调用 Telegram API；出错时返回 {"ok": False, "description": ...}，不会把 token 带进错误文字。"""
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            j = json.load(e)
            return j if isinstance(j, dict) else {"ok": False, "description": f"HTTP {e.code}"}
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}


def send_telegram(title, body):
    c = conf()
    chat = int(c["tg_chat"]) if c["tg_chat"].lstrip("-").isdigit() else c["tg_chat"]
    r = _post_json(f"https://api.telegram.org/bot{c['tg_token']}/sendMessage",
                   {"chat_id": chat, "text": f"{title}\n\n{body}", "link_preview_options": {"is_disabled": True}}, 15)
    return bool(r.get("ok"))


def send_bark(title, body):
    c = conf()
    r = _post_json(f"{c['bark_base']}/push", {"device_key": c["bark_key"], "title": title, "body": body, "group": "固定支出"}, 10)
    return r.get("code") == 200


def deliver(title, body, want):
    """want: {"telegram": bool, "bark": bool}。返回每个渠道的结果文字。"""
    conf_ok, out = channels(), {}
    for name, fn in (("telegram", send_telegram), ("bark", send_bark)):
        if not want.get(name):
            out[name] = "未开启"
        elif not conf_ok[name]:
            out[name] = "未配置"
        else:
            try:
                out[name] = "成功" if fn(title, body) else "失败"
            except Exception as e:
                out[name] = "失败：" + _err(e)
    return out


def check():
    """不发消息，只检查 Bark 服务器能否连上。"""
    c, res = conf(), {}
    if c["bark_base"] and c["bark_key"]:
        try:
            with urllib.request.urlopen(f"{c['bark_base']}/ping", timeout=5) as r:
                res["bark"] = "可用" if json.load(r).get("code") == 200 else "异常"
        except Exception as e:
            res["bark"] = "失败：" + _err(e)
    return res


# ---------- 推送渠道配置（页面可改） ----------
def _tail(s):
    return "…" + s[-4:] if s else ""


def public_conf():
    """给页面看的配置：不返回 token / key 本身，只给末尾 4 位。"""
    c = conf()
    tg = {"tokenSet": bool(c["tg_token"]), "tokenTail": _tail(c["tg_token"]), "chat": c["tg_chat"],
          "source": c["tg_source"], "bot": "", "tokenValid": False, "chatType": "", "chatName": ""}
    if c["tg_token"]:
        me = _tg(c["tg_token"], "getMe")
        tg["tokenValid"] = bool(me.get("ok"))
        if me.get("ok"):
            tg["bot"] = "@" + str((me.get("result") or {}).get("username", ""))
            if c["tg_chat"]:
                ch = _tg(c["tg_token"], "getChat", {"chat_id": c["tg_chat"]})
                r = ch.get("result") or {}
                tg["chatType"] = ("私聊" if r.get("type") == "private" else "群组") if ch.get("ok") else "无法访问"
                tg["chatName"] = " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or r.get("title") or ""
    bark = {"base": c["bark_base"], "keySet": bool(c["bark_key"]), "keyTail": _tail(c["bark_key"]), "source": c["bark_source"]}
    return {"telegram": tg, "bark": bark}


def update_conf(obj):
    """保存页面提交的推送配置。token / key 留空表示保持不变；clear 表示清空。成功返回 None，否则返回错误文字。"""
    if not isinstance(obj, dict):
        return "请求不合法"
    with _cfg_lock:
        c, st = conf(), _stored()
        t = obj.get("telegram")
        if isinstance(t, dict):
            if t.get("clear"):
                st["telegram"] = {"token": "", "chat": ""}
            else:
                token = str(t.get("token") or "").strip() or c["tg_token"]
                chat = str(t.get("chat") or "").strip()
                if not TOKEN_RE.match(token):
                    return "Bot Token 格式不对（形如 123456789:ABC…，在 @BotFather 里获取）"
                if not CHAT_RE.match(chat):
                    return "Chat ID 格式不对（一串数字，可以点「自动获取」）"
                me = _tg(token, "getMe")
                if not me.get("ok"):
                    return "Bot Token 无效：" + str(me.get("description", ""))[:80]
                ch = _tg(token, "getChat", {"chat_id": chat})
                if not ch.get("ok"):
                    return "这个机器人还不能给该 Chat ID 发消息：请先在 Telegram 里打开你的机器人，点「开始」或发一条 /start"
                st["telegram"] = {"token": token, "chat": chat}
        b = obj.get("bark")
        if isinstance(b, dict):
            if b.get("clear"):
                st["bark"] = {"base": "", "key": ""}
            else:
                base = str(b.get("base") or "").strip().rstrip("/") or "https://api.day.app"
                key = str(b.get("key") or "").strip() or c["bark_key"]
                u = urllib.parse.urlsplit(base)
                if u.scheme not in ("http", "https") or not u.netloc or u.query or u.fragment:
                    return "Bark 服务器地址不对（如 https://api.day.app）"
                if not KEY_RE.match(key):
                    return "Bark 设备 Key 格式不对（在 Bark App 首页复制）"
                st["bark"] = {"base": base, "key": key}
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    return None


def detect_chats(token=None):
    """读取机器人最近收到的消息，找出给它发过消息的聊天（不确认、不消费更新，也不改机器人的设置）。"""
    typed = str(token or "").strip()
    token = typed or conf()["tg_token"]
    if not TOKEN_RE.match(token):
        return {"error": "Bot Token 格式不对（形如 123456789:ABC…）" if typed else "请先填写 Bot Token"}
    r = _tg(token, "getUpdates", {"timeout": 0, "limit": 100})
    if not r.get("ok"):
        d = str(r.get("description", ""))
        if "Conflict" in d or "webhook" in d:
            return {"error": "这个机器人正被别的程序接收消息（或设置了 webhook），无法自动获取，请手动填写 Chat ID"}
        return {"error": "获取失败：" + d[:80]}
    chats = {}
    for u in r.get("result") or []:
        for k in ("message", "edited_message", "channel_post", "my_chat_member"):
            ch = (u.get(k) or {}).get("chat") or {}
            if ch.get("id"):
                name = " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("title") or ch.get("username") or ""
                chats[str(ch["id"])] = {"id": str(ch["id"]), "type": "私聊" if ch.get("type") == "private" else "群组", "name": name}
    if not chats:
        return {"error": "还没收到消息：请先在 Telegram 里给你的机器人发一条消息（比如 /start），再点一次"}
    return {"chats": list(chats.values())[::-1]}


# ---------- 定时任务 ----------
class Scheduler(threading.Thread):
    RETRY = 900

    def __init__(self, read_state, get_rates, meta_file):
        super().__init__(daemon=True, name="notify")
        self.read_state, self.get_rates, self.meta_file = read_state, get_rates, meta_file
        self.lock = threading.Lock()

    def _load(self):
        try:
            m = json.loads(self.meta_file.read_text("utf-8"))
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}

    def _save(self, meta):
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.meta_file)

    def _rates(self):
        try:
            return (self.get_rates() or {}).get("rates") or {}
        except Exception:
            return {}

    def status(self):
        now = dt.datetime.now(TZ)
        with self.lock:
            meta = self._load()
        state = self.read_state() or {}
        title, body, _ = build(state, now.date(), meta.get("sent") or {}, self._rates())
        return {"channels": channels(), "tz": str(TZ), "now": now.strftime("%Y-%m-%d %H:%M"),
                "lastRun": meta.get("lastRun"), "lastResult": meta.get("lastResult"),
                "lastDate": meta.get("lastDate"), "preview": {"title": title, "body": body}}

    def run_once(self, now=None, force=False):
        now = now or dt.datetime.now(TZ)
        today = now.date()
        with self.lock:
            meta = self._load()
            state = self.read_state()
            if not state:
                return None
            s = settings_of(state)
            if not force:
                if now.hour < s["hour"] or meta.get("lastDate") == today.isoformat():
                    return None
                if time.time() - meta.get("lastAttempt", 0) < self.RETRY:
                    return None
            sent = meta.get("sent") or {}
            title, body, keys = build(state, today, sent, self._rates())
            meta["lastAttempt"] = time.time()
            meta["lastRun"] = now.strftime("%Y-%m-%d %H:%M")
            want = {"telegram": s["telegram"], "bark": s["bark"]}
            if not body:
                meta.update(lastDate=today.isoformat(), lastResult={"note": "今天没有需要提醒的"})
            elif not any(want[c] and channels()[c] for c in want):
                meta.update(lastDate=today.isoformat(), lastResult={"note": "没有可用的推送渠道"})
            else:
                res = deliver(title, body, want)
                meta["lastResult"] = res
                if any(v == "成功" for v in res.values()):
                    for k in keys:
                        sent[k] = today.isoformat()
                    meta["lastDate"] = today.isoformat()
            cutoff = (today - dt.timedelta(days=120)).isoformat()
            meta["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
            self._save(meta)
            return meta.get("lastResult")

    def run(self):
        time.sleep(15)
        while True:
            try:
                self.run_once()
            except Exception:
                pass
            time.sleep(60)
