"""Run script for the EQL SATMS Flask application.

Use this command from the repository root:

    python "EQL SATMS/run.py"

This script avoids the name collision between a top-level module named
`app.py` and the `app` package directory.
"""

import os
import importlib
import sys
from dotenv import load_dotenv

# Load .env file from the same directory as run.py
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Ensure the EQL SATMS directory is on sys.path
pkg_dir = os.path.dirname(__file__)
if pkg_dir not in sys.path:
    sys.path.insert(0, pkg_dir)

app_pkg = importlib.import_module("app")
create_app = getattr(app_pkg, "create_app")

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
