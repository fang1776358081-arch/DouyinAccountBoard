"""Explicit, ordered schema migrations. Invocation is controlled, never startup."""
import hashlib
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / 'migrations/postgresql'
FILES = ('0001_legacy_baseline.sql', '0002_office_scope.sql', '0003_idempotency.sql', '0004_device_workflow.sql',
         '0005_employee_cards.sql', '0006_publish_review.sql')


def expected_versions():
    return [(index, hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest()) for index, name in enumerate(FILES, 1)]


def runtime_versions_supported(rows):
    """Complete reviewed v4/v5/v6 schemas only; extensions stay disabled until applied.

    Hashes of every installed migration must match. Startup never applies DDL.
    """
    expected = expected_versions()
    return list(rows) in (expected[:4], expected[:5], expected)


def validate_extensions(db, rows):
    """Fail startup if a recorded extension lacks its reviewed tables."""
    installed = len(rows)
    if installed >= 5:
        import employee_cards
        if not employee_cards.available(db):
            raise ValueError('Employee-card schema incomplete')
    if installed >= 6:
        import publish_review
        if not publish_review.available(db):
            raise ValueError('Publish-review schema incomplete')



def apply_migrations(connection):
    expected = expected_versions()
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute('SELECT pg_advisory_xact_lock(811032001)')
        exists = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='board_candidate'").fetchone()
        if not exists:
            connection.execute('CREATE SCHEMA board_candidate')
            connection.execute('CREATE TABLE board_candidate.schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
        current = connection.execute('SELECT version,sha256 FROM board_candidate.schema_migrations ORDER BY version').fetchall()
        if current != expected[:len(current)] or len(current) > len(expected):
            raise ValueError('Migration checksum/version mismatch; never overwrite')
        if current == expected:
            return 'already-applied'
        if current:
            mode = connection.execute('SELECT mode FROM board_candidate.runtime_control WHERE id=1 FOR UPDATE').fetchone()
            if mode != ('maintenance',):
                raise ValueError('Schema migration requires maintenance')
        connection.execute('SET LOCAL search_path=board_candidate,pg_catalog')
        for version, digest in expected[len(current):]:
            connection.execute((MIGRATIONS / FILES[version-1]).read_text(encoding='utf-8'))
            connection.execute('INSERT INTO schema_migrations VALUES(%s,%s)', (version, digest))
    return 'applied'
