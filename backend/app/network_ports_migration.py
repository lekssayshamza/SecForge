"""Add room port policy; old rows keep VPN SSH/HTTP. No cloud operations."""
from datetime import datetime
from sqlalchemy import JSON, inspect, select, text
from app.models import SchemaRevision

REVISION = '20261017_room_ports'
DEFAULT = '[{"protocol":"tcp","from_port":22,"to_port":22},{"protocol":"tcp","from_port":80,"to_port":80}]'


def migrate(c):
    if c.dialect.name not in ('postgresql', 'sqlite'):
        raise RuntimeError('Unsupported database')
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261017,1)'))
    columns = {v['name']: v for v in inspect(c).get_columns('rooms')}
    if 'exposed_ports' not in columns:
        # No SQL bind values embedded in the JSON literal (SQLAlchemy text parses colons).
        c.exec_driver_sql("ALTER TABLE rooms ADD COLUMN exposed_ports JSON NOT NULL DEFAULT '" + DEFAULT + "'")
    else:
        column = columns['exposed_ports']
        if column['nullable'] or not isinstance(column['type'], JSON):
            raise RuntimeError('Incompatible room port policy column')
    SchemaRevision.__table__.create(c, checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
    return True
