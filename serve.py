"""Serve the site locally: python serve.py [port]  (default 8765), then open http://localhost:<port>."""
import functools
import http.server
import sys
from pathlib import Path

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "site"))
print(f"Serving on http://localhost:{port}")
http.server.ThreadingHTTPServer(("", port), handler).serve_forever()
