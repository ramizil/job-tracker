"""jobtracker - a personal job-application tracker with resume-aware job search.

Modules:
    config     - paths and environment configuration
    db         - SQLite connection and schema
    models     - status / rejection vocabularies
    resume     - parse an HTML CV into a matching profile
    matcher    - score a job posting against the resume profile
    tracker    - application CRUD + status history + rejection logging
    analytics  - funnel, rejection and source analysis
    sources/   - job-board aggregator clients (JSearch, Jooble, Adzuna)
    cli        - command-line interface
"""

__version__ = "0.1.0"

# Use the OS (Windows) certificate store for TLS. This lets HTTPS work behind
# corporate proxies that do TLS interception with a self-signed root that is
# trusted by Windows but not by Python's bundled CA list.
try:  # pragma: no cover - environment dependent
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:
    pass
