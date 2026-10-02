import os
import sys


# cPanel / Passenger: use this app's Python 3.10 virtualenv
INTERP = "/home/mygymlahore/virtualenv/mianbrother.usmanlateef.com/3.10/bin/python"
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Application root on the server
APP_ROOT = os.path.dirname(__file__)
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

# Flask app callable expected by Passenger (Application Entry point: application)
from run import app as application
