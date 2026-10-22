"""Only the owned, disposable PostgreSQL harness may activate these tests."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner', allow_module_level=True)

from sqlalchemy import inspect, text
from app.network_ports import DEFAULT_PORTS
from app.network_ports_migration import migrate
from test_postgres_lab_concurrency import pg_sessions
from test_postgres_challenges import seed


def test_room_ports_migration_preserves_existing_rooms_and_is_idempotent(pg_sessions):
    room_id, *_ = seed(pg_sessions)
    with pg_sessions() as db:
        before = db.execute(text('SELECT id, name, creator_id FROM rooms ORDER BY id')).all()
        db.execute(text('ALTER TABLE rooms DROP COLUMN exposed_ports'))
        db.commit()
        assert migrate(db.connection()) is True
        db.commit()
        assert migrate(db.connection()) is False
        db.commit()
        assert db.execute(text('SELECT id, name, creator_id FROM rooms ORDER BY id')).all() == before
        assert db.execute(text('SELECT exposed_ports FROM rooms WHERE id=:id'), {'id':room_id}).scalar_one() == DEFAULT_PORTS
        columns = {c['name']: c for c in inspect(db.connection()).get_columns('rooms')}
        assert columns['exposed_ports']['nullable'] is False


def test_room_ports_migration_refuses_incompatible_column(pg_sessions):
    with pg_sessions() as db:
        db.execute(text('ALTER TABLE rooms DROP COLUMN exposed_ports'))
        db.execute(text('ALTER TABLE rooms ADD COLUMN exposed_ports TEXT'))
        db.commit()
        with pytest.raises(RuntimeError, match='Incompatible'):
            migrate(db.connection())
        db.rollback()
