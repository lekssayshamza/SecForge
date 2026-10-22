"""Add private lesson images and release eligibility; never rewrite room content."""
from datetime import datetime
from sqlalchemy import inspect, select, text
from app.models import RoomMedia, RoomReleaseMedia, SchemaRevision
REVISION='20261016_room_media'

def migrate(c):
    if c.dialect.name not in ('postgresql','sqlite'):raise RuntimeError('Unsupported database')
    if c.dialect.name=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261016,1)'))
    if not {'rooms','room_releases'}<=set(inspect(c).get_table_names()):raise RuntimeError('Existing room schema required')
    for table in (RoomMedia.__table__,RoomReleaseMedia.__table__):
        table.create(c,checkfirst=True)
        i=inspect(c)
        columns={x['name']:x for x in i.get_columns(table.name)}
        if set(columns)!={x.name for x in table.columns}:raise RuntimeError('Incompatible lesson image schema')
        for column in table.columns:
            actual_column=columns[column.name]
            if actual_column['nullable'] != column.nullable or actual_column['type']._type_affinity != column.type._type_affinity or getattr(actual_column['type'],'length',None) != getattr(column.type,'length',None):
                raise RuntimeError('Incompatible image column definition')
        if set(i.get_pk_constraint(table.name)['constrained_columns'])!={x.name for x in table.primary_key}:raise RuntimeError('Image identity constraint missing')
        expected={(fk.parent.name,fk.column.table.name,fk.column.name) for fk in table.foreign_keys}
        actual={(f['constrained_columns'][0],f['referred_table'],f['referred_columns'][0]) for f in i.get_foreign_keys(table.name) if f.get('options',{}).get('ondelete','').upper()=='CASCADE'}
        if actual!=expected:raise RuntimeError('Incompatible image foreign keys')
        if not {x.name for x in table.constraints if getattr(x,'sqltext',None) is not None}<={x['name'] for x in i.get_check_constraints(table.name)}:raise RuntimeError('Image validation constraints missing')
        for index in table.indexes:
            index.create(c,checkfirst=True)
            actual_index=next(x for x in inspect(c).get_indexes(table.name) if x['name']==index.name)
            if actual_index['column_names'] != [x.name for x in index.columns] or bool(actual_index['unique']) != bool(index.unique):raise RuntimeError('Incompatible image index')
    uniques={tuple(x['column_names']) for x in inspect(c).get_unique_constraints('room_media')}
    if ('room_id','content_digest') not in uniques:raise RuntimeError('Image deduplication constraint missing')
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()))
    return True
