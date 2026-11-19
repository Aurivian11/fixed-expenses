"""固定支出 — 扣款提醒与 Telegram 机器人。

- Scheduler：每天定时检查一次，通过 Telegram / Bark 推送扣款提醒；超出预算提醒；1 月初推送年度报告
- BotPoller：接收 Telegram 按钮点击（一键标记已付 / 撤销）和 /start、/month 命令

扣款规则与前端 public/index.html 保持一致：扣款日、开始月份、试用期、到期、暂停、自动扣款、价格历史、
按扣款当天汇率折算。
"""
import bisect
import calendar
import datetime as dt
import hashlib
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
CONFIG_FILE = None   # 由 server.py 设置为 data/notify-config.json
POLLER = None        # 由 server.py 设置为正在运行的 BotPoller
TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,200}$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,64})$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_cfg_lock = threading.Lock()

STEP = {"monthly": 1, "quarterly": 3, "yearly": 12}
UNIT = {"monthly": "月", "quarterly": "季", "yearly": "年"}
SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€", "HKD": "HK$", "GBP": "£", "JPY": "JP¥", "SGD": "S$",
          "AUD": "A$", "CAD": "C$", "KRW": "₩", "TWD": "NT$", "CHF": "CHF "}
FALLBACK = {"CNY": 1, "USD": 7.1, "EUR": 8.2, "HKD": 0.91, "GBP": 9.3, "JPY": 0.047, "SGD": 5.4,
            "AUD": 4.6, "CAD": 5.1, "KRW": 0.0051, "TWD": 0.22, "CHF": 8.8}
DEFAULTS = {"telegram": True, "bark": True, "hour": 9, "lead": 1, "autoLead": 3, "bigLead": 7,
            "monthly": True, "late": True, "trial": True, "budget": True, "annual": True}
LIMITS = {"hour": (0, 23), "lead": (0, 15), "autoLead": (0, 15), "bigLead": (0, 60)}
SECTIONS = [("today", "📌 今天要付"), ("late", "⚠️ 已过扣款日还没付"), ("soon", "⏰ 即将扣款"),
            ("big", "🗓 年付 / 季付"), ("auto", "🔁 即将自动续费（不需要的话记得提前取消）"),
            ("trial", "🎁 试用即将结束"), ("end", "📄 即将到期")]


# ---------- 推送渠道配置：页面里保存的优先，没有则用环境变量 ----------
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


def _num(v, hi):
    try:
        return min(hi, max(0.0, float(v or 0)))
    except (TypeError, ValueError):
        return 0.0


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
    out["fxFee"] = _num(s.get("fxFee"), 10)
    out["income"] = _num(s.get("income"), 1e9)
    out["budgetAmount"] = _num(s.get("budget"), 1e9)
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


def _items(state):
    return [it for it in (state or {}).get("items") or [] if isinstance(it, dict)]


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


def life(it, today):
    if paused_now(it):
        return "paused"
    en, tr = _date(it.get("endDate")), _date(it.get("trialEnd"))
    if en and en < today:
        return "ended"
    if tr and today < tr:
        return "trial"
    return "active"


def is_paid(state, it, key, today):
    iid = it.get("id")
    if iid in ((state.get("paid") or {}).get(key) or []):
        return True
    if not it.get("auto") or iid in ((state.get("unpaid") or {}).get(key) or []):
        return False
    y, m = map(int, key.split("-"))
    occ = occurrence(it, y, m)
    return bool(occ and occ <= today)


def _prices(it):
    ps = [p for p in it.get("prices") or [] if isinstance(p, dict) and isinstance(p.get("from"), str) and MONTH_RE.match(p["from"])]
    return sorted(ps, key=lambda p: p["from"])


def price_at(it, key):
    """某月适用的价格（价格历史里不晚于该月的最后一条）。"""
    ps = _prices(it)
    if not ps:
        return float(it.get("amount") or 0), it.get("currency") or "CNY"
    best = ps[0]
    for p in ps:
        if p["from"] <= key:
            best = p
    return float(best.get("amount") or 0), best.get("currency") or "CNY"


