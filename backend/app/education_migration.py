"""Standalone additive migration, importable before the new application is deployed."""
from datetime import datetime
from sqlalchemy import inspect,select,text,String,Integer,DateTime
from app.models import SchemaRevision
REVISION='20261014_education_paths'
def migrate(c):
    dialect=c.dialect.name
    if dialect not in ('sqlite','postgresql'):raise RuntimeError('Unsupported education database')
    if dialect=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261014,7)'))
    if not inspect(c).has_table('rooms'):raise RuntimeError('Existing rooms required')
    cols={x['name']:x for x in inspect(c).get_columns('rooms')}
    for name,ddl in [('room_type',"VARCHAR(20) NOT NULL DEFAULT 'PRACTICAL' CHECK(room_type IN ('THEORY','PRACTICAL'))"),('objectives',"VARCHAR(2000) NOT NULL DEFAULT ''"),('prerequisites',"VARCHAR(2000) NOT NULL DEFAULT ''")]:
        if name not in cols:c.execute(text('ALTER TABLE rooms ADD COLUMN '+name+' '+ddl))
        elif cols[name]['nullable']:raise RuntimeError('Incompatible education columns')
    key='SERIAL PRIMARY KEY' if dialect=='postgresql' else 'INTEGER NOT NULL PRIMARY KEY'
    if not inspect(c).has_table('learning_paths'):
        c.execute(text('CREATE TABLE learning_paths (id '+key+''',creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,title VARCHAR(120) NOT NULL,description VARCHAR(2000) NOT NULL,difficulty VARCHAR(20) NOT NULL,category VARCHAR(50) NOT NULL,objectives VARCHAR(2000) NOT NULL,status VARCHAR(20) NOT NULL,created_at TIMESTAMP NOT NULL,updated_at TIMESTAMP NOT NULL,CONSTRAINT ck_path_status CHECK(status IN ('draft','published')))'''))
    if not inspect(c).has_table('learning_path_rooms'):
        c.execute(text('''CREATE TABLE learning_path_rooms (path_id INTEGER NOT NULL REFERENCES learning_paths(id) ON DELETE CASCADE,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,display_order INTEGER NOT NULL,PRIMARY KEY(path_id,room_id),CONSTRAINT uq_path_room_order UNIQUE(path_id,display_order),CONSTRAINT ck_path_room_order CHECK(display_order>0))'''))
    current={x['name']:x for x in inspect(c).get_columns('rooms')}
    for name,length in [('room_type',20),('objectives',2000),('prerequisites',2000)]:
        col=current[name]
        if col['nullable'] or not isinstance(col['type'],String) or col['type'].length!=length:raise RuntimeError('Incompatible education column')
    if c.scalar(text("SELECT count(*) FROM rooms WHERE room_type NOT IN ('THEORY','PRACTICAL')")):raise RuntimeError('Invalid existing room types')
    if not any('room_type' in x['sqltext'] and 'THEORY' in x['sqltext'] and 'PRACTICAL' in x['sqltext'] for x in inspect(c).get_check_constraints('rooms')):raise RuntimeError('Missing room type constraint')
    expected={'learning_paths':{'id':Integer(),'creator_id':Integer(),'title':String(120),'description':String(2000),'difficulty':String(20),'category':String(50),'objectives':String(2000),'status':String(20),'created_at':DateTime(),'updated_at':DateTime()},'learning_path_rooms':{'path_id':Integer(),'room_id':Integer(),'display_order':Integer()}}
    for table,columns in expected.items():
        actual={x['name']:x for x in inspect(c).get_columns(table)}
        for name,kind in columns.items():
            a=actual.get(name)
            if a is None or a['nullable'] or not isinstance(a['type'],type(kind)) or isinstance(kind,String) and a['type'].length!=kind.length:raise RuntimeError('Incompatible path schema')
        required={('creator_id','users','id')} if table=='learning_paths' else {('path_id','learning_paths','id'),('room_id','rooms','id')}
        fks={(f['constrained_columns'][0],f['referred_table'],f['referred_columns'][0]) for f in inspect(c).get_foreign_keys(table) if len(f['constrained_columns'])==1 and f['options'].get('ondelete')=='CASCADE'}
        if dialect=='sqlite':
            # SQLite's SQLAlchemy reflection omits inline ON DELETE options;
            # verify the engine's actual FK actions instead of weakening checks.
            raw=c.execute(text('PRAGMA foreign_key_list('+table+')')).mappings().all()
            fks={(f['from'],f['table'],f['to']) for f in raw if f['on_delete']=='CASCADE' and sum(x['id']==f['id'] for x in raw)==1}
        if not required.issubset(fks):raise RuntimeError('Incompatible path references')
    if not any(x['column_names']==['path_id','display_order'] for x in inspect(c).get_unique_constraints('learning_path_rooms')):raise RuntimeError('Missing path order constraint')
    if inspect(c).get_pk_constraint('learning_path_rooms')['constrained_columns']!=['path_id','room_id']:raise RuntimeError('Missing path membership identity')
    for table,name,columns in [('learning_paths','ix_learning_paths_creator_id',['creator_id']),('learning_paths','ix_paths_status_id',['status','id']),('learning_path_rooms','ix_learning_path_rooms_room_id',['room_id'])]:
        existing=next((i for i in inspect(c).get_indexes(table) if i['name']==name),None)
        if existing:
            if existing['column_names']!=columns:raise RuntimeError('Incompatible path index')
        else:c.execute(text('CREATE INDEX '+name+' ON '+table+'('+','.join(columns)+')'))
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
