"""Storage: every SQL statement GeoLogs runs is in this package."""
import sqlite3

from .database import Database, database_path
from .postgres import INTEGRITY_ERRORS as _POSTGRES_INTEGRITY_ERRORS

# A write that breaks a constraint (a duplicate name, a missing row), with either database.
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + _POSTGRES_INTEGRITY_ERRORS

__all__ = ["Database", "INTEGRITY_ERRORS", "database_path"]
