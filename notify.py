"""固定支出 — 扣款提醒：每天定时检查一次，通过 Telegram / Bark 推送。

规则与前端 public/index.html 保持一致（扣款日、试用期、到期、暂停、自动扣款）。
"""
import calendar
import datetime as dt
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("GZ_TZ", "Asia/Shanghai"))
TG_TOKEN = os.environ.get("GZ_TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("GZ_TG_CHAT", "").strip()
BARK_BASE = os.environ.get("GZ_BARK_BASE", "").strip().rstrip("/")
BARK_KEY = os.environ.get("GZ_BARK_KEY", "").strip()

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


def channels():
    return {"telegram": bool(TG_TOKEN and TG_CHAT), "bark": bool(BARK_BASE and BARK_KEY)}


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


def send_telegram(title, body):
    chat = int(TG_CHAT) if TG_CHAT.lstrip("-").isdigit() else TG_CHAT
    r = _post_json(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                   {"chat_id": chat, "text": f"{title}\n\n{body}", "link_preview_options": {"is_disabled": True}}, 15)
    return bool(r.get("ok"))


def send_bark(title, body):
    r = _post_json(f"{BARK_BASE}/push", {"device_key": BARK_KEY, "title": title, "body": body, "group": "固定支出"}, 10)
    return r.get("code") == 200


def deliver(title, body, want):
    """want: {"telegram": bool, "bark": bool}。返回每个渠道的结果文字。"""
    conf, out = channels(), {}
    for name, fn in (("telegram", send_telegram), ("bark", send_bark)):
        if not want.get(name):
            out[name] = "未开启"
        elif not conf[name]:
            out[name] = "未配置"
        else:
            try:
                out[name] = "成功" if fn(title, body) else "失败"
            except Exception as e:  # 不把 token 等信息带进错误文字
                out[name] = "失败：" + _err(e)
    return out


def check():
    """不发消息，只检查渠道是否可用（Telegram getMe / Bark ping）。"""
    res = {}
    if channels()["telegram"]:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=10) as r:
                me = json.load(r)
            res["telegram"] = "可用" if me.get("ok") else "token 无效"
            res["telegramBot"] = "@" + (me.get("result") or {}).get("username", "")
            q = urllib.parse.urlencode({"chat_id": TG_CHAT})
            with urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/getChat?{q}", timeout=10) as r:
                chat = (json.load(r).get("result") or {})
            res["telegramTarget"] = "私聊" if chat.get("type") == "private" else (chat.get("title") or "群组")
        except Exception as e:
            res["telegram"] = "失败：" + _err(e)
    if channels()["bark"]:
        try:
            with urllib.request.urlopen(f"{BARK_BASE}/ping", timeout=5) as r:
                res["bark"] = "可用" if json.load(r).get("code") == 200 else "异常"
        except Exception as e:
            res["bark"] = "失败：" + _err(e)
    return res


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
