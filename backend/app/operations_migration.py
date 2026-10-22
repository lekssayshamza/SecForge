"""Additive administration safety and durable audit records; never promotes users."""
from datetime import datetime
from sqlalchemy import inspect,text,select,Boolean,MetaData,Table,Column,Integer,String,DateTime,CheckConstraint
from app.models import SchemaRevision
_metadata=MetaData()
_control=Table("admin_control",_metadata,Column("id",Integer,primary_key=True),CheckConstraint("id = 1",name="ck_admin_control_singleton"))
_audit=Table("admin_audit",_metadata,Column("id",Integer,primary_key=True),Column("actor_id",Integer,nullable=True),Column("target_type",String(30),nullable=False),Column("target_id",Integer,nullable=False),Column("action",String(40),nullable=False),Column("outcome",String(20),nullable=False),Column("context",String(500),nullable=False),Column("created_at",DateTime,nullable=False))
REVISION='20261014_operations'
def migrate(c):
    if c.dialect.name=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261014,9)'))
    elif c.dialect.name!='sqlite':raise RuntimeError('Unsupported database')
    cols={x['name']:x for x in inspect(c).get_columns('users')}
    if 'suspended' not in cols:c.execute(text('ALTER TABLE users ADD COLUMN suspended BOOLEAN NOT NULL DEFAULT FALSE'))
    elif cols['suspended']['nullable'] or not isinstance(cols['suspended']['type'],Boolean):raise RuntimeError('Incompatible account status column')
    for table in ['rooms','learning_paths']:
        columns={x['name']:x for x in inspect(c).get_columns(table)}
        if 'moderation_hold' not in columns:c.execute(text('ALTER TABLE '+table+' ADD COLUMN moderation_hold BOOLEAN NOT NULL DEFAULT FALSE'))
        elif columns['moderation_hold']['nullable'] or not isinstance(columns['moderation_hold']['type'],Boolean):raise RuntimeError('Incompatible moderation column')
    _control.create(c,checkfirst=True);_audit.create(c,checkfirst=True)
    for table,expected in [('admin_control',{'id'}),('admin_audit',{'id','actor_id','target_type','target_id','action','outcome','context','created_at'})]:
        if {x['name'] for x in inspect(c).get_columns(table)}!=expected:raise RuntimeError('Incompatible administration table')
        if inspect(c).get_pk_constraint(table)['constrained_columns']!=['id']:raise RuntimeError('Incompatible administration identity')
    if c.scalar(select(_control.c.id).where(_control.c.id==1)) is None:c.execute(_control.insert().values(id=1))
    for table,name,columns in [('users','ix_users_role_suspended',['role','suspended']),('admin_audit','ix_admin_audit_created',['created_at','id']),('admin_audit','ix_admin_audit_actor',['actor_id','id']),('room_progress','ix_room_progress_room_completed',['room_id','completed_at'])]:
        old=next((x for x in inspect(c).get_indexes(table) if x['name']==name),None)
        if old and old['column_names']!=columns:raise RuntimeError('Incompatible operations index')
        if not old:c.execute(text('CREATE INDEX '+name+' ON '+table+'('+','.join(columns)+')'))
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)):return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
