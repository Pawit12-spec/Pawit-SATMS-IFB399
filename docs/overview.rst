Overview
========

Purpose
-------

The EQL Substation Monitoring system provides a Flask-based dashboard for
collecting InfluxDB time-series data, triggering isolation-forest anomaly
alerts, and relaying notifications via SMS and email integrations.

Local Development
-----------------

1. Create a virtual environment and install dependencies::

      python -m venv .venv
      source .venv/bin/activate
      pip install -r requirements.txt

2. Provide application settings via ``.env`` (see ``app/__init__.py`` for the
   configuration keys that are read).

3. Start the development server::

      flask --app app:create_app run --debug

Testing
-------

The repository includes a ``tests`` directory with pytest suites. Run::

    pytest

Documentation
-------------

Inside ``docs/`` you will find the Sphinx project, which can be built locally
with::

    cd docs
    make html

HTML files are placed in ``docs/_build/html`` and can be opened in a browser.

