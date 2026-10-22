"""Add two read indexes transactionally; no scoring/account rewrites."""
from datetime import datetime
from sqlalchemy import inspect,text,select
from app.models import SchemaRevision
REVISION='20261013_leaderboard'
INDEXES=[('question_solves','ix_question_solves_time_learner',['solved_at','learner_id'],None),('room_progress','ix_room_progress_completion_learner',['completed_at','learner_id'],'completed_at IS NOT NULL')]
def migrate(connection):
    if connection.dialect.name not in ('sqlite','postgresql'):raise RuntimeError('Unsupported leaderboard database')
    if connection.dialect.name=='postgresql':
        connection.execute(text("SET LOCAL lock_timeout='5s'"));connection.execute(text("SET LOCAL statement_timeout='30s'"));connection.execute(text('SELECT pg_advisory_xact_lock(20261013,5)'))
    for table,name,columns,predicate in INDEXES:
        existing=next((i for i in inspect(connection).get_indexes(table) if i['name']==name),None)
        if existing:
            where=str(existing.get('dialect_options',{}).get(connection.dialect.name+'_where','')).lower().replace('(','').replace(')','').strip()
            if existing['column_names']!=columns or existing['unique'] or where!=(predicate or '').lower():raise RuntimeError('Incompatible leaderboard index; migration refused')
        else:connection.execute(text('CREATE INDEX '+name+' ON '+table+' ('+', '.join(columns)+')'+(' WHERE '+predicate if predicate else '')))
    SchemaRevision.__table__.create(connection,checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)) is None:
        connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
    return False
