@ECHO OFF
TITLE Publ tests
ECHO Starting up Publ test site...

SET PORT=5000
SET FLASK_DEBUG=1
set FLASK_ENV=development
set FLASK_APP=test_app.py
poetry install
poetry run flask run

