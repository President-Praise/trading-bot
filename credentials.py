import os

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN")
if not DERIV_API_TOKEN:
    raise RuntimeError("Missing required environment variable: DERIV_API_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing required environment variable: TELEGRAM_BOT_TOKEN")

_chat_id_raw = os.getenv("TELEGRAM_CHAT_ID")
if not _chat_id_raw:
    raise RuntimeError("Missing required environment variable: TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID = [cid.strip() for cid in _chat_id_raw.split(",")]
