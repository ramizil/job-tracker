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
