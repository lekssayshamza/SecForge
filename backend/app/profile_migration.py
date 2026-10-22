"""Versioned additive profile migration. No database operations on import."""
from datetime import datetime
import re
from sqlalchemy import String, inspect, select, text
from app.models import SchemaRevision

REVISION = '20261011_user_profiles_v1'
INDEX = 'uq_users_username_ci'
COLUMNS = {
    'display_name': ('VARCHAR(80)', True),
    'avatar_style': ("VARCHAR(20) NOT NULL DEFAULT 'initials'", False),
    'avatar_color': ("VARCHAR(20) NOT NULL DEFAULT 'red'", False),
}


def index_definition(connection):
    if connection.dialect.name == 'postgresql':
        return connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename='users' AND indexname=:name"), {'name': INDEX})
    return connection.scalar(text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='users' AND name=:name"), {'name': INDEX})


def valid_index(definition):
    return bool(definition and ',' not in definition and not re.search(r'\bWHERE\b', definition, re.I) and re.search(r'CREATE\s+UNIQUE\s+INDEX', definition, re.I) and re.search(r'lower\(\s*\(?\s*"?username"?\s*\)?\s*(?:::text)?\s*\)', definition, re.I))


def migrate(connection):
    dialect = connection.dialect.name
    if dialect not in ('postgresql', 'sqlite'):
        raise RuntimeError('Unsupported profile migration database.')
    if dialect == 'postgresql':
        connection.execute(text('SET LOCAL lock_timeout = \'5s\''))
        connection.execute(text('SET LOCAL statement_timeout = \'30s\''))
        connection.execute(text('SELECT pg_advisory_xact_lock(20261011, 2)'))
    inspector = inspect(connection)
    if not inspector.has_table('users'):
        raise RuntimeError('Existing application users are required; initialization is not performed.')
    # Serialize against concurrent registration/renames while checking/building
    # the unique index; timeouts prevent an unbounded maintenance stall.
    if dialect == 'postgresql':
        connection.execute(text('LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE'))
    columns = {c['name']: c for c in inspector.get_columns('users')}
    if not {'id', 'username', 'email', 'role', 'password_hash'}.issubset(columns):
        raise RuntimeError('Incompatible existing users schema; no data was reset.')
    for name, (_, nullable) in COLUMNS.items():
        if name in columns and (columns[name]['nullable'] != nullable or not isinstance(columns[name]['type'], String) or columns[name]['type'].length != (80 if name == 'display_name' else 20)):
            raise RuntimeError('Incompatible existing profile column; migration refused.')
    duplicates = connection.scalar(text('SELECT count(*) FROM (SELECT lower(username) FROM users GROUP BY lower(username) HAVING count(*) > 1) AS collisions'))
    if duplicates:
        # Do not print account names or automatically rename existing users.
        raise RuntimeError('Case-insensitive username collisions require operator resolution; no accounts changed.')
    definition = index_definition(connection)
    if definition and not valid_index(definition):
        raise RuntimeError('Incompatible username index; migration refused.')
    for name, (definition, _) in COLUMNS.items():
        if name not in columns:
            connection.execute(text(f'ALTER TABLE users ADD COLUMN {name} {definition}'))
    if index_definition(connection) is None:
        connection.execute(text(f'CREATE UNIQUE INDEX {INDEX} ON users (lower(username))'))
    if not valid_index(index_definition(connection)):
        raise RuntimeError('Username uniqueness could not be verified.')
    SchemaRevision.__table__.create(connection, checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)) is None:
        connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
        return True
    return False
