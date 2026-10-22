#!/usr/bin/env python3
"""Tests for the LabInstance model and schema."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import LabInstance, Room, User


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


def make_learner(db, username="lab_learner"):
    learner = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="hash",
        role="learner",
    )
    db.add(learner)
    db.flush()
    return learner


def make_room(db, creator, name="Lab Room", category="Web", difficulty="easy"):
    room = Room(
        name=name,
        category=category,
        difficulty=difficulty,
        creator_id=creator.id,
        status="published",
    )
    db.add(room)
    db.flush()
    return room


def make_instance(db, room, learner, expires_at=None, **overrides):
    instance = LabInstance(
        room_id=room.id,
        learner_id=learner.id,
        expires_at=expires_at or datetime.utcnow() + timedelta(hours=1),
        **overrides,
    )
    db.add(instance)
    db.flush()
    return instance


def test_lab_instance_created_with_required_relationships(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    instance = make_instance(db, room, learner)
    db.commit()

    assert instance.room_id == room.id
    assert instance.learner_id == learner.id

    db.expire_all()
    stored = db.get(LabInstance, instance.id)
    assert stored.room.name == room.name
    assert stored.learner.username == learner.username
    assert [i.id for i in stored.room.lab_instances] == [instance.id]
    assert [i.id for i in stored.learner.lab_instances] == [instance.id]


def test_lab_instance_defaults_to_pending_status(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    instance = make_instance(db, room, learner)
    assert instance.status == "pending"

    db.commit()
    db.expire_all()
    stored = db.get(LabInstance, instance.id)
    assert stored.status == "pending"
    assert LabInstance.__table__.c.status.default.arg == "pending"


def test_infrastructure_fields_are_nullable_before_provisioning(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    instance = make_instance(db, room, learner)
    db.commit()

    assert instance.instance_id is None
    assert instance.private_ip is None
    assert instance.security_group_id is None
    assert instance.terraform_state_path is None
    assert instance.stopped_at is None
    assert instance.error_message is None

    columns = {c["name"]: c for c in inspect(db.bind).get_columns("lab_instances")}
    for name in (
        "instance_id",
        "private_ip",
        "security_group_id",
        "terraform_state_path",
        "stopped_at",
        "error_message",
    ):
        assert columns[name]["nullable"] is True, name
    for name in ("id", "room_id", "learner_id", "status", "expires_at", "created_at", "updated_at"):
        assert columns[name]["nullable"] is False, name


def test_expires_at_is_stored_and_queryable(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    expires_at = datetime.utcnow() + timedelta(hours=2)
    instance = make_instance(db, room, learner, expires_at=expires_at)
    db.commit()

    db.expire_all()
    stored = db.get(LabInstance, instance.id)
    assert stored.expires_at == expires_at

    expired = db.scalars(
        select(LabInstance).where(LabInstance.expires_at < datetime.utcnow())
    ).all()
    assert instance.id not in [i.id for i in expired]

    due = db.scalars(
        select(LabInstance).where(
            LabInstance.expires_at < datetime.utcnow() + timedelta(hours=3)
        )
    ).all()
    assert instance.id in [i.id for i in due]


def test_same_learner_can_have_multiple_instances(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room_a = make_room(db, creator, name="Room A")
    room_b = make_room(db, creator, name="Room B")

    first = make_instance(db, room_a, learner)
    second = make_instance(db, room_b, learner)
    db.commit()

    assert first.id != second.id
    db.expire_all()
    owned = db.scalars(
        select(LabInstance).where(LabInstance.learner_id == learner.id)
    ).all()
    assert sorted(i.id for i in owned) == sorted([first.id, second.id])

    unique = [
        u for u in inspect(db.bind).get_unique_constraints("lab_instances")
        if [c for c in u["column_names"]] == ["learner_id"]
    ]
    assert unique == []


def test_same_room_can_have_multiple_instances(db):
    creator = make_learner(db, "lab_creator")
    learner_one = make_learner(db, "lab_learner_one")
    learner_two = make_learner(db, "lab_learner_two")
    room = make_room(db, creator)

    first = make_instance(db, room, learner_one)
    second = make_instance(db, room, learner_two)
    db.commit()

    db.expire_all()
    instances = db.scalars(
        select(LabInstance).where(LabInstance.room_id == room.id)
    ).all()
    assert sorted(i.id for i in instances) == sorted([first.id, second.id])
    assert sorted(i.learner_id for i in instances) == sorted(
        [learner_one.id, learner_two.id]
    )


def test_lab_instance_schema_has_expected_indexes(db):
    indexes = {i["name"]: i for i in inspect(db.bind).get_indexes("lab_instances")}

    assert indexes["ix_lab_instances_room_id"]["column_names"] == ["room_id"]
    assert indexes["ix_lab_instances_learner_id"]["column_names"] == ["learner_id"]
    assert indexes["ix_lab_instance_status"]["column_names"] == ["status"]
    assert indexes["ix_lab_instance_expires_at"]["column_names"] == ["expires_at"]
    assert indexes["ix_lab_instance_status_expires_at"]["column_names"] == [
        "status",
        "expires_at",
    ]

    active_unique = indexes["ix_one_active_lab_per_learner_room"]
    assert active_unique["unique"]
    assert active_unique["column_names"] == ["room_id", "learner_id"]

    for name, index in indexes.items():
        if name != "ix_one_active_lab_per_learner_room":
            assert not index["unique"], name

    fks = inspect(db.bind).get_foreign_keys("lab_instances")
    assert sorted(
        (fk["referred_table"], fk["options"].get("ondelete")) for fk in fks
    ) == [("rooms", "CASCADE"), ("users", "CASCADE")]


def test_historical_instances_do_not_block_new_active_instance(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    for historical_status in ("stopped", "failed"):
        historical = make_instance(
            db, room, learner, expires_at=datetime.utcnow() - timedelta(hours=1)
        )
        historical.status = historical_status
        db.flush()

    new_instance = make_instance(db, room, learner)
    db.commit()

    assert new_instance.status == "pending"


def test_second_active_instance_for_same_learner_room_is_rejected(db):
    creator = make_learner(db, "lab_creator")
    learner = make_learner(db)
    room = make_room(db, creator)

    make_instance(db, room, learner)
    db.commit()

    db.add(
        LabInstance(
            room_id=room.id,
            learner_id=learner.id,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
