"""Add task presentation table and nullable membership, without rewriting questions."""
from datetime import datetime
from sqlalchemy import inspect,select,text,Integer,String,DateTime
from app.models import SchemaRevision
REVISION='20261013_room_tasks'
def migrate(c):
    dialect=c.dialect.name
    if dialect not in ('sqlite','postgresql'):raise RuntimeError('Unsupported tasks database')
    if dialect=='postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='30s'"));c.execute(text('SELECT pg_advisory_xact_lock(20261013,6)'))
    i=inspect(c)
    if not i.has_table('rooms'):raise RuntimeError('Existing room schema required')
    if not i.has_table('room_tasks'):
        key='SERIAL PRIMARY KEY' if dialect=='postgresql' else 'INTEGER NOT NULL PRIMARY KEY'
        c.execute(text('CREATE TABLE room_tasks (id '+key+''',room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,title VARCHAR(120) NOT NULL,instructions VARCHAR(12000) NOT NULL DEFAULT '',display_order INTEGER NOT NULL,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,CONSTRAINT uq_room_task_identity_room UNIQUE(id,room_id),CONSTRAINT uq_room_task_order UNIQUE(room_id,display_order),CONSTRAINT ck_room_task_order CHECK(display_order>0))'''))
    cols={x['name']:x for x in inspect(c).get_columns('room_tasks')}
    types={'id':Integer,'room_id':Integer,'title':String,'instructions':String,'display_order':Integer,'created_at':DateTime}
    uniques={tuple(x['column_names']) for x in inspect(c).get_unique_constraints('room_tasks')}
    if set(cols)!=set(types) or any(not isinstance(cols[n]['type'],t) or cols[n]['nullable'] for n,t in types.items()) or not {('id','room_id'),('room_id','display_order')}.issubset(uniques) or cols['title']['type'].length!=120 or cols['instructions']['type'].length!=12000:raise RuntimeError('Incompatible tasks schema; migration refused')
    indexes={x['name']:x for x in inspect(c).get_indexes('room_tasks')}
    if 'ix_room_tasks_room_id' in indexes:
        if indexes['ix_room_tasks_room_id']['column_names']!=['room_id']:raise RuntimeError('Incompatible task index')
    else:c.execute(text('CREATE INDEX ix_room_tasks_room_id ON room_tasks(room_id)'))
    if inspect(c).has_table('challenge_questions'):
        columns={x['name']:x for x in inspect(c).get_columns('challenge_questions')}
        if 'task_id' not in columns:
            c.execute(text('ALTER TABLE challenge_questions ADD COLUMN task_id INTEGER'+(' REFERENCES room_tasks(id)' if dialect=='sqlite' else '')))
        else:
            if not columns['task_id']['nullable'] or not isinstance(columns['task_id']['type'],Integer):raise RuntimeError('Incompatible question membership')
        if dialect=='postgresql':
            fks=inspect(c).get_foreign_keys('challenge_questions');fk=next((x for x in fks if x['name']=='fk_challenge_question_task_room'),None)
            if fk:
                if fk['constrained_columns']!=['task_id','room_id'] or fk['referred_table']!='room_tasks' or fk['referred_columns']!=['id','room_id']:raise RuntimeError('Incompatible question task constraint')
            else:c.execute(text('ALTER TABLE challenge_questions ADD CONSTRAINT fk_challenge_question_task_room FOREIGN KEY(task_id,room_id) REFERENCES room_tasks(id,room_id)'))
        else:
            # SQLite cannot ALTER ADD a composite FK. Guard legacy columns with
            # triggers; fresh model schemas also have the real composite FK.
            for operation in ('INSERT','UPDATE'):
                c.execute(text('CREATE TRIGGER IF NOT EXISTS task_room_guard_'+operation.lower()+''' BEFORE '''+operation+''' ON challenge_questions WHEN NEW.task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM room_tasks WHERE id=NEW.task_id AND room_id=NEW.room_id) BEGIN SELECT RAISE(ABORT,'Question task must belong to its room'); END'''))
            c.execute(text("CREATE TRIGGER IF NOT EXISTS task_delete_guard BEFORE DELETE ON room_tasks WHEN EXISTS(SELECT 1 FROM rooms WHERE id=OLD.room_id) AND EXISTS(SELECT 1 FROM challenge_questions WHERE task_id=OLD.id) BEGIN SELECT RAISE(ABORT,'Unassign questions before removing their task'); END"))
    SchemaRevision.__table__.create(c,checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)) is None:
        c.execute(SchemaRevision.__table__.insert().values(revision=REVISION,applied_at=datetime.utcnow()));return True
    return False
