"""
Проверка стратегии «первый час» на истории (последние ~60 дней 5-мин свечей).
Запуск:  .venv/bin/python backtest.py
"""
from __future__ import annotations

import copy
import sys

import pandas as pd
import requests
import yaml

import orb

NY = "America/New_York"


def load(ticker: str) -> pd.DataFrame:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="60d", interval="5m", prepost=False)
    except Exception as e:
        print("yfinance не сработал, беру напрямую:", e)
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"range": "60d", "interval": "5m"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"],
                           "Close": q["close"], "Volume": q["volume"]},
                          index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = df.index.tz_convert(NY)
    t = df.index.time
    return df[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())]


def run(df: pd.DataFrame, cfg: dict, exit_at: str = "11:30") -> pd.DataFrame:
    trades = []
    for day, today in df.groupby(df.index.date):
        ds: dict = {}
        sigs = []
        for i in range(len(today)):
            part = today.iloc[: i + 1]
            for s in sigs:
                orb.update_trade(part, s)
            side_state = {s["side"]: True for s in sigs}
            sig = orb.check_signal(part, cfg, side_state)
            if sig:
                sigs.append(sig)
        cut = today[today.index.time <= pd.Timestamp(exit_at).time()]
        for s in sigs:
            orb.update_trade(cut, s)
            if s["status"] == "open":                     # закрываем в 11:30 по рынку
                s["status"], s["exit"] = "time", float(cut.Close.iloc[-1])
            sign = 1 if s["side"] == "long" else -1
            pct = sign * (s["exit"] / s["entry"] - 1) * 100
            risk = abs(s["entry"] - s["stop"]) / s["entry"] * 100
            trades.append(dict(day=str(day), side=s["side"], time=pd.Timestamp(s["time"]).strftime("%H:%M"),
                               entry=round(s["entry"], 2), stop=round(s["stop"], 2), target=round(s["target"], 2),
                               result=s["status"], pct=round(pct, 3), risk_pct=round(risk, 3),
                               R=round(pct / risk, 2) if risk else 0))
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame, days: int) -> dict:
    if t.empty:
        return dict(сделок=0)
    streak = mx = 0
    for p in t.pct:
        streak = streak + 1 if p < 0 else 0
        mx = max(mx, streak)
    return {
        "дней": days,
        "сделок": len(t),
        "в плюс": int((t.pct > 0).sum()),
        "в минус": int((t.pct <= 0).sum()),
        "% побед": round((t.pct > 0).mean() * 100),
        "цель": int((t.result == "target").sum()),
        "стоп": int((t.result == "stop").sum()),
        "по времени": int((t.result == "time").sum()),
        "средняя сделка %": round(t.pct.mean(), 3),
        "сумма %": round(t.pct.sum(), 2),
        "сумма в R": round(t.R.sum(), 1),
        "макс. убытков подряд": mx,
    }


def main():
    cfg = yaml.safe_load(open("config.yml", encoding="utf-8"))
    ticker = (sys.argv[1] if len(sys.argv) > 1 else cfg.get("tickers", ["TSLA"])[0]).upper()
    print(f"Загружаю 5-мин свечи {ticker} за ~60 дней...")
    df = load(ticker)
    days = df.index.normalize().nunique()
    print(f"Получено {len(df)} свечей, {days} торговых дней "
          f"({df.index[0]:%d.%m} – {df.index[-1]:%d.%m}).\n")

    variants = {
        "ТЕКУЩИЕ настройки": {},
        "LONG + цель 1.5R": {"allow_short": False, "reward_risk": 1.5},
        "LONG+SHORT, цель 1.5R": {"allow_short": True, "reward_risk": 1.5},
        "только LONG, цель 2R": {"allow_short": False, "reward_risk": 2.0},
        "строже: 4 из 4": {"min_score": 4},
        "мягче: 2 из 4": {"min_score": 2},
        "цель 3R": {"reward_risk": 3.0},
        "диапазон 30 мин": {"range_minutes": 30, "window_end": "11:00"},
    }
    rows = {}
    main_trades = None
    for name, ch in variants.items():
        c = copy.deepcopy(cfg)
        c.setdefault("orb", {}).update(ch)
        t = run(df, c)
        rows[name] = stats(t, days)
        if main_trades is None:
            main_trades = t

    table = pd.DataFrame(rows).T
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(table.to_string())
    print()
    if main_trades is not None and not main_trades.empty:
        main_trades.to_csv("backtest_trades.csv", index=False)
        print("Сделки с текущими настройками:")
        print(main_trades.to_string(index=False))
        print("\nСохранено в backtest_trades.csv")
    print("\nВажно: комиссии и проскальзывание не учтены; прошлое не гарантирует будущее.")


if __name__ == "__main__":
    main()
