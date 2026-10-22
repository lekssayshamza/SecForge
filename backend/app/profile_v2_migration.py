"""Add nullable identity fields; no account, score or auth changes."""
from datetime import datetime
from sqlalchemy import inspect, select, String, text
from app.models import SchemaRevision
REVISION = '20261013_profiles_v2'

def migrate(connection):
    if connection.dialect.name not in ('sqlite','postgresql'):raise RuntimeError('Unsupported profile database')
    if connection.dialect.name=='postgresql':
        connection.execute(text("SET LOCAL lock_timeout='5s'"))
        connection.execute(text("SET LOCAL statement_timeout='30s'"))
        connection.execute(text('SELECT pg_advisory_xact_lock(20261013,4)'))
        connection.execute(text('LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE'))
    columns={c['name']:c for c in inspect(connection).get_columns('users')}
    for name,size in [('bio',280),('avatar_version',32)]:
        if name in columns:
            c=columns[name]
            if not c['nullable'] or not isinstance(c['type'],String) or c['type'].length!=size:raise RuntimeError('Incompatible profile field; migration refused')
        else:connection.execute(text(f'ALTER TABLE users ADD COLUMN {name} VARCHAR({size})'))
    SchemaRevision.__table__.create(connection,checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)) is None:
        connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
    return False
