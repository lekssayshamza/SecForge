from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="learner",
    )
    creator_access: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(280), nullable=True)
    avatar_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avatar_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    bio_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    avatar_style: Mapped[str] = mapped_column(String(20), default="initials", server_default="initials", nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(20), default="red", server_default="red", nullable=False)
    password_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    __table_args__ = (Index("uq_users_username_ci", func.lower(username), unique=True),
                      Index("uq_users_email_ci", func.lower(email), unique=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    lab_instances: Mapped[list["LabInstance"]] = relationship(
        "LabInstance",
        back_populates="learner",
        passive_deletes=True,
    )

    vpn_identity: Mapped["VpnIdentity | None"] = relationship(
        "VpnIdentity",
        back_populates="user",
        uselist=False,
        passive_deletes=True,
    )


class GoogleIdentity(Base):
    __tablename__ = "google_identities"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True)


class OAuthAttempt(Base):
    """Short-lived browser-bound, single-use authorization-code handoff."""
    __tablename__ = "oauth_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    state_digest: Mapped[str] = mapped_column(String(64), unique=True)
    browser_digest: Mapped[str] = mapped_column(String(64))
    nonce_digest: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    callback_consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    link_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    link_token_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CreatorInvitation(Base):
    __tablename__ = "creator_invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )
    invited_by: Mapped[int] = mapped_column(
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class Room(Base):
    __tablename__ = "rooms"
    exposed_ports: Mapped[list] = mapped_column(JSON, nullable=False,
        default=lambda: [{'protocol': 'tcp', 'from_port': p, 'to_port': p} for p in (22, 80)],
        server_default='[{"protocol":"tcp","from_port":22,"to_port":22},{"protocol":"tcp","from_port":80,"to_port":80}]')
    moderation_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    category_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    room_type: Mapped[str] = mapped_column(String(20), default="PRACTICAL", server_default="PRACTICAL")
    objectives: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    prerequisites: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    __table_args__ = (CheckConstraint("room_type IN ('THEORY','PRACTICAL')", name="ck_room_type"),)

    category_links: Mapped[list["RoomCategory"]] = relationship(lazy="selectin", cascade="all, delete-orphan", passive_deletes=True)

    @property
    def categories(self):
        return [self.category] + sorted(link.category for link in self.category_links if link.category != self.category)

    id: Mapped[int] = mapped_column(primary_key=True)
    creator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        String(2000),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    difficulty: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    lab_instances: Mapped[list["LabInstance"]] = relationship(
        "LabInstance",
        back_populates="room",
        passive_deletes=True,
    )
    templates: Mapped[list["RoomTemplate"]] = relationship(
        "RoomTemplate",
        back_populates="room",
        foreign_keys="RoomTemplate.room_id",
        order_by="RoomTemplate.version",
        passive_deletes=True,
    )


class BuilderEnvironment(Base):
    __tablename__ = "builder_environments"
    source_ami_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssh_auth_mode: Mapped[str] = mapped_column(String(16), default="key", server_default="key")
    ssh_guest_username: Mapped[str | None] = mapped_column(String(32), nullable=True)

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    creator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    instance_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(50), nullable=True)
    security_group_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    terraform_state_path: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_one_active_builder_per_creator",
            "creator_id",
            unique=True,
            postgresql_where=status.in_(["pending", "provisioning", "running", "stopping"]),
            sqlite_where=status.in_(["pending", "provisioning", "running", "stopping"]),
        ),
        Index("ix_builder_environment_status", "status"),
    )


class LabInstance(Base):
    __tablename__ = "lab_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    learner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    instance_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(50), nullable=True)
    security_group_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    terraform_state_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extensions_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    room: Mapped[Room] = relationship("Room", back_populates="lab_instances")
    learner: Mapped[User] = relationship("User", back_populates="lab_instances")

    __table_args__ = (
        CheckConstraint("extensions_used >= 0 AND extensions_used <= 2", name="ck_lab_extension_quota"),
        Index("ix_lab_instance_status", "status"),
        Index("ix_lab_instance_expires_at", "expires_at"),
        Index("ix_lab_instance_status_expires_at", "status", "expires_at"),
        Index(
            "ix_one_active_lab_per_learner_room",
            "room_id",
            "learner_id",
            unique=True,
            postgresql_where=status.in_(
                ["pending", "provisioning", "running", "stopping"]
            ),
            sqlite_where=status.in_(
                ["pending", "provisioning", "running", "stopping"]
            ),
        ),
    )


class VpnIdentity(Base):
    __tablename__ = "vpn_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    common_name: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    vpn_ip: Mapped[str] = mapped_column(
        String(15),
        unique=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="vpn_identity",
    )


