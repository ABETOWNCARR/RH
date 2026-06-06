"""
Robin the Hood — Backend API
FastAPI backend for pattern scanning and notifications.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import yfinance as yf
import pandas as pd
import ta
import os
import logging

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "firebase_credentials.json")
    if os.path.exists(_cred_path):
        cred = credentials.Certificate(_cred_path)
        firebase_admin.initialize_app(cred)
        FIREBASE_ENABLED = True
    else:
        FIREBASE_ENABLED = False
        logging.warning("Firebase credentials not found — push notifications disabled.")
except ImportError:
    FIREBASE_ENABLED = False

app = FastAPI(title="Robin the Hood API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScanRequest(BaseModel):
    tickers: List[str]
    fcm_token: Optional[str] = None

class TradeRequest(BaseModel):
    ticker: str
    action: str
    quantity: float
    demo_mode: bool = True


def fetch_ohlcv(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=True)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    return df


def detect_patterns(ticker: str) -> List[dict]:
    try:
        df = fetch_ohlcv(ticker)
    except Exception as e:
        logging.error(f"Failed to fetch {ticker}: {e}")
        return []

    if df.empty or len(df) < 30:
        return []

    signals = []

    # RSI
    try:
        rsi = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
        last_rsi, prev_rsi = rsi.iloc[-1], rsi.iloc[-2]
        if prev_rsi < 30 and last_rsi >= 30:
            signals.append({"pattern": "RSI Oversold Bounce", "signal": "Bullish Reversal",
                            "confidence": round(min(0.95, 0.65 + (30 - prev_rsi) / 100), 2),
                            "detail": f"RSI crossed above 30 (was {prev_rsi:.1f})"})
        elif last_rsi > 70:
            signals.append({"pattern": "RSI Overbought", "signal": "Bearish Warning",
                            "confidence": round(min(0.90, 0.55 + (last_rsi - 70) / 100), 2),
                            "detail": f"RSI at {last_rsi:.1f}"})
    except Exception as e:
        logging.warning(f"RSI failed for {ticker}: {e}")

    # MACD
    try:
        macd_ind = ta.trend.MACD(df["close"])
        macd_line = macd_ind.macd()
        signal_line = macd_ind.macd_signal()
        if macd_line.iloc[-2] < signal_line.iloc[-2] and macd_line.iloc[-1] > signal_line.iloc[-1]:
            signals.append({"pattern": "MACD Bullish Crossover", "signal": "Bullish Momentum",
                            "confidence": 0.72, "detail": "MACD crossed above signal line"})
        elif macd_line.iloc[-2] > signal_line.iloc[-2] and macd_line.iloc[-1] < signal_line.iloc[-1]:
            signals.append({"pattern": "MACD Bearish Crossover", "signal": "Bearish Momentum",
                            "confidence": 0.70, "detail": "MACD crossed below signal line"})
    except Exception as e:
        logging.warning(f"MACD failed for {ticker}: {e}")

    # Bull Flag
    try:
        closes = df["close"].values
        pole_gain = (closes[-6] - closes[-11]) / closes[-11] if closes[-11] != 0 else 0
        flag_range = (max(closes[-5:]) - min(closes[-5:])) / closes[-6] if closes[-6] != 0 else 0
        if pole_gain > 0.05 and flag_range < 0.03:
            signals.append({"pattern": "Bull Flag", "signal": "Bullish Continuation",
                            "confidence": round(min(0.88, 0.60 + pole_gain * 2), 2),
                            "detail": f"Pole gain: {pole_gain*100:.1f}%, flag range: {flag_range*100:.1f}%"})
    except Exception as e:
        logging.warning(f"Bull Flag failed for {ticker}: {e}")

    # Golden/Death Cross
    try:
        if len(df) >= 200:
            sma50 = df["close"].rolling(50).mean()
            sma200 = df["close"].rolling(200).mean()
            if sma50.iloc[-2] < sma200.iloc[-2] and sma50.iloc[-1] > sma200.iloc[-1]:
                signals.append({"pattern": "Golden Cross", "signal": "Bullish Long-Term",
                                "confidence": 0.82, "detail": "50-day SMA crossed above 200-day SMA"})
            elif sma50.iloc[-2] > sma200.iloc[-2] and sma50.iloc[-1] < sma200.iloc[-1]:
                signals.append({"pattern": "Death Cross", "signal": "Bearish Long-Term",
                                "confidence": 0.80, "detail": "50-day SMA crossed below 200-day SMA"})
    except Exception as e:
        logging.warning(f"Golden/Death Cross failed for {ticker}: {e}")

    # Volume Surge
    try:
        avg_vol = df["volume"].tail(20).mean()
        last_vol = df["volume"].iloc[-1]
        last_close, prev_close = df["close"].iloc[-1], df["close"].iloc[-2]
        if last_vol > avg_vol * 2:
            direction = "Bullish" if last_close > prev_close else "Bearish"
            signals.append({"pattern": "Volume Surge", "signal": f"{direction} Volume Spike",
                            "confidence": round(min(0.78, 0.50 + (last_vol / avg_vol - 2) * 0.05), 2),
                            "detail": f"Volume {last_vol/avg_vol:.1f}x above 20-day average"})
    except Exception as e:
        logging.warning(f"Volume surge failed for {ticker}: {e}")

    return signals


@app.get("/")
def root():
    return {"status": "ok", "service": "Robin the Hood API"}

@app.get("/health")
def health():
    return {"status": "healthy", "firebase_enabled": FIREBASE_ENABLED}

@app.post("/scan_and_notify")
def scan_and_notify(req: ScanRequest):
    results = {}
    high_confidence_alerts = []

    for ticker in req.tickers:
        ticker = ticker.upper().strip()
        patterns = detect_patterns(ticker)
        results[ticker] = patterns
        for p in patterns:
            if p["confidence"] >= 0.75:
                high_confidence_alerts.append({"ticker": ticker, **p})

    if req.fcm_token and FIREBASE_ENABLED:
        for alert in high_confidence_alerts:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(
                        title=f"Pattern Detected: {alert['ticker']}",
                        body=f"{alert['pattern']} ({int(alert['confidence']*100)}%) — {alert['signal']}",
                    ),
                    token=req.fcm_token,
                )
                messaging.send(message)
            except Exception as e:
                logging.warning(f"FCM send failed for {alert['ticker']}: {e}")

    return {"results": results, "high_confidence_alerts": high_confidence_alerts, "scanned": len(req.tickers)}

@app.post("/execute_trade")
def execute_trade(req: TradeRequest):
    raise HTTPException(
        status_code=503,
        detail="Auto-trading is not yet available. Robin the Hood operates in alerts-only mode until Robinhood opens a public trading API."
    )
