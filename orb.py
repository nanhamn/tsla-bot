"""
Режим «первый час»: пробой диапазона открытия (Opening Range Breakout).

Время по Нью-Йорку (ET):
  09:00  — брифинг перед открытием (уровни вчерашнего дня и премаркета)
  09:30  — открытие рынка
  09:45  — сформирован диапазон открытия (первые 15 минут)
  09:45–10:30 — сигналы LONG/SHORT на пробой диапазона
  после сигнала — бот следит за ценой и сообщает: стоп или цель
  11:30  — итог дня
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import pandas as pd

NY = "America/New_York"


def _t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def now_ny() -> pd.Timestamp:
    return pd.Timestamp.now(tz=NY)


def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df.High + df.Low + df.Close) / 3
    return (tp * df.Volume).cumsum() / df.Volume.cumsum().replace(0, float("nan"))


def split_days(df: pd.DataFrame, day) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """-> (вчерашняя основная сессия, премаркет сегодня, основная сессия сегодня)"""
    df = df.copy()
    df.index = df.index.tz_convert(NY)
    t = df.index.time
    regular = (t >= dtime(9, 30)) & (t < dtime(16, 0))
    dates = df.index.date
    prev_days = sorted({d for d, r in zip(dates, regular) if r and d < day})
    prev = df[(dates == prev_days[-1]) & regular] if prev_days else df.iloc[0:0]
    pre = df[(dates == day) & (t >= dtime(4, 0)) & (t < dtime(9, 30))]
    today = df[(dates == day) & regular]
    return prev, pre, today


def fmt(x: float) -> str:
    return f"{x:.2f}"


# ---------------------------------------------------------------- брифинг
def briefing(df: pd.DataFrame, ticker: str, day) -> tuple[str, list[float]]:
    prev, pre, _ = split_days(df, day)
    lines = [f"☀️ <b>{ticker} · брифинг перед открытием</b>"]
    levels = []
    if not prev.empty:
        ph, pl, pc = prev.High.max(), prev.Low.min(), prev.Close.iloc[-1]
        levels += [ph, pl, pc]
        lines.append(f"Вчера: макс {fmt(ph)} · мин {fmt(pl)} · закрытие {fmt(pc)}")
    if not pre.empty:
        mh, ml, last = pre.High.max(), pre.Low.min(), pre.Close.iloc[-1]
        levels += [mh, ml]
        lines.append(f"Премаркет: макс {fmt(mh)} · мин {fmt(ml)} · сейчас <b>{fmt(last)}</b>")
        if not prev.empty:
            gap = (last / pc - 1) * 100
            word = "вверх ⬆️" if gap > 0 else "вниз ⬇️"
            lines.append(f"Гэп {word}: {gap:+.2f}%")
            if abs(gap) >= 2:
                lines.append("⚠️ Большой гэп — открытие может быть резким, не спеши с входом.")
    lines.append(f"\nОткрытие в {_alm(day, '09:30')}, диапазон готов в {_alm(day, '09:45')}, "
                 f"сигналы до {_alm(day, '10:30')} (Алматы).")
    return "\n".join(lines), sorted(levels)


def _alm(day, hhmm: str) -> str:
    ts = pd.Timestamp(f"{day} {hhmm}", tz=NY).tz_convert("Asia/Almaty")
    return f"{ts:%H:%M}"


# ---------------------------------------------------------------- сигналы
def check_signal(today: pd.DataFrame, cfg: dict, day_state: dict) -> dict | None:
    """Проверяет последнюю закрытую свечу. Возвращает сигнал или None."""
    o = cfg.get("orb", {})
    or_n = max(1, int(o.get("range_minutes", 15)) // 5)
    if len(today) <= or_n:
        return None
    c = today.iloc[-1]
    if c.name.time() >= _t(o.get("window_end", "10:30")):
        return None

    rng = today.iloc[:or_n]
    or_hi, or_lo = rng.High.max(), rng.Low.min()
    or_mid = (or_hi + or_lo) / 2
    vw = vwap(today).iloc[-1]
    ema9 = today.Close.ewm(span=9).mean().iloc[-1]
    ema21 = today.Close.ewm(span=21).mean().iloc[-1]
    prev_vol = today.Volume.iloc[or_n:-1]
    avg_vol = prev_vol.mean() if len(prev_vol) else today.Volume.iloc[:or_n].mean() / 2
    max_loss = float(o.get("max_loss_pct", 1.0)) / 100
    rr = float(o.get("reward_risk", 2.0))
    prev_close = today.Close.iloc[-2]

    for side in ("long", "short"):
        if day_state.get(side) or (side == "short" and not o.get("allow_short", True)):
            continue
        if side == "long":
            breakout = c.Close > or_hi and prev_close <= or_hi
            checks = [("Закрытие выше VWAP", c.Close > vw),
                      ("Объём выше среднего", c.Volume >= avg_vol),
                      ("EMA9 выше EMA21 (тренд вверх)", ema9 > ema21),
                      ("Свеча зелёная", c.Close > c.Open)]
        else:
            breakout = c.Close < or_lo and prev_close >= or_lo
            checks = [("Закрытие ниже VWAP", c.Close < vw),
                      ("Объём выше среднего", c.Volume >= avg_vol),
                      ("EMA9 ниже EMA21 (тренд вниз)", ema9 < ema21),
                      ("Свеча красная", c.Close < c.Open)]
        if not breakout:
            continue
        score = sum(ok for _, ok in checks)
        if not checks[0][1] or score < int(o.get("min_score", 3)):
            day_state.setdefault("skipped", []).append(f"{side} {c.name:%H:%M} ({score}/4)")
            continue

        entry = float(c.Close)
        if side == "long":
            stop = max(or_mid, entry * (1 - max_loss))
            target = entry + rr * (entry - stop)
        else:
            stop = min(or_mid, entry * (1 + max_loss))
            target = entry - rr * (stop - entry)
        return dict(side=side, time=str(c.name), entry=entry, stop=float(stop), target=float(target),
                    or_hi=float(or_hi), or_lo=float(or_lo), vwap=float(vw), score=score,
                    checks=[(n, bool(ok)) for n, ok in checks], status="open")
    return None


def signal_text(ticker: str, s: dict) -> str:
    long = s["side"] == "long"
    head = "🟢 <b>LONG (покупка)</b>" if long else "🔴 <b>SHORT (продажа)</b>"
    risk = abs(s["entry"] - s["stop"]) / s["entry"] * 100
    reward = abs(s["target"] - s["entry"]) / s["entry"] * 100
    t = (pd.Timestamp(s["time"]) + pd.Timedelta(minutes=5)).tz_convert("Asia/Almaty")
    lines = [
        f"{head} · {ticker} · 5-мин свеча закрылась в {t:%H:%M} (Алматы)",
        "",
        f"Вход ≈ <b>{fmt(s['entry'])}</b>",
        f"Стоп: <b>{fmt(s['stop'])}</b> (−{risk:.2f}%)",
        f"Цель: <b>{fmt(s['target'])}</b> (+{reward:.2f}%)",
        "",
        f"Почему: цена {'пробила вверх' if long else 'пробила вниз'} диапазон открытия "
        f"{fmt(s['or_lo'])}–{fmt(s['or_hi'])}.",
    ]
    lines += [f"{'✅' if ok else '❌'} {n}" for n, ok in s["checks"]]
    lines.append(f"Совпало {s['score']}/4")
    lines.append("\n<i>Это подсказка, а не гарантия. Стоп ставь сразу.</i>")
    return "\n".join(lines)


def update_trade(today: pd.DataFrame, s: dict) -> str | None:
    """Проверяет свечи после сигнала: сработал стоп или цель."""
    if s["status"] != "open":
        return None
    after = today[today.index > pd.Timestamp(s["time"])]
    for ts, c in after.iterrows():
        long = s["side"] == "long"
        hit_stop = c.Low <= s["stop"] if long else c.High >= s["stop"]
        hit_target = c.High >= s["target"] if long else c.Low <= s["target"]
        if hit_stop:          # если в одной свече и то и другое — считаем стоп (осторожно)
            s["status"], s["exit"], s["exit_time"] = "stop", s["stop"], str(ts)
            return f"❌ {s['side'].upper()} закрыт по стопу {fmt(s['stop'])}. Бывает — главное, что убыток ограничен."
        if hit_target:
            s["status"], s["exit"], s["exit_time"] = "target", s["target"], str(ts)
            return f"🎯 {s['side'].upper()} дошёл до цели {fmt(s['target'])}!"
    return None


def day_summary(ticker: str, day_state: dict, last_price: float) -> str:
    sigs = day_state.get("signals", [])
    lines = [f"📋 <b>{ticker} · итог первого часа</b>"]
    if not sigs:
        lines.append("Сигналов не было — рынок не дал чистого пробоя. Пропустить день тоже решение 👍")
    total = 0.0
    for s in sigs:
        sign = 1 if s["side"] == "long" else -1
        exit_ = s.get("exit", last_price)
        pct = sign * (exit_ / s["entry"] - 1) * 100
        total += pct
        status = {"stop": "стоп", "target": "цель", "open": f"ещё открыт, сейчас {fmt(last_price)}"}[s["status"]]
        lines.append(f"• {s['side'].upper()} от {fmt(s['entry'])} → {status}: {pct:+.2f}%")
    if day_state.get("skipped"):
        lines.append("Отброшено слабых пробоев: " + ", ".join(day_state["skipped"]))
    if sigs:
        lines.append(f"Итого: {total:+.2f}%")
    return "\n".join(lines)


# ---------------------------------------------------------------- главный шаг
def step(cfg: dict, state: dict, fetch, send, make_chart, force_now: pd.Timestamp | None = None):
    o = cfg.get("orb", {})
    now = force_now or now_ny()
    if now.weekday() >= 5:
        return
    day = now.date()
    key = str(day)
    st = state.setdefault("orb", {})
    for k in list(st):                      # чистим старые дни
        if k != key:
            st.pop(k)

    for ticker in cfg.get("tickers", ["TSLA"]):
        ds = st.setdefault(key, {}).setdefault(ticker, {})
        t = now.time()
        if t < _t(o.get("briefing_at", "09:00")) or t > dtime(16, 0):
            continue
        df = fetch(ticker, "5m", True)
        if df is None or df.empty:
            continue
        df.index = df.index.tz_convert(NY)
        # только закрытые свечи
        df = df[df.index + pd.Timedelta(minutes=5) <= now]

        if not ds.get("briefing"):
            text, levels = briefing(df, ticker, day)
            ds["briefing"] = True
            ds["levels"] = levels
            chart = _safe_chart(make_chart, df.iloc[-120:], ticker, "5m", 120, levels)
            send(text, chart)

        if t < _t("09:30"):
            continue
        _, _, today = split_days(df, day)
        if today.empty:
            continue

        sigs = ds.setdefault("signals", [])
        for s in sigs:                                   # следим за открытыми
            msg = update_trade(today, s)
            if msg:
                send(f"<b>{ticker}</b> · {msg}")

        last_ts = str(today.index[-1])
        if ds.get("last_checked") != last_ts:
            ds["last_checked"] = last_ts
            side_state = {s["side"]: True for s in sigs}
            side_state["skipped"] = ds.get("skipped", [])
            sig = check_signal(today, cfg, side_state)
            ds["skipped"] = side_state.get("skipped", [])
            if sig:
                sigs.append(sig)
                lv = [sig["or_hi"], sig["or_lo"], sig["stop"], sig["target"]]
                chart = _safe_chart(make_chart, df.iloc[-60:], ticker, "5m", 60, lv,
                                    ["#f5c542", "#f5c542", "#ef5350", "#26a69a"])
                send(signal_text(ticker, sig), chart)

        if t >= _t(o.get("summary_at", "11:30")) and not ds.get("summary"):
            ds["summary"] = True
            send(day_summary(ticker, ds, float(today.Close.iloc[-1])))


def _safe_chart(make_chart, df, ticker, interval, n, levels, colors=None):
    try:
        return make_chart(df, ticker, interval, n, levels, colors)
    except Exception as e:
        print("график не построен:", e)
        return None
