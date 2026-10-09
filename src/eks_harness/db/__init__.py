from eks_harness.db.connection import Database, open_connection
from eks_harness.db.migrations import current_version, migrate


def open_database(path) -> Database:
    database = Database(path)
    migrate(database.conn())
    return database


__all__ = ["Database", "current_version", "migrate", "open_connection", "open_database"]
