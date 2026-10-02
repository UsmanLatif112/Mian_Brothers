import os
import sys

# cPanel LiteSpeed: do NOT os.execl (venv python symlink often breaks).
# Inject the app virtualenv site-packages instead.
VENV = "/home1/mygymlahore/virtualenv/octaneflow.udottechnologies.com/3.13"

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
