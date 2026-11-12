"""
Мониторинг свечей (TSLA и др.) — запускается GitHub Actions по расписанию.
Берёт свечи с Yahoo Finance, ищет сигналы на последней ЗАКРЫТОЙ свече
и шлёт уведомление в Telegram с картинкой графика.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
INTERVAL_MIN = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "90m": 90}

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


# ---------- данные ----------
def fetch_direct(ticker: str, interval: str, prepost: bool) -> pd.DataFrame:
    """Запасной вариант без yfinance: напрямую Yahoo chart API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = requests.get(url, params={"range": "5d", "interval": interval, "includePrePost": str(prepost).lower()},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"],
                       "Close": q["close"], "Volume": q["volume"]}, index=idx)
    return df.dropna()


def fetch(ticker: str, interval: str, prepost: bool) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        return fetch_direct(ticker, interval, prepost)
    df = yf.Ticker(ticker).history(period="5d", interval=interval, prepost=prepost)
    if df.empty:
        return df
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


def drop_unclosed(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Убираем текущую (ещё не закрытую) свечу."""
    if df.empty:
        return df
    minutes = INTERVAL_MIN.get(interval, 15)
    last_start = df.index[-1].tz_convert("UTC") if df.index.tz else df.index[-1].tz_localize("UTC")
    if datetime.now(timezone.utc) < last_start + pd.Timedelta(minutes=minutes):
        return df.iloc[:-1]
    return df


# ---------- сигналы ----------
def detect(df: pd.DataFrame, cfg: dict, ticker: str) -> list[str]:
    if len(df) < 22:
        return []
    c, p = df.iloc[-1], df.iloc[-2]
    sig = []

    body = c.Close - c.Open
    body_abs = abs(body)
    rng = c.High - c.Low
    green = body > 0
    body_pct = body_abs / c.Open * 100

    # 1. Большая свеча
    if body_pct >= cfg.get("big_candle_pct", 1.5):
        sig.append(f"{'🟢 Большая зелёная' if green else '🔴 Большая красная'} свеча: {body / c.Open * 100:+.2f}%")

    # 2. Всплеск объёма
    avg_vol = df.Volume.iloc[-21:-1].mean()
    mult = cfg.get("volume_spike_mult", 2.5)
    if avg_vol > 0 and c.Volume >= mult * avg_vol:
        sig.append(f"📊 Всплеск объёма: x{c.Volume / avg_vol:.1f} от среднего")

    # 3. Поглощение
    if cfg.get("engulfing", True):
        p_green = p.Close > p.Open
        if green and not p_green and c.Open <= p.Close and c.Close >= p.Open and body_abs > abs(p.Close - p.Open):
            sig.append("🟢 Бычье поглощение")
        if not green and p_green and c.Open >= p.Close and c.Close <= p.Open and body_abs > abs(p.Close - p.Open):
            sig.append("🔴 Медвежье поглощение")

    # 4. Молот / падающая звезда
    if cfg.get("hammer_star", True) and rng > 0 and body_abs > 0:
        upper = c.High - max(c.Open, c.Close)
        lower = min(c.Open, c.Close) - c.Low
        trend_down = df.Close.iloc[-6:-1].iloc[-1] < df.Close.iloc[-6:-1].iloc[0]
        if lower >= 2 * body_abs and upper <= body_abs * 0.5 and trend_down:
            sig.append("🔨 Молот после снижения (возможный разворот вверх)")
        if upper >= 2 * body_abs and lower <= body_abs * 0.5 and not trend_down:
            sig.append("🌠 Падающая звезда после роста (возможный разворот вниз)")

    # 5. Три свечи подряд
    if cfg.get("three_in_row", True):
        last3 = df.iloc[-3:]
        bodies = (last3.Close - last3.Open)
        b = bodies.abs().values
        accel = b[0] < b[1] < b[2]
        if (bodies > 0).all() and accel:
            sig.append("🚀 Три зелёные свечи подряд с ускорением")
        if (bodies < 0).all() and accel:
            sig.append("⚠️ Три красные свечи подряд с ускорением")

    # 6. Пробой уровней
    for lvl in (cfg.get("levels") or {}).get(ticker, []):
        if p.Close < lvl <= c.Close:
            sig.append(f"⬆️ Пробой уровня {lvl} вверх")
        if p.Close > lvl >= c.Close:
            sig.append(f"⬇️ Пробой уровня {lvl} вниз")

    return sig


# ---------- график ----------
def make_chart(df: pd.DataFrame, ticker: str, interval: str, n: int, levels: list, colors: list | None = None) -> Path:
    import mplfinance as mpf
    path = ROOT / f"chart_{ticker}.png"
    data = df.iloc[-n:].copy()
    if data.index.tz is not None:                       # время на графике — по Алматы
        data.index = data.index.tz_convert("Asia/Almaty").tz_localize(None)
    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc, gridstyle=":")
    kw = dict(type="candle", volume=True, style=style, mav=(9, 21),
              title=f"{ticker} {interval} (время Алматы)", figsize=(11, 6), savefig=dict(fname=path, dpi=110))
    if levels:
        lo, hi = data.Low.min(), data.High.max()
        cols = colors or ["#f5c542"] * len(levels)
        pairs = [(l, c) for l, c in zip(levels, cols) if lo * 0.97 <= l <= hi * 1.03]
        if pairs:
            kw["hlines"] = dict(hlines=[p[0] for p in pairs], colors=[p[1] for p in pairs],
                                linestyle="--", linewidths=1.0)
    mpf.plot(data, **kw)
    return path


# ---------- Telegram ----------
def send(text: str, photo: Path | None = None):
    if not (TG_TOKEN and TG_CHAT):
        print("[нет TELEGRAM_TOKEN/CHAT_ID — вывожу в лог]\n" + text)
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    for chat in [c.strip() for c in TG_CHAT.split(",") if c.strip()]:   # несколько id через запятую
        try:
            if photo and photo.exists():
                with open(photo, "rb") as f:
                    r = requests.post(f"{base}/sendPhoto", data={"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML"},
                                      files={"photo": f}, timeout=30)
            else:
                r = requests.post(f"{base}/sendMessage", data={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
            print(f"telegram {chat}:", r.status_code, r.text[:200])
        except Exception as e:
            print(f"telegram {chat}: ошибка {e}")


# ---------- main ----------
def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))


def check_once(cfg: dict, state: dict, test_mode: bool = False):
    interval = cfg.get("interval", "15m")
    for t in cfg.get("tickers", ["TSLA"]):
        try:
            df = drop_unclosed(fetch(t, interval, cfg.get("include_prepost", True)), interval)
        except Exception as e:
            print(f"{t}: ошибка загрузки: {e}")
            continue
        if df.empty:
            print(f"{t}: нет данных")
            continue

        last_ts = str(df.index[-1])
        if state.get(t) == last_ts and not test_mode:
            print(f"{t}: новой свечи нет ({last_ts})")
            continue
        state[t] = last_ts

        signals = detect(df, cfg, t)
        c = df.iloc[-1]
        print(f"{t} {last_ts}: close={c.Close:.2f} сигналы={signals}")
        if not signals and not test_mode:
            continue

        local = df.index[-1].tz_convert("America/New_York").strftime("%d.%m %H:%M ET")
        text = (f"<b>{t}</b> · {interval} · свеча {local}\n"
                f"O {c.Open:.2f}  H {c.High:.2f}  L {c.Low:.2f}  C <b>{c.Close:.2f}</b>\n\n"
                + ("\n".join(signals) if signals else "✅ Тестовое сообщение: бот работает"))
        levels = (cfg.get("levels") or {}).get(t, [])
        try:
            chart = make_chart(df, t, interval, cfg.get("chart_candles", 60), levels)
        except Exception as e:          # например, на телефоне нет mplfinance
            print("график не построен:", e)
            chart = None
        send(text, chart)

    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    test_mode = "--test" in sys.argv

    if "--session" in sys.argv:             # GitHub Actions: одна торговая сессия и выход
        run_session(state, test_mode)
        return

    if "--loop" not in sys.argv:            # одна проверка
        check_once(load_cfg(), state, test_mode)
        return

    # режим 24/7: бесконечный цикл (телефон / ПК / сервер)
    import time
    print("Запущен режим 24/7. Остановить: Ctrl+C")
    if test_mode:
        check_once(load_cfg(), state, True)
    import orb
    cfg = load_cfg()
    last_beat = 0.0
    while True:
        if time.time() - last_beat > 600:          # раз в 10 минут: «я жив»
            last_beat = time.time()
            alm = pd.Timestamp.now(tz="Asia/Almaty")
            print(f"[{alm:%d.%m %H:%M}] бот работает. Режим «первый час»: брифинг ~19:00, сигналы 19:45–20:30 (пн–пт)")
        try:
            cfg = load_cfg()                 # конфиг перечитывается на лету
            if cfg.get("candle_alerts", True):
                check_once(cfg, state)
            if cfg.get("orb", {}).get("enabled", False):
                orb.step(cfg, state, fetch, send, make_chart)
            STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        except Exception as e:
            print("ошибка цикла:", e)
            time.sleep(60)
        time.sleep(cfg.get("poll_seconds", 30))


def run_session(state: dict, test_mode: bool = False):
    """Работает с запуска до конца окна «первого часа» (11:40 по Нью-Йорку) и завершается.
    Для GitHub Actions: старт заранее, поэтому задержки запуска не важны."""
    import time
    import orb
    cfg = load_cfg()
    end = orb.now_ny().normalize() + pd.Timedelta(hours=11, minutes=40)
    print(f"Сессия запущена, работаю до {end:%H:%M} ET")
    if test_mode:
        send("✅ Тест: бот на GitHub запущен и работает.")
    if orb.now_ny().weekday() >= 5:
        print("Выходной — биржа закрыта.")
        return
    while orb.now_ny() < end:
        try:
            cfg = load_cfg()
            orb.step(cfg, state, fetch, send, make_chart)
        except Exception as e:
            print("ошибка:", e)
        time.sleep(cfg.get("poll_seconds", 20))
    print("Сессия завершена.")


if __name__ == "__main__":
    main()
