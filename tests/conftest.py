import sys
from pathlib import Path

# add project root to PYTHONPATH
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
from app import create_app


@pytest.fixture()
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "UPLOAD_DIR": str(tmp_path / "uploads"),
    })
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()
