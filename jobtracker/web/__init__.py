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

    # Pull new Gmail job alerts every 10 minutes while the server is up
    # (no-op until Gmail is connected in Settings).
    from ..gmail_alerts import start_auto_fetch
    start_auto_fetch()
    from ..gmail_rejections import start_auto_fetch as start_rejections_fetch
    start_rejections_fetch()
    return app
