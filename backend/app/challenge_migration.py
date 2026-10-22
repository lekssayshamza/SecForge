"""Additive, versioned challenge schema migration; no operations at import time."""
from datetime import datetime
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, select, text
from app.models import RoomTask, ChallengeQuestion, QuestionSolve, RoomProgress, SchemaRevision

REVISION = "20261011_challenge_learning_v1"
TABLES = [SchemaRevision.__table__, RoomTask.__table__, ChallengeQuestion.__table__, QuestionSolve.__table__, RoomProgress.__table__]


def migrate(connection):
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(20261011, 1)"))
    inspector = inspect(connection)
    if not {"rooms", "users"}.issubset(inspector.get_table_names()):
        raise RuntimeError("Existing application schema is required; initialization is not performed.")
    # Refuse name collisions/incompatible pre-existing tables instead of resetting data.
    for table in TABLES:
        if inspector.has_table(table.name):
            columns = {c["name"]: c for c in inspector.get_columns(table.name)}
            compatible = set(columns) == {c.name for c in table.columns}
            if compatible:
                compatible = all(columns[c.name]["nullable"] == c.nullable and
                    columns[c.name]["type"]._type_affinity is c.type._type_affinity for c in table.columns)
            uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
            compatible &= all(tuple(c.name for c in constraint.columns) in uniques
                for constraint in table.constraints if isinstance(constraint, UniqueConstraint))
            compatible &= inspector.get_pk_constraint(table.name)["constrained_columns"] == [c.name for c in table.primary_key.columns]
            checks = {item["name"] for item in inspector.get_check_constraints(table.name)}
            compatible &= all(constraint.name in checks for constraint in table.constraints
                if isinstance(constraint, CheckConstraint))
            indexes = {item["name"]: item for item in inspector.get_indexes(table.name)}
            compatible &= all(index.name in indexes and
                bool(indexes[index.name]["unique"]) == bool(index.unique) and
                indexes[index.name]["column_names"] == [c.name for c in index.columns]
                for index in table.indexes)
            if not compatible:
                raise RuntimeError("An incompatible challenge schema exists; migration refused.")
    for table in TABLES:
        table.create(connection, checkfirst=True)
    if connection.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision == REVISION)) is None:
        connection.execute(SchemaRevision.__table__.insert().values(revision=REVISION, applied_at=datetime.utcnow()))
        return True
    return False
