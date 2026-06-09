import os
import sys
import threading
import logging

# Add scripts dir to path so imports work on Railway
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


def run_poller():
    log.info("Starting lead poller thread...")
    from sell_do_poller import main as poller_main
    poller_main()


if __name__ == "__main__":
    # Start poller in background daemon thread
    poller_thread = threading.Thread(target=run_poller, daemon=True, name="poller")
    poller_thread.start()
    log.info("Poller thread started")

    # Start Flask webhook in main thread
    from inbound_handler import app
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting Flask webhook on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)
