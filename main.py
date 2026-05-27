import logging
import database as db
from bot import build_app

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

if __name__ == "__main__":
    db.init_db()
    app = build_app()
    logging.info("Bot starting — polling for updates…")
    app.run_polling(drop_pending_updates=True)
