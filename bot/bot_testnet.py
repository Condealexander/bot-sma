"""
Bot de trading para el TESTNET de Binance Spot (dinero virtual).
Estrategia: cruce de medias móviles (SMA rápida / lenta), solo largos.

Instalación:
    pip install -U ccxt pandas python-dotenv

Configuración:
    1. Genera tus llaves en https://testnet.binance.vision
    2. Copia .env.example a .env y pega tus llaves ahí.

Uso:
    python bot_testnet.py
"""
import json
import logging
import os
import time
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv

# ---------- Configuración ----------
SYMBOL = "BTC/USDT"
BASE = SYMBOL.split("/")[0]
TIMEFRAME = "1h"
FAST = 20              # SMA rápida
SLOW = 50              # SMA lenta
QUOTE_AMOUNT = 100.0   # USDT (virtuales) por operación
STOP_LOSS = 0.02       # stop loss de 2% bajo el precio de entrada
MAX_LOSS_USDT = 50.0   # kill switch: se apaga si las pérdidas acumuladas llegan a esto
CHECK_EVERY = 60       # segundos entre revisiones
STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot_testnet.log"), logging.StreamHandler()],
)

# ---------- Conexión al testnet ----------
load_dotenv()
API_KEY = os.getenv("BINANCE_TESTNET_API_KEY")
API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET")
if not API_KEY or not API_SECRET:
    raise SystemExit("Faltan BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET en el archivo .env")

exchange = ccxt.binance(
    {"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True}
)
exchange.set_sandbox_mode(True)  # usa testnet.binance.vision

# Seguro extra: si por algún motivo no está en testnet, no arrancar.
if "testnet" not in str(exchange.urls["api"]):
    raise SystemExit("El modo sandbox no se activó. Por seguridad el bot no arranca.")

# ---------- Estado persistente (sobrevive a reinicios) ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"amount": 0.0, "entry": None, "cost": 0.0, "realized_pnl": 0.0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


state = load_state()


# ---------- Datos y señal ----------
def get_data() -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=SLOW + 10)
    df = pd.DataFrame(
        ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["fast"] = df["close"].rolling(FAST).mean()
    df["slow"] = df["close"].rolling(SLOW).mean()
    return df


def get_signal(df: pd.DataFrame):
    """Usa solo velas CERRADAS (la última fila aún está en formación)."""
    prev, last = df.iloc[-3], df.iloc[-2]
    if prev["fast"] <= prev["slow"] and last["fast"] > last["slow"]:
        return "buy"
    if prev["fast"] >= prev["slow"] and last["fast"] < last["slow"]:
        return "sell"
    return None


# ---------- Ejecución ----------
def in_position() -> bool:
    return state["amount"] > 0


def buy(price: float):
    if in_position():
        return
    order = exchange.create_market_buy_order_with_cost(SYMBOL, QUOTE_AMOUNT)
    filled = order.get("filled") or QUOTE_AMOUNT / price
    avg = order.get("average") or price
    cost = order.get("cost") or filled * avg
    state.update(amount=float(filled), entry=float(avg), cost=float(cost))
    save_state(state)
    logging.info(f"COMPRA {filled:.6f} {BASE} a {avg:.2f} (costo {cost:.2f} USDT)")


def sell(price: float, reason: str):
    if not in_position():
        return
    free = exchange.fetch_balance()[BASE]["free"]
    amount = float(exchange.amount_to_precision(SYMBOL, min(state["amount"], free)))
    order = exchange.create_market_sell_order(SYMBOL, amount)
    proceeds = order.get("cost") or amount * price
    pnl = proceeds - state["cost"]
    state["realized_pnl"] += pnl
    state.update(amount=0.0, entry=None, cost=0.0)
    save_state(state)
    logging.info(
        f"VENTA ({reason}) {amount:.6f} {BASE} -> {proceeds:.2f} USDT | "
        f"PnL operación: {pnl:+.2f} | PnL acumulado: {state['realized_pnl']:+.2f}"
    )


# ---------- Bucle principal ----------
def main():
    exchange.load_markets()
    last_candle_ts = None
    logging.info("Bot iniciado en TESTNET de Binance")

    while True:
        try:
            df = get_data()
            price = float(exchange.fetch_ticker(SYMBOL)["last"])

            # 1) Kill switch por pérdidas acumuladas
            if state["realized_pnl"] <= -MAX_LOSS_USDT:
                sell(price, "KILL SWITCH")
                logging.error("Pérdida máxima alcanzada. Bot detenido.")
                break

            # 2) Stop loss
            if in_position() and price < state["entry"] * (1 - STOP_LOSS):
                sell(price, "STOP LOSS")

            # 3) Señal, solo una vez por vela cerrada
            candle_ts = df.iloc[-2]["ts"]
            if candle_ts != last_candle_ts:
                last_candle_ts = candle_ts
                signal = get_signal(df)
                if signal == "buy":
                    buy(price)
                elif signal == "sell":
                    sell(price, "señal")
                logging.info(f"Precio: {price:.2f} | señal: {signal} | en posición: {in_position()}")

        except ccxt.NetworkError as e:
            logging.warning(f"Error de red, reintentando: {e}")
        except ccxt.ExchangeError as e:
            logging.error(f"Error del exchange: {e}")
        except Exception as e:
            logging.exception(f"Error inesperado: {e}")

        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
