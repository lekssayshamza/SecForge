"""Add encrypted accepted input without rewriting any existing award."""
from datetime import datetime
from sqlalchemy import inspect, select, text, Text
from app.models import SchemaRevision

REVISION = '20261018_learner_submissions'


def migrate(c):
    if c.dialect.name not in ('postgresql', 'sqlite'):
        raise RuntimeError('Unsupported database')
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout='5s'"))
        c.execute(text("SET LOCAL statement_timeout='30s'"))
        c.execute(text('SELECT pg_advisory_xact_lock(20261018,2)'))
    if not inspect(c).has_table('question_solves'):
        raise RuntimeError('Existing challenge schema is required')
    columns = {v['name']: v for v in inspect(c).get_columns('question_solves')}
    if 'submission_ciphertext' not in columns:
        c.exec_driver_sql('ALTER TABLE question_solves ADD COLUMN submission_ciphertext TEXT NULL')
    elif not columns['submission_ciphertext']['nullable'] or not isinstance(columns['submission_ciphertext']['type'], Text):
        raise RuntimeError('Incompatible accepted submission column')
    SchemaRevision.__table__.create(c, checkfirst=True)
    if c.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    c.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
    return True