# ---------- 汇率：R = {"now": {币种: 汇率}, "hist": [(日期, {币种: 汇率}), ...], "_dates": [...]} ----------
def rate_on(cur, day, R):
    """day 为 ISO 日期时取那天（或之前最近一天）的汇率，否则取当前汇率。"""
    if cur == "CNY":
        return 1.0
    r = None
    hist = (R or {}).get("hist") or []
    if day and hist:
        dates = R.get("_dates") or [d for d, _ in hist]
        i = max(bisect.bisect_right(dates, day) - 1, 0)
        r = (hist[i][1] or {}).get(cur)
    return r or ((R or {}).get("now") or {}).get(cur) or FALLBACK.get(cur) or 1.0


def cny_on(amount, cur, day, R, fee):
    return amount if cur == "CNY" else amount * rate_on(cur, day, R) * (1 + fee / 100)


def monthly_avg(state, today, R, fee):
    return sum(cny_on(float(it.get("amount") or 0), it.get("currency") or "CNY", None, R, fee) / STEP.get(it.get("cycle"), 1)
               for it in _items(state) if life(it, today) == "active")


# ---------- 文案 ----------
def _money(amount, cur):
    dec = 0 if cur in ("JPY", "KRW") or float(amount).is_integer() else 2
    return f"{SYMBOL.get(cur, '¥')}{amount:,.{dec}f}"


def _label(it, R, fee):
    amt, cur = float(it.get("amount") or 0), it.get("currency") or "CNY"
    s = f"{it.get('name', '')} {_money(amt, cur)}"
    if cur != "CNY":
        s += f"（≈¥{cny_on(amt, cur, None, R, fee):,.0f}）"
    return s


def _when(days):
    return "今天" if days == 0 else "明天" if days == 1 else f"{days} 天后"


