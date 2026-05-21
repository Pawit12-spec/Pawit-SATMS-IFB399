"""Sphinx configuration for the EQL Substation Monitoring docs."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

project = "EQL Substation Monitoring"
author = "BitWise Team"
copyright = (
    f"{datetime.utcnow():%Y}, {author}"
)  # UTC avoids timezone differences in reproducible builds.
release = os.environ.get("EQL_SATMS_VERSION", "0.1.0")
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.autosummary",
    "sphinx.ext.todo",
]

autosummary_generate = True
autodoc_member_order = "bysource"
todo_include_todos = True
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_use_param = True
napoleon_use_rtype = True

autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "inherited-members": False,
    "show-inheritance": False,
}

templates_path = ["_templates"]
exclude_patterns: list[str] = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_static_path = ["_static"]
html_title = "EQL SATMS"
