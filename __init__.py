"""Agent companion: an optional dashboard API, without runtime patches or hooks."""

__version__ = "0.3.0"


def register(ctx):
    """The dashboard manifest supplies the API; no agent behavior is registered."""
    return None
