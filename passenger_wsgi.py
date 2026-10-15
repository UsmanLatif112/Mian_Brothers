"""
LiteSpeed / Passenger entry point.

IMPORTANT:
- Do NOT use os.execl here (re-exec loops with alt-python / lswsgi).
- Do NOT run DB migrations on import (see scripts/migrate_schema.py).
- Keep this file cheap: one create_app(), then serve.
- cPanel env: set LSAPI_CHILDREN=3, then Stop + Start the app
  (plain Restart can leave old workers running).
"""
import os
import sys
import traceback

VENV = "/home1/mygymlahore/virtualenv/octaneflow.udottechnologies.com/3.13"
APP_ROOT = os.path.dirname(os.path.abspath(__file__))

for p in (
    os.path.join(VENV, "lib", "python3.13", "site-packages"),
    os.path.join(VENV, "lib64", "python3.13", "site-packages"),
):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

try:
    os.chdir(APP_ROOT)
except OSError:
    pass

# Singleton — LiteSpeed may import this module more than once in a process
application = None


def _build_app():
    from app import create_app
    return create_app()


try:
    application = _build_app()
except Exception:
    # Surface a static WSGI app instead of crash-respawn looping lswsgi
    _err = traceback.format_exc()
    try:
        err_path = os.path.join(APP_ROOT, "logs", "boot_error.log")
        os.makedirs(os.path.dirname(err_path), exist_ok=True)
        with open(err_path, "a", encoding="utf-8") as fh:
            fh.write(_err + "\n")
    except Exception:
        pass

    def application(environ, start_response):
        body = (
            b"OctaneFlow failed to start. Check logs/boot_error.log on the server.\n"
        )
        start_response(
            "503 Service Unavailable",
            [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]
