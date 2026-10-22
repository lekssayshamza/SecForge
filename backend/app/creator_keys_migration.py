"""Public-key registry only; no account, guest, AMI or infrastructure mutation."""
from datetime import datetime
from sqlalchemy import inspect,select,text
from app.models import CreatorSshKey,CreatorBuilderKey,SchemaRevision
REVISION='20261016_creator_keys'
def migrate(c):
    if c.dialect.name not in ('postgresql','sqlite'):raise RuntimeError('Unsupported database')
    if c.dialect.name=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261016,5)'))
    if not {'users','builder_environments'}<=set(inspect(c).get_table_names()):raise RuntimeError('Existing account/builder schema required')
    table=CreatorSshKey.__table__;table.create(c,checkfirst=True);i=inspect(c)
    columns={v['name']:v for v in i.get_columns(table.name)}
    if set(columns)!={v.name for v in table.columns} or i.get_pk_constraint(table.name)['constrained_columns']!=['user_id']:raise RuntimeError('Incompatible creator key registry')
    for col in table.columns:
        actual=columns[col.name]
        if actual['nullable']!=col.nullable or actual['type']._type_affinity!=col.type._type_affinity or getattr(actual['type'],'length',None)!=getattr(col.type,'length',None):raise RuntimeError('Incompatible creator key column')
    if not any(v['column_names']==['fingerprint'] for v in i.get_unique_constraints(table.name)):raise RuntimeError('Missing creator key uniqueness')
    foreign={(v['constrained_columns'][0],v['referred_table'],v['referred_columns'][0],v.get('options',{}).get('ondelete','').upper()) for v in i.get_foreign_keys(table.name)}
    if foreign!={('user_id','users','id','CASCADE'),('applied_builder_id','builder_environments','id','SET NULL')}:raise RuntimeError('Invalid key registry references')
    if 'ck_creator_ssh_key_revision' not in {v['name'] for v in i.get_check_constraints(table.name)}:raise RuntimeError('Missing key revision constraint')
    grant=CreatorBuilderKey.__table__;grant.create(c,checkfirst=True);i=inspect(c)
    cols={v['name']:v for v in i.get_columns(grant.name)}
    if set(cols)!={v.name for v in grant.columns} or i.get_pk_constraint(grant.name)['constrained_columns']!=['builder_id']:raise RuntimeError('Incompatible builder key bindings')
    for col in grant.columns:
        actual=cols[col.name]
        if actual['nullable']!=col.nullable or actual['type']._type_affinity!=col.type._type_affinity:raise RuntimeError('Incompatible binding column')
    foreign={(v['constrained_columns'][0],v['referred_table'],v['referred_columns'][0],v.get('options',{}).get('ondelete','').upper()) for v in i.get_foreign_keys(grant.name)}
    if foreign!={('builder_id','builder_environments','id','CASCADE'),('user_id','users','id','CASCADE')}:raise RuntimeError('Invalid builder key references')
    for index in grant.indexes:
        index.create(c,checkfirst=True)
        if next(v for v in inspect(c).get_indexes(grant.name) if v['name']==index.name)['column_names']!=['user_id','enabled','builder_id']:raise RuntimeError('Incompatible key lookup index')
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
