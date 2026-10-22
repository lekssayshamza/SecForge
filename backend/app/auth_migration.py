"""Additive authentication migration; no connection/initialization on import."""
from datetime import datetime
from sqlalchemy import inspect, select, text
from app.models import GoogleIdentity, OAuthAttempt, SchemaRevision
from app.profile_migration import valid_index

REVISION = '20261012_authentication_v2'
COLUMNS = {'password_enabled': 'BOOLEAN NOT NULL DEFAULT TRUE',
           'must_change_password': 'BOOLEAN NOT NULL DEFAULT FALSE',
           'token_version': 'INTEGER NOT NULL DEFAULT 0'}

def migrate(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise RuntimeError('Unsupported authentication migration database')
    if connection.dialect.name == 'postgresql':
        connection.execute(text("SET LOCAL lock_timeout='5s'"))
        connection.execute(text("SET LOCAL statement_timeout='30s'"))
        connection.execute(text('SELECT pg_advisory_xact_lock(20261012, 2)'))
        connection.execute(text('LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE'))
    if not inspect(connection).has_table('users'):
        raise RuntimeError('Existing users schema is required')
    duplicates = connection.scalar(text('SELECT count(*) FROM (SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*)>1) collisions'))
    if duplicates:
        raise RuntimeError('Resolve case-insensitive email collisions before migration; accounts were not changed')
    columns = {c['name']: c for c in inspect(connection).get_columns('users')}
    for name in COLUMNS:
        if name in columns and columns[name]['nullable']:
            raise RuntimeError('Incompatible existing authentication column')
    # Check uniqueness structure before any DDL. Do not silently accept a partial index.
    if connection.dialect.name == 'postgresql':
        definition = connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname='uq_users_email_ci'"))
    else:
        definition = connection.scalar(text("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_users_email_ci'"))
    if definition and not valid_index(definition.replace('email', 'username')):
        raise RuntimeError('Incompatible email uniqueness index')
    for name, ddl in COLUMNS.items():
        if name not in columns: connection.execute(text(f'ALTER TABLE users ADD COLUMN {name} {ddl}'))
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_ci ON users(lower(email))'))
    for table in [GoogleIdentity.__table__, OAuthAttempt.__table__, SchemaRevision.__table__]:
        table.create(connection, checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)) is None:
        connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
        return True
    return False
