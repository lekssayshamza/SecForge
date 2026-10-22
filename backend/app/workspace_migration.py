"""Additive creator drafts/releases; rerunnable without rewriting live content."""
from datetime import datetime
from sqlalchemy import inspect, select, text, Integer, String
from app.models import RoomDraft, RoomRelease, SchemaRevision

REVISION = '20261015_creator_workspace'


def migrate(c):
    if c.dialect.name not in ('postgresql', 'sqlite'):
        raise RuntimeError('Unsupported database')
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261015,2)'))
    for table in (RoomRelease.__table__, RoomDraft.__table__):
        table.create(c, checkfirst=True)
        actual = {x['name'] for x in inspect(c).get_columns(table.name)}
        if actual != {x.name for x in table.columns}:
            raise RuntimeError('Incompatible workspace schema')
        for index in table.indexes:
            index.create(c, checkfirst=True)
    fields = {
        'builder_environments': {'source_ami_id': 'VARCHAR(64)', 'ssh_auth_mode': "VARCHAR(16) NOT NULL DEFAULT 'key'", 'ssh_guest_username': 'VARCHAR(32)'},
        'question_solves': {'release_id': 'INTEGER REFERENCES room_releases(id) ON DELETE SET NULL'},
        'room_progress': {'completed_release_id': 'INTEGER REFERENCES room_releases(id) ON DELETE SET NULL'},
    }
    for table, columns in fields.items():
        existing = {x['name'] for x in inspect(c).get_columns(table)}
        for name, definition in columns.items():
            if name not in existing:
                c.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
        actual = {x['name']: x for x in inspect(c).get_columns(table)}
        for name in columns:
            col = actual[name]
            kind = String if name in ('source_ami_id', 'ssh_auth_mode', 'ssh_guest_username') else Integer
            if not isinstance(col['type'], kind) or col['nullable'] != (name != 'ssh_auth_mode'):
                raise RuntimeError('Incompatible workspace field')
    SchemaRevision.__table__.create(c, checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
    return True
