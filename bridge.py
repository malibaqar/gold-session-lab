"""Optional alert receiver: authentication and inbox writes only; never calls MT5."""
import hmac
import json
import os
import time
from flask import Flask, request, jsonify
from waitress import serve
from storage import BASE, Store
from engine import SECONDS


def create_app(config=None, db_path=None):
    cfg = config or json.loads((BASE / "config.json").read_text())
    secret = os.environ.get("GOLD_WEBHOOK_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Set GOLD_WEBHOOK_SECRET to a random secret of at least 32 characters")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8192

    @app.post("/webhook")
    def webhook():
        obj = request.get_json(silent=True)
        if not isinstance(obj, dict) or not isinstance(obj.get("secret"), str):
            return jsonify(error="unauthorized"), 401
        if not hmac.compare_digest(obj["secret"], secret):
            return jsonify(error="unauthorized"), 401
        tf, ticker = obj.get("timeframe"), obj.get("ticker")
        bar, direction = obj.get("bar_open"), obj.get("direction")
        if (obj.get("version") != 1 or obj.get("setup") != "session_pullback" or
                type(tf) is not str or tf not in SECONDS or ticker != cfg["tv_ticker"] or
                type(bar) is not int or type(direction) is not int or direction not in (-1, 1)):
            return jsonify(error="invalid signal"), 400
        if bar % SECONDS[tf] or not 0 <= time.time() - (bar + SECONDS[tf]) <= cfg["max_signal_age_seconds"]:
            return jsonify(error="expired or future candle"), 400
        store = Store() if db_path is None else Store(db_path)
        try:
            store.insert_alert(ticker, tf, bar, direction)
        finally:
            store.db.close()
        return jsonify(accepted=True), 200

    @app.get("/health")
    def health():
        return jsonify(service="gold-confirmation-inbox", ok=True)

    return app


if __name__ == "__main__":
    # Bind to loopback. Provide HTTPS via your own authenticated tunnel/reverse proxy.
    serve(create_app(), host="127.0.0.1", port=8787, threads=2)
