"""Read-only dashboard server: loopback only, factory data only, fixed file names only.

Serves factory/dashboard.html and a directory-listing view of factory/jobs
compatible with the reference dashboard. Never serves the repo root, never
follows arbitrary paths, and never writes anything.
"""

from __future__ import annotations

import html
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

JOB_ID = re.compile(r"^\d{3}-[a-z0-9-]+$")
TOP_FILES = re.compile(r"^(job\.json|issue\.md|intake(-\d+)?\.md|spec\.md|build\.md|decision\.md|rejection\.md|"
                       r"feedback-\d+\.md)$")
ROUND_DIR = re.compile(r"^round-\d+$")
ROUND_FILES = re.compile(r"^(checks|merge-checks|review-[a-z]+)\.(md|log)$")


def make_handler(factory_dir: Path):
    factory_dir = Path(factory_dir).resolve()
    jobs_dir = factory_dir / "jobs"

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-dashboard"

        def log_message(self, *args):
            pass

        def send(self, code, body: bytes, ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def listing(self, names):
            items = "".join(f'<a href="{html.escape(n, quote=True)}">{html.escape(n)}</a>\n' for n in names)
            return self.send(200, f"<!doctype html><pre>\n{items}</pre>".encode(), "text/html; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path in {"/", "/dashboard.html"}:
                return self.send(200, (factory_dir / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            parts = [p for p in path.split("/") if p]
            if not parts or parts[0] != "jobs" or any(p in {".", ".."} for p in parts):
                return self.send(404, b"not found")
            if len(parts) == 1:
                ids = sorted(p.name for p in jobs_dir.iterdir()
                             if JOB_ID.match(p.name) and (p / "job.json").is_file()) if jobs_dir.is_dir() else []
                return self.listing([i + "/" for i in ids])
            job_id = parts[1]
            jd = jobs_dir / job_id
            if not JOB_ID.match(job_id) or not (jd / "job.json").is_file():
                return self.send(404, b"not found")
            rest = parts[2:]
            if not rest:
                names = sorted(p.name for p in jd.iterdir() if p.is_file() and TOP_FILES.match(p.name))
                names += sorted(p.name + "/" for p in jd.iterdir() if p.is_dir() and ROUND_DIR.match(p.name))
                return self.listing(names)
            if len(rest) == 1 and ROUND_DIR.match(rest[0]) and path.endswith("/"):
                rd = jd / rest[0]
                if not rd.is_dir():
                    return self.send(404, b"not found")
                return self.listing(sorted(p.name for p in rd.iterdir() if p.is_file() and ROUND_FILES.match(p.name)))
            ok = (len(rest) == 1 and TOP_FILES.match(rest[0])) or \
                 (len(rest) == 2 and ROUND_DIR.match(rest[0]) and ROUND_FILES.match(rest[1]))
            target = jd.joinpath(*rest)
            if not ok or target.is_symlink() or not target.is_file() or jd.resolve() not in target.resolve().parents:
                return self.send(404, b"not found")
            ctype = "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            return self.send(200, target.read_bytes(), ctype)

        def do_POST(self):
            self.send(405, b"read-only")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def serve(factory_dir: Path, port: int = 8765, out=print):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(factory_dir))
    out(f"factory dashboard (read-only): http://127.0.0.1:{httpd.server_address[1]}/  (Ctrl-C to stop)")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