def build(state, today, sent, R):
    """计算今天要推送的内容。返回 (标题, 正文, 去重键, 可一键标记已付的项目)；正文为空表示无需推送。"""
    s, state = settings_of(state), state or {}
    fee = s["fxFee"]
    items = [it for it in _items(state) if not paused_now(it)]
    groups = {g: [] for g, _ in SECTIONS}
    keys, actions = [], []

    def add(group, key, text, when):
        if key in sent:
            return False
        groups[group].append((when, text))
        keys.append(key)
        return True

    def act(it, mkey):
        amt, cur = float(it.get("amount") or 0), it.get("currency") or "CNY"
        actions.append({"id": str(it.get("id")), "key": mkey, "label": f"{it.get('name', '')} {_money(amt, cur)}"})

    horizon = max(s["lead"], s["autoLead"], s["bigLead"])
    for it in items:
        iid, big = it.get("id"), STEP.get(it.get("cycle"), 1) > 1
        for i in range(3):
            y, m = today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1
            occ = occurrence(it, y, m)
            if not occ:
                continue
            days, mkey = (occ - today).days, _key(y, m)
            if days < 0 or days > horizon or iid in ((state.get("paid") or {}).get(mkey) or []):
                continue
            tag = f"{_when(days)} {occ.month}/{occ.day}："
            if it.get("auto"):
                if 0 < days <= max(s["autoLead"], s["bigLead"] if big else 0):
                    add("auto", f"auto:{iid}:{occ}", tag + _label(it, R, fee), occ)
            elif days == 0:
                if add("today", f"today:{iid}:{occ}", _label(it, R, fee), occ):
                    act(it, mkey)
            elif big and days <= s["bigLead"]:
                if add("big", f"big:{iid}:{occ}", tag + _label(it, R, fee) + ("（年付）" if it.get("cycle") == "yearly" else "（季付）"), occ):
                    act(it, mkey)
            elif not big and days <= s["lead"]:
                if add("soon", f"soon:{iid}:{occ}", tag + _label(it, R, fee), occ):
                    act(it, mkey)
        if s["late"] and not it.get("auto"):
            occ = occurrence(it, today.year, today.month)
            mkey = _key(today.year, today.month)
            if occ and 1 <= (today - occ).days <= 3 and not is_paid(state, it, mkey, today):
                if add("late", f"late:{iid}:{occ}", f"{_label(it, R, fee)}（{occ.month}/{occ.day}，已过 {(today - occ).days} 天）", occ):
                    act(it, mkey)
        if s["trial"]:
            tr = _date(it.get("trialEnd"))
            if tr and 0 <= (tr - today).days <= 3:
                add("trial", f"trial:{iid}:{tr}", f"{it.get('name', '')}：试用 {tr.month}/{tr.day} 结束（{_when((tr - today).days)}），之后开始扣费 {_money(float(it.get('amount') or 0), it.get('currency') or 'CNY')}", tr)
            en = _date(it.get("endDate"))
            if en and 0 <= (en - today).days <= 7:
                add("end", f"end:{iid}:{en}", f"{it.get('name', '')}：{en.month}/{en.day} 到期（{_when((en - today).days)}）", en)

    parts = []
    # 年度报告：1 月第一次检查时发去年的
    akey = f"annual:{today.year - 1}"
    if s["annual"] and today.month == 1 and today.day <= 7 and akey not in sent:
        rep = year_report(state, today.year - 1, R, today)
        if rep["count"]:
            parts.append(report_text(rep))
            keys.append(akey)
    # 每月 1 号汇总
    skey = f"summary:{_key(today.year, today.month)}"
    if s["monthly"] and today.day == 1 and skey not in sent:
        due = [(it, occurrence(it, today.year, today.month)) for it in items]
        due = [(it, d) for it, d in due if d]
        if due:
            total = sum(cny_on(*price_at(it, _key(today.year, today.month)), None, R, fee) for it, _ in due)
            autos = sum(1 for it, _ in due if it.get("auto"))
            parts.append(f"📊 {today.month} 月共 {len(due)} 笔固定支出，合计约 ¥{total:,.0f}" + (f"，其中 {autos} 笔自动扣款" if autos else ""))
            keys.append(skey)
    # 超出预算：只在项目或预算变化后提醒一次（汇率每天的小波动不会反复提醒）
    budget = s["budgetAmount"]
    if s["budget"] and budget > 0:
        avg = monthly_avg(state, today, R, fee)
        if avg > budget:
            sig = sorted((str(it.get("id")), it.get("amount"), it.get("currency"), it.get("cycle")) for it in _items(state) if life(it, today) == "active")
            bkey = "budget:" + hashlib.sha256(json.dumps([sig, budget], default=str).encode()).hexdigest()[:12]
            if bkey not in sent:
                parts.append(f"💸 预算提醒：每月固定支出约 ¥{avg:,.0f}，超出预算 ¥{budget:,.0f}（多 ¥{avg - budget:,.0f}）")
                keys.append(bkey)
    for g, title in SECTIONS:
        rows = sorted(groups[g], key=lambda r: r[0])
        if rows:
            parts.append(title + "\n" + "\n".join("• " + t for _, t in rows))
    return f"固定支出提醒 · {today.month}月{today.day}日", "\n\n".join(parts), keys, actions


def month_text(state, today, R):
    """本月情况（给 /month 命令用）。"""
    s = settings_of(state)
    fee, key = s["fxFee"], _key(today.year, today.month)
    rows, due, paid = [], 0.0, 0.0
    for it in _items(state):
        occ = occurrence(it, today.year, today.month)
        if not occ:
            continue
        amt, cur = price_at(it, key)
        v = cny_on(amt, cur, occ.isoformat() if occ <= today else None, R, fee)
        due += v
        if is_paid(state, it, key, today):
            paid += v
        else:
            note = "（自动扣）" if it.get("auto") else ("（已过期）" if occ < today else "")
            rows.append((occ, f"{occ.month}/{occ.day} {it.get('name', '')} {_money(amt, cur)}{note}"))
    text = f"📅 {today.month} 月：应付 ¥{due:,.0f}，已付 ¥{paid:,.0f}，还剩 ¥{due - paid:,.0f}"
    text += ("\n\n待付：\n" + "\n".join("• " + t for _, t in sorted(rows))) if rows else "\n\n本月都付完了 🎉"
    avg, budget = monthly_avg(state, today, R, fee), s["budgetAmount"]
    text += f"\n\n每月固定约 ¥{avg:,.0f}"
    if budget:
        text += f"，预算 ¥{budget:,.0f}（{'超出' if avg > budget else '还剩'} ¥{abs(budget - avg):,.0f}）"
    return text


