"""Test session configuration.

Ensures a placeholder ``DATABASE_URL`` exists before any application settings
are instantiated (real infrastructure is never contacted by these tests).
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
os.environ.setdefault("APP_ENV", "local")
