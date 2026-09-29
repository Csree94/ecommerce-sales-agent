"""Test session configuration.

Ensures a placeholder ``DATABASE_URL`` exists before any application settings
are instantiated (real infrastructure is never contacted by these tests).
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
os.environ.setdefault("APP_ENV", "local")

# LLM providers are NEVER contacted by tests: pin the provider keys to empty
# (overriding any developer-local values) so graph tests resolve the
# fail-fast UnconfiguredProvider path — no network, no real API calls.
os.environ["GEMINI_API_KEY"] = ""
os.environ["FALLBACK_LLM_API_KEY"] = ""
