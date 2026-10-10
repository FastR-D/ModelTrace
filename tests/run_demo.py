"""Run the real web UI with a local mock API for safe manual acceptance."""

import argparse
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app
from test_local_smoke import MockModelHandler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-port", type=int, default=0)
    parser.add_argument("--api-port", type=int, default=0)
    args = parser.parse_args()
    app.config.update(PUBLIC_MODE=False, READ_ONLY=False)
    mock = ThreadingHTTPServer(("127.0.0.1", args.api_port), MockModelHandler)
    web = make_server("127.0.0.1", args.ui_port, app, threaded=True)
    worker = threading.Thread(target=mock.serve_forever, daemon=True)
    worker.start()
    print(f"Demo UI: http://127.0.0.1:{web.server_port}", flush=True)
    print(f"Mock API: http://127.0.0.1:{mock.server_port}/v1; model: local-mock; token: local-test-only", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        mock.shutdown()
        mock.server_close()
