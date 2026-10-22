"""Explicit, additive learner timing migration; no initialization on import."""
from sqlalchemy import DateTime, Integer, inspect, select, text
from app.models import SchemaRevision
from app.lab_time import INITIAL_TIME, as_utc, utc_now

REVISION = "20261011_learner_lab_time_v1"


def migrate(connection, now=None):
    dialect = connection.dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError("Unsupported database")
    if dialect == "postgresql":
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(20261011, 3)"))
    inspector = inspect(connection)
    if not inspector.has_table("lab_instances") or not inspector.has_table("users"):
        raise RuntimeError("Existing application schema required; no initialization performed")
    if dialect == "postgresql":
        connection.execute(text("LOCK TABLE lab_instances IN SHARE ROW EXCLUSIVE MODE"))
    columns = {c['name']: c for c in inspector.get_columns('lab_instances')}
    expected = {'session_started_at': (DateTime, True), 'expired_at': (DateTime, True), 'extensions_used': (Integer, False)}
    for name, (type_, nullable) in expected.items():
        if name in columns and (not isinstance(columns[name]['type'], type_) or columns[name]['nullable'] != nullable):
            raise RuntimeError("Incompatible lab timing schema")
    timestamp = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"
    for name in ['session_started_at', 'expired_at']:
        if name not in columns:
            connection.execute(text(f"ALTER TABLE lab_instances ADD COLUMN {name} {timestamp}"))
    if 'extensions_used' not in columns:
        connection.execute(text('ALTER TABLE lab_instances ADD COLUMN extensions_used INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_lab_extension_quota CHECK (extensions_used >= 0 AND extensions_used <= 2)'))
    checks = {c['name']: c['sqltext'] for c in inspect(connection).get_check_constraints('lab_instances')}
    if 'ck_lab_extension_quota' not in checks:
        if dialect != 'postgresql':
            raise RuntimeError('Existing extension quota constraint missing; migration refused')
        connection.execute(text('ALTER TABLE lab_instances ADD CONSTRAINT ck_lab_extension_quota CHECK (extensions_used >= 0 AND extensions_used <= 2)'))
    elif not all(part in checks['ck_lab_extension_quota'].replace('(', '').replace(')', '') for part in ['extensions_used >= 0', 'extensions_used <= 2']):
        raise RuntimeError('Incompatible extension quota constraint')
    if dialect == 'postgresql':
        for name in ['expires_at', 'session_started_at', 'expired_at']:
            col = next(c for c in inspect(connection).get_columns('lab_instances') if c['name'] == name)
            if not isinstance(col['type'], DateTime):
                raise RuntimeError('Incompatible timestamp type')
            if not col['type'].timezone:
                connection.execute(text(f"ALTER TABLE lab_instances ALTER COLUMN {name} TYPE TIMESTAMP WITH TIME ZONE USING {name} AT TIME ZONE 'UTC'"))
    SchemaRevision.__table__.create(connection, checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)):
        return False
    now = as_utc(now or utc_now())
    # Grace period on first rollout only: never shorten an existing deadline.
    # Creator test labs and stopped/failed history are not backfilled.
    rows = connection.execute(text("SELECT l.id,l.expires_at FROM lab_instances l JOIN users u ON u.id=l.learner_id WHERE u.role='learner' AND l.status IN ('pending','provisioning','running') AND l.session_started_at IS NULL")).mappings().all()
    for row in rows:
        from datetime import datetime
        deadline = row['expires_at']
        if isinstance(deadline, str):
            deadline = datetime.fromisoformat(deadline)
        deadline = max(as_utc(deadline), now + INITIAL_TIME)
        connection.execute(text('UPDATE lab_instances SET session_started_at=:now, expires_at=:deadline WHERE id=:id AND session_started_at IS NULL'), dict(id=row['id'], now=now, deadline=deadline))
    connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=now.replace(tzinfo=None)))
    return True
