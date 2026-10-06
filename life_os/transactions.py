"""Compose existing committing APIs within one caller-owned transaction."""


class DeferredConnection:
    """Defer commits and context-manager exits to the enclosing transaction."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False
