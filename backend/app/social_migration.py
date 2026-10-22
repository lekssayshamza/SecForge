"""Add only social associations; no existing account or scoring data is rewritten."""
from datetime import datetime
from sqlalchemy import inspect, select, text
from app.models import UserFollow, UserBlock, SchemaRevision
REVISION = '20261015_social_connections'

def migrate(c):
    if c.dialect.name not in ('postgresql', 'sqlite'):
        raise RuntimeError('Unsupported database')
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261015,3)'))
    for table in (UserFollow.__table__, UserBlock.__table__):
        table.create(c, checkfirst=True)
        inspector = inspect(c)
        if {x['name'] for x in inspector.get_columns(table.name)} != {x.name for x in table.columns}:
            raise RuntimeError('Incompatible social schema')
        if set(inspector.get_pk_constraint(table.name)['constrained_columns']) != {x.name for x in table.primary_key}:
            raise RuntimeError('Social uniqueness constraint missing')
        expected = {(fk.parent.name, fk.column.table.name, fk.column.name) for fk in table.foreign_keys}
        actual = {(fk['constrained_columns'][0], fk['referred_table'], fk['referred_columns'][0]) for fk in inspector.get_foreign_keys(table.name) if fk.get('options', {}).get('ondelete', '').upper() == 'CASCADE'}
        if actual != expected:
            raise RuntimeError('Social foreign keys incompatible')
        if not {x.name for x in table.constraints if getattr(x, 'sqltext', None) is not None} <= {x['name'] for x in inspector.get_check_constraints(table.name)}:
            raise RuntimeError('Social self-reference constraint missing')
        for index in table.indexes:
            index.create(c, checkfirst=True)
    SchemaRevision.__table__.create(c, checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
    return True
