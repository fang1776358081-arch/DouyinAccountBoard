"""Synthetic-only migration rehearsal. Not a production migration entrypoint."""
import json
import os
import time
import uuid
from pathlib import Path
import psycopg
from psycopg import sql
from test_isolation import PROJECT, checked_root
from pg_schema import apply_migrations

MIGRATIONS = PROJECT / 'migrations/postgresql'
SCHEMA = 'board_candidate'
LEGACY_TABLES = ('computers', 'phone_devices', 'accounts', 'daily', 'imports', 'auth_users',
          'auth_sessions', 'auth_setup', 'auth_limits', 'auth_audit', 'auth_migrations',
          'phone_cards', 'collection_jobs', 'collection_settings', 'collection_worker',
          'publishing_layout', 'publishing_layout_state', 'publishing_layout_links',
          'operations_history', 'operations_previews', 'nurture_records', 'nurture_images')
WORKFLOW_TABLES = ('phone_units', 'device_cases', 'phone_card_placements',
                   'phone_account_access', 'device_workflow_events')
TABLES = LEGACY_TABLES + WORKFLOW_TABLES
IDENTITIES = ('auth_users', 'auth_audit', 'phone_cards', 'operations_history')


def connect_synthetic():
    root = checked_root()
    if any(os.environ.get(key) != value for key, value in {
        'PGHOST': '127.0.0.1', 'PGDATABASE': 'synthetic_probe', 'PGUSER': 'synthetic_owner'
    }.items()):
        raise ValueError('Explicit synthetic endpoint required')
    conn = psycopg.connect(autocommit=True, connect_timeout=5)
    try:
        if Path(conn.execute('SHOW data_directory').fetchone()[0]).resolve() != root / 'cluster':
            raise ValueError('Wrong synthetic cluster directory')
        if conn.execute('SELECT kind FROM board_app.environment').fetchone() != ('synthetic-only',):
            raise ValueError('Wrong synthetic cluster marker')
    except BaseException:
        conn.close()
        raise
    return conn


def install_schema(conn):
    return apply_migrations(conn)


def copy_synthetic(conn, source):
    """Caller supplies a freshly generated SQLite connection, never a file path.

    All tables retained for type-fidelity evaluation, including synthetic sessions.
    Production session invalidation/recovery classification is a different contract.
    """
    databases = source.execute('PRAGMA database_list').fetchall()
    if len(databases) != 1 or Path(databases[0][2]).resolve() != checked_root() / 'data' / 'board.db':
        raise ValueError('Only this run freshly generated SQLite source is allowed')
    if source.in_transaction:
        raise ValueError('Source must not have a pending transaction')
    source.execute('BEGIN')
    try:
        return _copy_snapshot(conn, source)
    finally:
        source.rollback()  # End read snapshot; never change the generated source.


def _copy_snapshot(conn, source):
    source_tables = reviewed_source_tables(source)
    with conn.transaction():
        conn.execute('SET LOCAL search_path=board_candidate,pg_catalog')
        conn.execute('SET CONSTRAINTS accounts_parent_account_id_fkey DEFERRED')
        conn.execute("SET LOCAL lock_timeout='5s'")
        control = conn.execute('SELECT mode FROM runtime_control WHERE id=1 FOR UPDATE').fetchone()
        if control != ('maintenance',):
            raise ValueError('Migration requires maintenance')
        for table in TABLES + ('offices','user_office_grants','account_offices','computer_offices','phone_card_offices','phone_device_offices'):
            if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                raise ValueError('Migration target must be empty')
        counts = {}
        for table in source_tables:
            cursor = source.execute('SELECT * FROM "' + table + '"')
            columns = [column[0] for column in cursor.description]
            extras = {'auth_users': ['authorization_version'], 'operations_history': ['scope_offices']}.get(table, [])
            rows = [tuple(row) for row in cursor.fetchall()]
            _write_verified_rows(conn, table, columns, rows, extras)
            counts[table] = len(rows)
        if source_tables == LEGACY_TABLES:
            # 0004 ran while the target was empty. Its backfill must happen
            # after legacy rows are copied, in this same verified transaction.
            counts.update(_backfill_legacy_workflow(conn, source))
        for table in IDENTITIES:
            maximum = conn.execute(sql.SQL('SELECT max(id) FROM {}').format(sql.Identifier(table))).fetchone()[0]
            # RESTART is transactional, unlike setval; failed rehearsals must not leak sequence changes.
            sequence = conn.execute('SELECT pg_get_serial_sequence(%s,%s)', (SCHEMA + '.' + table, 'id')).fetchone()[0]
            conn.execute(sql.SQL('ALTER SEQUENCE {} RESTART WITH {}').format(
                sql.Identifier(*sequence.split('.')), sql.Literal(max(1, (maximum or 0) + 1))))
    return counts