# ---------- 年度报告 ----------
def year_report(state, year, R, today):
    s = settings_of(state)
    fee, items = s["fxFee"], _items(state)
    since = state.get("since") if isinstance(state.get("since"), str) and MONTH_RE.match(state.get("since")) else None
    cur_key = _key(today.year, today.month)

    def scan(y):
        months = [_key(y, m) for m in range(1, 13) if (not since or _key(y, m) >= since) and _key(y, m) <= cur_key]
        out = {"months": months, "total": 0.0, "count": 0, "cats": {}, "items": {}}
        for it in items:
            for k in months:
                occ = occurrence(it, y, int(k[5:]))
                if not occ or occ > today:
                    continue
                amt, cur = price_at(it, k)
                v = cny_on(amt, cur, occ.isoformat(), R, fee)
                out["total"] += v
                out["count"] += 1
                cat, name = it.get("category") or "其他", it.get("name") or ""
                out["cats"][cat] = out["cats"].get(cat, 0) + v
                out["items"][name] = out["items"].get(name, 0) + v
        return out

    a, b = scan(year), scan(year - 1)
    n, total = len(a["months"]), a["total"]
    changes = []
    for it in items:
        ps = _prices(it)
        for i in range(1, len(ps)):
            if ps[i]["from"].startswith(f"{year}-"):
                p0, p1 = ps[i - 1], ps[i]
                a0, c0, a1, c1 = float(p0.get("amount") or 0), p0.get("currency") or "CNY", float(p1.get("amount") or 0), p1.get("currency") or "CNY"
                changes.append({"name": it.get("name", ""), "from": _money(a0, c0), "to": _money(a1, c1), "month": int(p1["from"][5:]),
                                "diff": round(cny_on(a1, c1, None, R, fee) - cny_on(a0, c0, None, R, fee), 2), "unit": UNIT.get(it.get("cycle"), "月")})
    avg = total / n if n else 0.0
    return {
        "year": year, "months": n, "first": a["months"][0] if n else None, "last": a["months"][-1] if n else None,
        "total": round(total, 2), "count": a["count"], "avg": round(avg, 2),
        "categories": sorted(({"name": k, "value": round(v, 2), "pct": round(v / total * 100, 1) if total else 0}
                              for k, v in a["cats"].items()), key=lambda x: -x["value"]),
        "top": sorted(({"name": k, "value": round(v, 2)} for k, v in a["items"].items()), key=lambda x: -x["value"])[:5],
        "changes": sorted(changes, key=lambda c: c["month"]),
        "prev": {"year": year - 1, "total": round(b["total"], 2), "months": len(b["months"])} if b["count"] else None,
        "incomePct": round(avg / s["income"] * 100, 1) if s["income"] and n else None,
        "ongoing": year == today.year,
    }