class RoomTemplate(Base):
    """Immutable snapshot of a room's machine configuration.

    Each save creates a new version; existing rows are never mutated except for
    the machine provisioning status as its AMI becomes available.
    """

    __tablename__ = "room_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    room: Mapped[Room] = relationship(
        "Room",
        back_populates="templates",
        foreign_keys=[room_id],
    )
    machines: Mapped[list["MachineTemplate"]] = relationship(
        "MachineTemplate",
        back_populates="room_template",
        order_by="MachineTemplate.id",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "ix_room_template_room_version",
            "room_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_room_template_published",
            "room_id",
            unique=True,
            postgresql_where=is_published.is_(True),
            sqlite_where=is_published.is_(True),
        ),
    )


class MachineTemplate(Base):
    """A single machine definition within a room template version."""

    __tablename__ = "machine_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_template_id: Mapped[int] = mapped_column(
        ForeignKey("room_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="primary")
    ami_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instance_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="t3.micro"
    )
    source_builder_environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("builder_environments.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="creating"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    room_template: Mapped[RoomTemplate] = relationship(
        "RoomTemplate",
        back_populates="machines",
    )

    __table_args__ = (
        Index("ix_machine_template_status", "status"),
    )


class CaptureRequest(Base):
    """A single logical room-template capture, keyed for HTTP idempotency.

    One capture request maps to exactly one ``RoomTemplate``/``MachineTemplate``
    version. Replaying the same request (same room, creator and idempotency key)
    resumes the existing version instead of starting another capture, so an HTTP
    retry or process restart can never create a second EC2 image for one logical
    intent. The row is committed together with its template before any EC2 call,
    so the mapping survives a crash between the database write and the image
    creation.
    """

    __tablename__ = "capture_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    creator_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    room_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("room_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "creator_id",
            "idempotency_key",
            name="uq_capture_request_idempotency",
        ),
        Index("ix_capture_request_template", "room_template_id"),
    )


class RoomTask(Base):
    __tablename__ = "room_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    instructions: Mapped[str] = mapped_column(String(12000), default="")
    display_order: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("id", "room_id", name="uq_room_task_identity_room"),
        UniqueConstraint("room_id", "display_order", name="uq_room_task_order"),
        CheckConstraint("display_order > 0", name="ck_room_task_order"),
    )


class ChallengeQuestion(Base):
    __tablename__ = "challenge_questions"
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    question_text: Mapped[str] = mapped_column(String(1000))
    hint: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    answer_salt: Mapped[str] = mapped_column(String(32))
    answer_digest: Mapped[str] = mapped_column(String(64))
    answer_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    points: Mapped[int] = mapped_column(default=100)
    display_order: Mapped[int] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["task_id", "room_id"], ["room_tasks.id", "room_tasks.room_id"], name="fk_challenge_question_task_room"),
        CheckConstraint("points BETWEEN 1 AND 1000", name="ck_challenge_question_points"),
        CheckConstraint("display_order > 0", name="ck_challenge_question_order"),
        Index("uq_challenge_question_order", "room_id", "display_order", unique=True,
              postgresql_where=is_active.is_(True), sqlite_where=is_active.is_(True)),
    )


class QuestionSolve(Base):
    """Immutable award ledger: no submitted or expected plaintext answers."""
    __tablename__ = "question_solves"
    submission_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_id: Mapped[int | None] = mapped_column(ForeignKey("room_releases.id", ondelete="SET NULL"), nullable=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("challenge_questions.id", ondelete="RESTRICT"), index=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    points_awarded: Mapped[int] = mapped_column()
    solved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("ix_question_solves_time_learner", "solved_at", "learner_id"),
        UniqueConstraint("question_id", "learner_id", name="uq_question_solve_learner"),
        CheckConstraint("points_awarded BETWEEN 1 AND 1000", name="ck_question_solve_points"),
    )


class RoomProgress(Base):
    __tablename__ = "room_progress"
    completed_release_id: Mapped[int | None] = mapped_column(ForeignKey("room_releases.id", ondelete="SET NULL"), nullable=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="RESTRICT"), index=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    points_earned: Mapped[int] = mapped_column(default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_window_started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    attempt_count: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        Index("ix_room_progress_completion_learner", "completed_at", "learner_id", postgresql_where=completed_at.is_not(None), sqlite_where=completed_at.is_not(None)),
        UniqueConstraint("room_id", "learner_id", name="uq_room_progress_learner"),
        CheckConstraint("points_earned >= 0 AND attempt_count >= 0", name="ck_room_progress_nonnegative"),
    )


