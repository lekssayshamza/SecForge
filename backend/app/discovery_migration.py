"""Auditable category rewrite and private bookmarks; no content or award deletion."""
from datetime import datetime
from sqlalchemy import inspect,select,text,String,Integer,DateTime,Text
from app.models import SchemaRevision
from app.categories import legacy_category
REVISION='20261014_discovery_bookmarks'
def migrate(c):
    dialect=c.dialect.name
    if dialect not in ('postgresql','sqlite'):raise RuntimeError('Unsupported discovery database')
    if dialect=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261014,8)'))
    for table in ['rooms','learning_paths']:
        columns={x['name']:x for x in inspect(c).get_columns(table)}
        if 'category_original' not in columns:c.execute(text('ALTER TABLE '+table+' ADD COLUMN category_original TEXT'))
        elif not columns['category_original']['nullable'] or not isinstance(columns['category_original']['type'],String):raise RuntimeError('Incompatible category audit column')
        for row in c.execute(text('SELECT id,category,category_original FROM '+table)).mappings().all():
            canonical=legacy_category(row['category'])
            if canonical!=row['category']:
                c.execute(text('UPDATE '+table+' SET category=:canonical,category_original=COALESCE(category_original,:original) WHERE id=:id'),dict(canonical=canonical,original=row['category'],id=row['id']))
    for table,target,key in [('room_bookmarks','rooms','room_id'),('path_bookmarks','learning_paths','path_id')]:
        if not inspect(c).has_table(table):c.execute(text('CREATE TABLE '+table+' (\nuser_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,\n'+key+' INTEGER NOT NULL REFERENCES '+target+'(id) ON DELETE CASCADE,\ncreated_at TIMESTAMP NOT NULL,\nPRIMARY KEY(user_id,'+key+'))'))
        cols={x['name']:x for x in inspect(c).get_columns(table)}
        if set(cols)!={'user_id',key,'created_at'} or any(x['nullable'] for x in cols.values()) or not isinstance(cols['user_id']['type'],Integer) or not isinstance(cols[key]['type'],Integer) or not isinstance(cols['created_at']['type'],DateTime):raise RuntimeError('Incompatible bookmark schema')
        if inspect(c).get_pk_constraint(table)['constrained_columns']!=['user_id',key]:raise RuntimeError('Missing bookmark uniqueness')
        if dialect=='sqlite':
            fks={(r['from'],r['table'],r['to']) for r in c.execute(text('PRAGMA foreign_key_list('+table+')')).mappings() if r['on_delete']=='CASCADE'}
        else:fks={(r['constrained_columns'][0],r['referred_table'],r['referred_columns'][0]) for r in inspect(c).get_foreign_keys(table) if len(r['constrained_columns'])==1 and r['options'].get('ondelete')=='CASCADE'}
        if not {('user_id','users','id'),(key,target,'id')}.issubset(fks):raise RuntimeError('Invalid bookmark references')
    indexes=[('rooms','ix_rooms_discovery_filter',['status','category','difficulty','id']),('rooms','ix_rooms_discovery_type',['status','room_type','id']),('rooms','ix_rooms_discovery_newest',['status','created_at','id']),('learning_paths','ix_paths_discovery_filter',['status','category','difficulty','id']),('learning_paths','ix_paths_discovery_newest',['status','created_at','id']),('room_bookmarks','ix_room_bookmarks_room_id',['room_id']),('path_bookmarks','ix_path_bookmarks_path_id',['path_id'])]
    for table,name,columns in indexes:
        old=next((x for x in inspect(c).get_indexes(table) if x['name']==name),None)
        if old:
            if old['column_names']!=columns:raise RuntimeError('Incompatible discovery index')
        else:c.execute(text('CREATE INDEX '+name+' ON '+table+'('+','.join(columns)+')'))
    for table,name,title in [('rooms','ix_rooms_discovery_name','name'),('learning_paths','ix_paths_discovery_title','title')]:
        c.execute(text('CREATE INDEX IF NOT EXISTS '+name+' ON '+table+' (status,lower('+title+'),id)'))
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
