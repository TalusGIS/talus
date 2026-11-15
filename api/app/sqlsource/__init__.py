"""SQL-source pull — read-only copies of tables that OTHER systems own, inside Talus
(decision-sql-source-pull.md). Core (code-boundary §4): the data-access layer.

  dialects.py  — the dialect seam: per-database driver, catalog reader, identifier quoting,
                 geometry expression and type map. PostgreSQL is the first dialect (s1);
                 SQL Server, MySQL/MariaDB and Oracle are slots.
  service.py   — connections (admin-only, Fernet password, never-list), the catalog doors,
                 pulls (a source table → a hosted non-spatial table) and runs.
  worker.py    — the run loop: a fourth job class under its own advisory lock, beside
                 maintenance, batch geocoding and the bulk import.
"""
