#!/usr/bin/env python3
"""Local dev server. Run: python3 server.py"""
import http.server
import os

PORT = 3000
os.chdir(os.path.dirname(os.path.abspath(__file__)))

server = http.server.HTTPServer(("", PORT), http.server.SimpleHTTPRequestHandler)
print(f"\n  HLD Docs running at \033[1;34mhttp://localhost:{PORT}\033[0m\n  Ctrl+C to stop\n")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\n  Server stopped.")
