#!/bin/sh


# Script to setup virtual environment and run flask development server.
# You can use this in place of "flask run" so the raspberry pi can send POST requests to the flask application.
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt

python3 run.py
