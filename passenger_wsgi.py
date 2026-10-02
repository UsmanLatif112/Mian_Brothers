import os
import sys

VENV = "/home1/mygymlahore/virtualenv/octaneflow.udottechnologies.com/3.13"
INTERP = os.path.join(VENV, "bin", "python")

# Prefer venv interpreter when possible
if os.path.exists(INTERP) and sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Always expose venv packages (LiteSpeed often ignores execl)
for p in (
    os.path.join(VENV, "lib", "python3.13", "site-packages"),
    os.path.join(VENV, "lib64", "python3.13", "site-packages"),
):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

APP_ROOT = os.path.dirname(__file__)
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from run import app as application