def report_text(rep):
    y = rep["year"]
    if not rep["count"]:
        return f"📒 {y} 年没有固定支出记录"
    span = "全年" if rep["months"] == 12 else f"{int(rep['first'][5:])}–{int(rep['last'][5:])} 月"
    lines = [f"📒 {y} 年固定支出年度报告（{span}）",
             f"共 {rep['count']} 笔，合计 ¥{rep['total']:,.0f}，平均每月 ¥{rep['avg']:,.0f}"
             + (f"，约占收入 {rep['incomePct']:.0f}%" if rep.get("incomePct") else "")]
    p = rep.get("prev")
    if p:
        if p["months"] == rep["months"]:
            d = rep["total"] - p["total"]
            pct = d / p["total"] * 100 if p["total"] else 0
            lines.append(f"比 {p['year']} 年{'多' if d >= 0 else '少'} ¥{abs(d):,.0f}（{pct:+.0f}%）")
        elif p["months"]:
            d = rep["avg"] - p["total"] / p["months"]
            lines.append(f"月均比 {p['year']} 年{'多' if d >= 0 else '少'} ¥{abs(d):,.0f}（{p['year']} 年记录了 {p['months']} 个月）")
    if rep["categories"]:
        lines.append("花得最多：" + "、".join(f"{c['name']} ¥{c['value']:,.0f}（{c['pct']:.0f}%）" for c in rep["categories"][:3]))
    if rep["top"]:
        lines.append("单项最多：" + "、".join(f"{t['name']} ¥{t['value']:,.0f}" for t in rep["top"][:3]))
    if rep["changes"]:
        lines.append("价格变化：\n" + "\n".join(
            f"• {c['name']}：{c['from']} → {c['to']}（{c['month']} 月起，每{c['unit']}{'多' if c['diff'] >= 0 else '少'} ¥{abs(c['diff']):,.0f}）"
            for c in rep["changes"]))
    else:
        lines.append("这一年没有项目涨价 👍")
    return "\n".join(lines)