class RoomDraft(Base):
    """Owner-only working copy. Answer material is keyed digests, never plaintext."""
    __tablename__ = "room_drafts"
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True)
    document: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    image_template_id: Mapped[int | None] = mapped_column(ForeignKey("room_templates.id", ondelete="RESTRICT"), nullable=True)
    last_published_revision: Mapped[int | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RoomRelease(Base):
    """Append-only published snapshots; no public API exposes this document."""
    __tablename__ = "room_releases"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    document: Mapped[dict] = mapped_column(JSON)
    image_template_id: Mapped[int | None] = mapped_column(ForeignKey("room_templates.id", ondelete="RESTRICT"), nullable=True)
    published_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SchemaRevision(Base):
    __tablename__ = "secforge_schema_revisions"
    revision: Mapped[str] = mapped_column(String(100), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LearningPath(Base):
    __tablename__ = "learning_paths"
    moderation_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    category_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    id: Mapped[int] = mapped_column(primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000), default="")
    difficulty: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(50))
    objectives: Mapped[str] = mapped_column(String(2000), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("status IN ('draft','published')", name="ck_path_status"),
                     Index("ix_paths_status_id", "status", "id"))


class LearningPathRoom(Base):
    __tablename__ = "learning_path_rooms"
    path_id: Mapped[int] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"), primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True, index=True)
    display_order: Mapped[int] = mapped_column()
    __table_args__ = (UniqueConstraint("path_id", "display_order", name="uq_path_room_order"),
                     CheckConstraint("display_order > 0", name="ck_path_room_order"))


class RoomBookmark(Base):
    __tablename__ = "room_bookmarks"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PathBookmark(Base):
    __tablename__ = "path_bookmarks"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    path_id: Mapped[int] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminControl(Base):
    """Single database lock serializes promotions, suspension and moderation."""
    __tablename__ = 'admin_control'
    id: Mapped[int] = mapped_column(primary_key=True)
    __table_args__ = (CheckConstraint('id = 1', name='ck_admin_control_singleton'),)


class AdminAudit(Base):
    """No API supports editing/deleting these durable records. IDs survive deletion."""
    __tablename__ = 'admin_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(20))
    context: Mapped[str] = mapped_column(String(500), default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserFollow(Base):
    __tablename__ = 'user_follows'
    follower_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    followed_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (CheckConstraint('follower_id <> followed_id', name='ck_follow_not_self'),
                      Index('ix_follow_incoming', 'followed_id', 'created_at', 'follower_id'),
                      Index('ix_follow_outgoing', 'follower_id', 'created_at', 'followed_id'))


class UserBlock(Base):
    __tablename__ = 'user_blocks'
    blocker_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (CheckConstraint('blocker_id <> blocked_id', name='ck_block_not_self'),
                      Index('ix_block_incoming', 'blocked_id', 'blocker_id'))


class RoomMedia(Base):
    """Private, decoded lesson images; no client filename or filesystem path."""
    __tablename__ = 'room_media'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey('rooms.id', ondelete='CASCADE'), index=True)
    content_digest: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(20))
    width: Mapped[int] = mapped_column()
    height: Mapped[int] = mapped_column()
    byte_size: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (
        UniqueConstraint('room_id', 'content_digest', name='uq_room_media_content'),
        CheckConstraint("media_type IN ('image/png','image/jpeg','image/webp')", name='ck_room_media_type'),
        CheckConstraint('width BETWEEN 1 AND 1920 AND height BETWEEN 1 AND 1920', name='ck_room_media_dimensions'),
        CheckConstraint('byte_size BETWEEN 1 AND 2621440', name='ck_room_media_size'),
    )


class RoomReleaseMedia(Base):
    """Explicit images in an immutable release; draft uploads are not learner-visible."""
    __tablename__ = 'room_release_media'
    release_id: Mapped[int] = mapped_column(ForeignKey('room_releases.id', ondelete='CASCADE'), primary_key=True)
    media_id: Mapped[str] = mapped_column(ForeignKey('room_media.id', ondelete='CASCADE'), primary_key=True, index=True)


class RoomCategory(Base):
    __tablename__ = "room_categories"
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True)
    category: Mapped[str] = mapped_column(String(50), primary_key=True)
    __table_args__ = (Index("ix_room_categories_filter", "category", "room_id"),)


class CreatorSshKey(Base):
    """Public keys only. Existing administrative private keys never enter this table."""
    __tablename__ = 'creator_ssh_keys'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(2048))
    fingerprint: Mapped[str] = mapped_column(String(80), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    applied_builder_id: Mapped[int | None] = mapped_column(ForeignKey('builder_environments.id', ondelete='SET NULL'), nullable=True)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (CheckConstraint('revision >= 1', name='ck_creator_ssh_key_revision'),)


class CreatorBuilderKey(Base):
    """Tracks every applied guest binding so revocation never forgets old guests."""
    __tablename__ = 'creator_builder_keys'
    builder_id: Mapped[int] = mapped_column(ForeignKey('builder_environments.id', ondelete='CASCADE'), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    revision: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index('ix_creator_builder_keys_owner', 'user_id', 'enabled', 'builder_id'), CheckConstraint('revision >= 1',name='ck_creator_builder_key_revision'))