def _write_verified_rows(conn, table, columns, rows, extras=()):
    target_columns = [row[0] for row in conn.execute(
        'SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
        (SCHEMA, table))]
    if list(columns) + list(extras) != target_columns:
        raise ValueError('Column drift: ' + table)
    query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),
        sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
    with conn.cursor() as target:
        target.executemany(query, rows)
    # Compare full values, including bytes, NULL and empty strings, before commit.
    copied = conn.execute(sql.SQL('SELECT {} FROM {}').format(
        sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(table))).fetchall()
    if sorted(rows, key=repr) != sorted(copied, key=repr):
        raise ValueError('Field mismatch: ' + table)


def _backfill_legacy_workflow(conn, source):
    """Apply 0004's reviewed derivation after copying an audited 22-table source.

    Only explicit device assignments establish last-known physical identities.
    Card free text is never inferred; login is left unconfirmed. Returned counts
    include these derived target rows, not just literal rows in the old source.
    """
    columns = {
        'phone_units': ['id', 'device_id', 'original_device_id', 'generation', 'active',
                        'status', 'model', 'note', 'created_at', 'revision'],
        'device_cases': ['id', 'unit_id', 'status', 'reason', 'owner', 'followup_on', 'note',
                         'closure_reason', 'created_at', 'updated_at', 'closed_at', 'revision', 'account_ids'],
        'phone_card_placements': ['card_id', 'unit_id', 'slot', 'storage', 'updated_at', 'revision'],
        'phone_account_access': ['account_id', 'last_unit_id', 'target_unit_id', 'login_state', 'recovery_state',
                                 'reason', 'owner', 'followup_on', 'case_id', 'confirmed_at', 'revision'],
        'device_workflow_events': ['id', 'case_id', 'unit_id', 'card_id', 'account_id',
                                   'actor_id', 'actor_name', 'at', 'action', 'detail'],
    }
    now = int(time.time())
    devices = source.execute('SELECT id,state FROM phone_devices ORDER BY id').fetchall()
    known_devices = {row[0] for row in devices}
    expected = {table: [] for table in WORKFLOW_TABLES}
    expected['phone_units'] = [
        ('legacy-unit:' + ident, ident, ident, 1, 1, status, '',
         '由原设备编号建立，实物资料待核对', now, 0) for ident, status in devices]
    expected['phone_account_access'] = [
        (ident, 'legacy-unit:' + device_id, None, '待确认', '待处理', '', '', '', None, 0, 0)
        for ident, device_id in source.execute('SELECT id,phone_device_id FROM accounts ORDER BY id')
        if device_id in known_devices]
    counts = {}
    for table in WORKFLOW_TABLES:
        _write_verified_rows(conn, table, columns[table], expected[table])
        counts[table] = len(expected[table])
    return counts


def reviewed_source_tables(source):
    """Accept only a complete audited schema, including exact index SQL.

    The device workflow file is an explicit additive audit profile. Existing
    definitions must remain byte-for-byte equal to the immutable legacy review;
    unknown objects or altered columns never pass by being ignored or filtered.
    """
    actual = source.execute('SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name').fetchall()
    actual = [list(row) for row in actual]
    legacy = json.loads((MIGRATIONS / 'legacy-sqlite-schema.json').read_text(encoding='utf-8-sig'))
    if actual == legacy:
        return LEGACY_TABLES
    additions = json.loads((MIGRATIONS / 'device-workflow-sqlite-schema.json').read_text(encoding='utf-8-sig'))
    if actual == sorted(legacy + additions, key=lambda row: (row[0], row[1])):
        return TABLES
    raise ValueError('Unreviewed SQLite schema; migration refused')


def first_cutover_synthetic(conn, source):
    """First-switch policy, distinct from field-fidelity and same-DB restart.

    Keep credentials and history; revoke sessions, previews and worker execution.
    Changes remain in the same transaction as the verified copy.
    """
    with conn.transaction():
        counts = copy_synthetic(conn, source)
        conn.execute('DELETE FROM board_candidate.auth_sessions')
        conn.execute('DELETE FROM board_candidate.auth_setup')
        conn.execute('DELETE FROM board_candidate.operations_previews')
        conn.execute("UPDATE board_candidate.collection_jobs SET token=''")
        conn.execute("UPDATE board_candidate.collection_worker SET seen=0,status='',command=''")
        conn.execute("UPDATE board_candidate.collection_settings SET auto_enabled=0,paused=1,pause_reason='initial-cutover'")
        generation = str(uuid.uuid4())
        conn.execute("UPDATE board_candidate.runtime_control SET mode='maintenance',restore_generation=%s WHERE id=1", (generation,))
    return dict(source_counts=counts, removed_sessions=counts['auth_sessions'],
                removed_setup_tokens=counts['auth_setup'], removed_previews=counts['operations_previews'],
                invalidated_worker_tokens=counts['collection_jobs'], restore_generation= generation, mode='maintenance')