# ---------- 发送 ----------
def _post_json(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "guding-zhichu/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(65536) or b"{}")


def _err(e):
    code = getattr(e, "code", None)
    return f"HTTP {code}" if code else type(e).__name__


def _tg(token, method, params=None, timeout=10, post=None):
    """调用 Telegram API；出错时返回 {"ok": False, "description": ...}，不会把 token 带进错误文字。"""
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if post is not None:
            return _post_json(url, post, timeout)
        if params:
            url += "?" + urllib.parse.urlencode(params)
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


def keyboard(actions):
    rows = []
    for a in (actions or [])[:8]:
        data = f"p:{a['id']}:{a['key']}"
        if len(data.encode()) <= 64:
            rows.append([{"text": f"✅ 已付：{a['label']}"[:60], "callback_data": data}])
    return {"inline_keyboard": rows} if rows else None


def send_telegram(title, body, actions=None):
    c = conf()
    chat = int(c["tg_chat"]) if c["tg_chat"].lstrip("-").isdigit() else c["tg_chat"]
    text = f"{title}\n\n{body}"
    if len(text) > 4000:  # Telegram 单条消息最多 4096 字，超出会被拒收
        text = text[:3960].rsplit("\n", 1)[0] + "\n…（内容较多，其余请打开页面查看）"
    payload = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
    kb = keyboard(actions)
    if kb:
        payload["reply_markup"] = kb
    return bool(_post_json(f"https://api.telegram.org/bot{c['tg_token']}/sendMessage", payload, 15).get("ok"))


def send_bark(title, body):
    c = conf()
    r = _post_json(f"{c['bark_base']}/push", {"device_key": c["bark_key"], "title": title, "body": body, "group": "固定支出"}, 10)
    return r.get("code") == 200


def deliver(title, body, want, actions=None):
    """want: {"telegram": bool, "bark": bool}。返回每个渠道的结果文字。"""
    ok, out = channels(), {}
    for name in ("telegram", "bark"):
        if not want.get(name):
            out[name] = "未开启"
        elif not ok[name]:
            out[name] = "未配置"
        else:
            try:
                sent = send_telegram(title, body, actions) if name == "telegram" else send_bark(title, body)
                out[name] = "成功" if sent else "失败"
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


_status_cache = {"t": 0.0, "v": None}


def channel_status():
    """渠道状态（会联网检查，结果缓存 5 分钟，设置页打开更快）。"""
    if _status_cache["v"] and time.time() - _status_cache["t"] < 300:
        return _status_cache["v"]
    v = {"config": public_conf(), "check": check()}
    _status_cache.update(t=time.time(), v=v)
    return v


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
        _status_cache["v"] = None
    return None


def detect_chats(token=None):
    """找出最近给机器人发过消息的聊天。机器人正被本服务监听时直接用监听到的记录，避免两处同时收消息冲突。"""
    typed = str(token or "").strip()
    token = typed or conf()["tg_token"]
    if not TOKEN_RE.match(token):
        return {"error": "Bot Token 格式不对（形如 123456789:ABC…）" if typed else "请先填写 Bot Token"}
    me = _tg(token, "getMe")
    bot = "@" + str((me.get("result") or {}).get("username", "")) if me.get("ok") else ""
    if POLLER is not None and POLLER.active_for(token):
        chats = POLLER.recent_chats()
        if not chats:
            return {"error": f"{bot or '机器人'} 还没收到消息：请先在 Telegram 里给它发一条消息（比如 /start），几秒后再点一次"}
        return {"chats": chats, "bot": bot, "typed": bool(typed)}
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
                chats[str(ch["id"])] = _chat_info(ch)
    if not chats:
        return {"error": f"{bot or '机器人'} 还没收到消息：请先在 Telegram 里给它发一条消息（比如 /start），再点一次"}
    return {"chats": list(chats.values())[::-1], "bot": bot, "typed": bool(typed)}


def _chat_info(ch):
    name = " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("title") or ch.get("username") or ""
    return {"id": str(ch["id"]), "type": "私聊" if ch.get("type") == "private" else "群组", "name": name}


# ---------- Telegram 机器人：一键标记已付、命令 ----------
class BotPoller(threading.Thread):
    """长轮询 getUpdates，只处理来自已绑定聊天的按钮和命令。"""

    def __init__(self, read_state, get_rates, mark):
        super().__init__(daemon=True, name="tg-bot")
        self.read_state, self.get_rates, self.mark = read_state, get_rates, mark
        self.token, self.offset, self.recent = None, None, {}
        self.lock = threading.Lock()

    def active_for(self, token):
        return bool(token) and token == self.token

    def recent_chats(self):
        with self.lock:
            return [{k: v for k, v in c.items() if k != "ts"} for c in sorted(self.recent.values(), key=lambda c: -c["ts"])]

    def run(self):
        time.sleep(3)
        while True:
            try:
                self.poll_once()
            except Exception:
                time.sleep(10)

    def poll_once(self):
        c = conf()
        tok = c["tg_token"]
        if not TOKEN_RE.match(tok or ""):
            self.token = None
            time.sleep(20)
            return
        if tok != self.token:
            self.token, self.offset = tok, None
            with self.lock:
                self.recent = {}
        params = {"timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}
        if self.offset is not None:
            params["offset"] = self.offset
        r = _tg(tok, "getUpdates", params, timeout=40)
        if not r.get("ok"):
            time.sleep(60 if "Conflict" in str(r.get("description", "")) else 15)
            return
        for u in r.get("result") or []:
            self.offset = int(u.get("update_id", 0)) + 1
            try:
                self.handle(tok, str(c["tg_chat"]), u)
            except Exception:
                pass

    def reply(self, tok, chat, text):
        _tg(tok, "sendMessage", post={"chat_id": int(chat) if chat.lstrip("-").isdigit() else chat, "text": text,
                                      "link_preview_options": {"is_disabled": True}})

    def handle(self, tok, my_chat, u):
        if "message" in u:
            m = u["message"]
            ch = m.get("chat") or {}
            if not ch.get("id"):
                return
            cid = str(ch["id"])
            with self.lock:
                self.recent[cid] = {**_chat_info(ch), "ts": time.time()}
            if m.get("date", 0) < time.time() - 600:   # 服务启动前积压的旧消息只记录，不回复
                return
            text = (m.get("text") or "").strip().split("@")[0]
            if cid != my_chat:
                if text.startswith("/start"):
                    self.reply(tok, cid, "你好，这是「固定支出」的私人提醒机器人。\n如果你是主人：回到网页的设置里点「自动获取」，再点「保存」就能完成绑定。")
                return
            if text.startswith("/start") or text in ("/help", "帮助"):
                self.reply(tok, cid, "✅ 已连接「固定支出」\n扣款前我会在这里提醒你，提醒下面的按钮可以一键标记已付。\n\n/month —— 查看本月情况")
            elif text in ("/month", "本月"):
                st = self.read_state()
                if st:
                    self.reply(tok, cid, month_text(st, dt.datetime.now(TZ).date(), self.get_rates()))
        elif "callback_query" in u:
            q = u["callback_query"]
            data, msg = str(q.get("data") or ""), q.get("message") or {}
            cid = str((msg.get("chat") or {}).get("id", ""))
            if not my_chat or cid != my_chat:
                _tg(tok, "answerCallbackQuery", {"callback_query_id": q.get("id"), "text": "没有权限"})
                return
            parts = data.split(":")
            if len(parts) != 3 or parts[0] not in ("p", "u") or not MONTH_RE.match(parts[2]):
                _tg(tok, "answerCallbackQuery", {"callback_query_id": q.get("id"), "text": "按钮已失效"})
                return
            paid = parts[0] == "p"
            ok, note, name = self.mark(parts[1], parts[2], paid)
            _tg(tok, "answerCallbackQuery", {"callback_query_id": q.get("id"), "text": f"{name} {note}".strip()})
            if not ok:
                return
            rows = []
            for row in (msg.get("reply_markup") or {}).get("inline_keyboard") or []:
                new_row = []
                for b in row:
                    if b.get("callback_data") == data:
                        label = str(b.get("text", "")).split("：", 1)[-1].replace("（点此撤销）", "")
                        b = ({"text": f"✔️ 已付：{label}（点此撤销）"[:60], "callback_data": f"u:{parts[1]}:{parts[2]}"} if paid
                             else {"text": f"✅ 已付：{label}"[:60], "callback_data": f"p:{parts[1]}:{parts[2]}"})
                    new_row.append(b)
                rows.append(new_row)
            if rows and msg.get("message_id"):
                _tg(tok, "editMessageReplyMarkup", post={"chat_id": int(cid) if cid.lstrip("-").isdigit() else cid,
                                                         "message_id": msg["message_id"], "reply_markup": {"inline_keyboard": rows}})


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
            return self.get_rates() or {}
        except Exception:
            return {}

    def status(self):
        now = dt.datetime.now(TZ)
        with self.lock:
            meta = self._load()
        state = self.read_state() or {}
        title, body, _, actions = build(state, now.date(), meta.get("sent") or {}, self._rates())
        return {"channels": channels(), "tz": str(TZ), "now": now.strftime("%Y-%m-%d %H:%M"),
                "lastRun": meta.get("lastRun"), "lastResult": meta.get("lastResult"),
                "lastDate": meta.get("lastDate"), "preview": {"title": title, "body": body, "buttons": len(actions)}}

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
            title, body, keys, actions = build(state, today, sent, self._rates())
            meta["lastAttempt"] = time.time()
            meta["lastRun"] = now.strftime("%Y-%m-%d %H:%M")
            want = {"telegram": s["telegram"], "bark": s["bark"]}
            if not body:
                meta.update(lastDate=today.isoformat(), lastResult={"note": "今天没有需要提醒的"})
            elif not any(want[c] and channels()[c] for c in want):
                meta.update(lastDate=today.isoformat(), lastResult={"note": "没有可用的推送渠道"})
            else:
                res = deliver(title, body, want, actions)
                meta["lastResult"] = res
                if any(v == "成功" for v in res.values()):
                    for k in keys:
                        sent[k] = today.isoformat()
                    meta["lastDate"] = today.isoformat()
            cutoff = (today - dt.timedelta(days=400)).isoformat()
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
