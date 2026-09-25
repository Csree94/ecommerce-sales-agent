"""Environment constants for the app config."""

from enum import StrEnum


class Environment(StrEnum):
    """Deployment environments."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
