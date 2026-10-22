"""Opt-in profile visibility; never publish an existing image or bio."""
from datetime import datetime
from sqlalchemy import Boolean, inspect, select, text
from app.models import SchemaRevision

REVISION = '20261015_profile_privacy'

def migrate(c):
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261015,1)'))
    elif c.dialect.name != 'sqlite':
        raise RuntimeError('Unsupported database')
    columns = {item['name']: item for item in inspect(c).get_columns('users')}
    for name in ('avatar_public', 'bio_public'):
        if name not in columns:
            c.execute(text(f'ALTER TABLE users ADD COLUMN {name} BOOLEAN NOT NULL DEFAULT FALSE'))
        elif columns[name]['nullable'] or not isinstance(columns[name]['type'], Boolean):
            raise RuntimeError('Incompatible profile privacy field')
    SchemaRevision.__table__.create(c, checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
    return True
