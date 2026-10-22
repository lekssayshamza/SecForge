"""Retained creator capability; no promotion or data reset during migration."""
from datetime import datetime
from sqlalchemy import inspect,text,select,Boolean
from app.models import SchemaRevision
REVISION='20261014_readiness'
def migrate(c):
    if c.dialect.name=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261014,10)'))
    elif c.dialect.name!='sqlite':raise RuntimeError('Unsupported database')
    columns={x['name']:x for x in inspect(c).get_columns('users')}
    if 'creator_access' not in columns:c.execute(text('ALTER TABLE users ADD COLUMN creator_access BOOLEAN NOT NULL DEFAULT FALSE'))
    elif columns['creator_access']['nullable'] or not isinstance(columns['creator_access']['type'],Boolean):raise RuntimeError('Incompatible creator capability')
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
