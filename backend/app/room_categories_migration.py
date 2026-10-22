"""Add category associations without rewriting content, drafts or release history."""
from datetime import datetime
from sqlalchemy import inspect,select,text
from app.models import RoomCategory,SchemaRevision
from app.categories import SLUGS
REVISION='20261016_room_categories'

def migrate(c):
    if c.dialect.name not in ('postgresql','sqlite'):raise RuntimeError('Unsupported database')
    if c.dialect.name=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261016,3)'))
    if not inspect(c).has_table('rooms'):raise RuntimeError('Existing room schema required')
    if any(value not in SLUGS for value in c.scalars(text('SELECT DISTINCT category FROM rooms'))):raise RuntimeError('Review noncanonical existing categories before migration')
    table=RoomCategory.__table__;table.create(c,checkfirst=True);i=inspect(c)
    cols={x['name']:x for x in i.get_columns(table.name)}
    if set(cols)!={'room_id','category'} or any(x['nullable'] for x in cols.values()) or set(i.get_pk_constraint(table.name)['constrained_columns'])!={'room_id','category'}:raise RuntimeError('Incompatible room category schema')
    if not any(f['constrained_columns']==['room_id'] and f['referred_table']=='rooms' and f['referred_columns']==['id'] and f.get('options',{}).get('ondelete')=='CASCADE' for f in i.get_foreign_keys(table.name)):raise RuntimeError('Invalid category ownership constraint')
    for index in table.indexes:
        index.create(c,checkfirst=True)
        actual=next(x for x in inspect(c).get_indexes(table.name) if x['name']==index.name)
        if actual['column_names']!=['category','room_id'] or actual['unique']:raise RuntimeError('Incompatible category lookup index')
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(text('INSERT INTO room_categories (room_id,category) SELECT r.id,r.category FROM rooms r WHERE NOT EXISTS (SELECT 1 FROM room_categories c WHERE c.room_id=r.id AND c.category=r.category)'))
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
