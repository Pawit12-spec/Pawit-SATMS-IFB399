"""
Run script for the EQL SATMS Flask application.

Run this file from the repository root with:

    python "EQL SATMS/app.py"

When executed, the directory containing this script is added to sys.path so
`from app import create_app` will import `EQL SATMS/app` package.
"""
from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Use host 0.0.0.0 to be reachable on the local network if desired.
    app.run(host="0.0.0.0", port=5050, debug=True)
