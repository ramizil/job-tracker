"""Flask web dashboard factory."""
from __future__ import annotations

from flask import Flask

from ..db import ensure_schema


def create_app() -> Flask:
    ensure_schema()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "jobtracker-local-dev"
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    from .views import bp, md_to_html
    app.register_blueprint(bp)
    app.jinja_env.filters["md"] = md_to_html
    return app
