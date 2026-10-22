from app.categories import validate_category, validate_categories
from datetime import datetime
from typing import Literal
import unicodedata

from pydantic import BaseModel, EmailStr, Field, SecretStr, StrictBool, field_validator
from app.config import settings
from app.profile_identity import normalize_username
from app.lab_time import as_utc, utc_now
from app.network_ports import PortRules, DEFAULT_PORTS, normalize_ports

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

    _username = field_validator("username")(normalize_username)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return str(value).lower()

    model_config = {"extra": "forbid"}


class ProfileUpdate(BaseModel):
    avatar_public: StrictBool = False
    bio_public: StrictBool = False
    username: str | None = None
    display_name: str | None = Field(default=None, max_length=80)
    bio: str | None = Field(default=None, max_length=280)
    avatar_style: Literal["initials", "terminal", "compass", "code", "shield", "network"] | None = None
    avatar_color: Literal["red", "navy", "green", "blue", "purple"] | None = None

    _username = field_validator("username")(normalize_username)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value):
        if value is None:
            return None
        value = value.strip()
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Display name must not contain control characters.")
        return value or None

    @field_validator('bio')
    @classmethod
    def clean_bio(cls, value):
        if value is None: return None
        value = unicodedata.normalize('NFC', value.strip())
        if any(unicodedata.category(c).startswith('C') and c != '\n' for c in value):
            raise ValueError('Bio must not contain control characters.')
        return value or None

    @field_validator("avatar_style", "avatar_color")
    @classmethod
    def require_avatar_value(cls, value):
        if value is None:
            raise ValueError("Choose an avatar option.")
        return value

    class Config:
        extra = "forbid"


class UserResponse(BaseModel):
    avatar_public: bool = False
    bio_public: bool = False
    creator_access: bool = False
    id: int
    username: str
    email: str
    role: str
    display_name: str | None = None
    avatar_style: str = "initials"
    avatar_color: str = "red"
    must_change_password: bool = False
    password_enabled: bool = True
    bio: str | None = None
    avatar_version: str | None = None
    created_at: datetime | None = None

    @field_validator('created_at')
    @classmethod
    def utc_join_date(cls, value):
        return as_utc(value) if value else None

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr = Field(min_length=12, max_length=128)


class GoogleCompleteRequest(BaseModel):
    username: str | None = None
    _username = field_validator("username")(normalize_username)
    model_config = {"extra": "forbid"}


class GoogleLinkRequest(BaseModel):
    password: SecretStr
    model_config = {"extra": "forbid"}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class CreatorInvitationCreate(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)
    model_config = {"extra": "forbid"}


class CreatorInvitationResponse(BaseModel):
    id: int
    token: str
    expires_at: datetime


class CreatorInvitationAccept(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    model_config = {"extra": "forbid"}


class RoomBase(BaseModel):
    exposed_ports: PortRules = Field(default_factory=lambda: list(DEFAULT_PORTS), validate_default=True)
    _ports = field_validator('exposed_ports', mode='before')(normalize_ports)
    room_type: Literal["THEORY", "PRACTICAL"] = "PRACTICAL"
    objectives: str = Field(default="", max_length=2000)
    prerequisites: str = Field(default="", max_length=2000)
    name: str
    description: str | None = None
    category: str
    difficulty: str


class RoomCreate(RoomBase):
    categories: list[str] | None = None
    _categories = field_validator("categories")(lambda v: validate_categories(v) if v is not None else v)
    _category = field_validator("category")(validate_category)


class RoomUpdate(BaseModel):
    exposed_ports: PortRules = Field(default_factory=lambda: list(DEFAULT_PORTS), validate_default=True)
    _ports = field_validator('exposed_ports', mode='before')(normalize_ports)
    categories: list[str] | None = None
    _categories = field_validator("categories")(lambda v: validate_categories(v) if v is not None else v)
    _category = field_validator("category")(lambda value: validate_category(value) if value is not None else value)
    model_config = {"extra": "forbid"}
    objectives: str | None = Field(default=None, max_length=2000)
    prerequisites: str | None = Field(default=None, max_length=2000)
    name: str | None = None
    description: str | None = None
    category: str | None = None
    difficulty: str | None = None


class RoomResponse(RoomBase):
    categories: list[str] = Field(default_factory=list)
    id: int
    creator_id: int
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PublicRoomResponse(BaseModel):
    categories: list[str] = Field(default_factory=list)
    points_total: int | None = None
    room_type: Literal["THEORY", "PRACTICAL"] = "PRACTICAL"
    objectives: str = ""
    prerequisites: str = ""
    """Published briefing only; deliberately independent of the creator schema."""
    id: int
    name: str
    description: str | None
    category: str
    difficulty: str


class PublicRoomPage(BaseModel):
    items: list[PublicRoomResponse]
    total: int
    page: int
    page_size: int
    categories: list[str]


class BuilderStartRequest(BaseModel):
    ttl_hours: int = 4


class BuilderResponse(BaseModel):
    ssh_auth_mode: str = 'key'
    ssh_guest_username: str | None = None
    id: int
    room_id: int
    status: str
    private_ip: str | None
    instance_id: str | None
    security_group_id: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    terraform_state_path: str
    error_message: str | None = None
    # Public connection metadata only. Never serialize the server's key path
    # or private key; EC2 key delivery remains an operator-managed process.
    ssh_username: str = Field(default_factory=lambda: settings.builder_ssh_user)

    class Config:
        from_attributes = True


class LabStartRequest(BaseModel):
    ttl_hours: int = 4


class LabResponse(BaseModel):
    id: int
    room_id: int
    status: str
    private_ip: str | None
    instance_id: str | None
    security_group_id: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    stopped_at: datetime | None = None
    error_message: str | None = None
    session_started_at: datetime | None = None
    extensions_used: int = 0
    expired_at: datetime | None = None
    server_time: datetime = Field(default_factory=utc_now)

    @field_validator("expires_at", "created_at", "updated_at", "stopped_at", "session_started_at", "expired_at")
    @classmethod
    def utc_timestamps(cls, value):
        return as_utc(value) if value is not None else None

    class Config:
        from_attributes = True


class LabExtendRequest(BaseModel):
    lab_id: int = Field(gt=0, strict=True)
    expected_extensions_used: int = Field(ge=0, le=2, strict=True)

    class Config:
        extra = "forbid"


class MachineTemplateResponse(BaseModel):
    id: int
    room_template_id: int
    role: str
    ami_id: str | None
    instance_type: str
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RoomTemplateResponse(BaseModel):
    id: int
    room_id: int
    version: int
    created_by: int
    is_published: bool
    created_at: datetime
    machines: list[MachineTemplateResponse] = []

    class Config:
        from_attributes = True


class QuestionCreate(BaseModel):
    task_id: int | None = Field(default=None, ge=1, strict=True)
    question_text: str = Field(min_length=1, max_length=1000)
    hint: str | None = Field(default=None, max_length=2000)
    answer: SecretStr
    points: int = Field(default=100, ge=1, le=1000, strict=True)
    display_order: int | None = Field(default=None, ge=1, le=100, strict=True)

    @field_validator("question_text")
    @classmethod
    def text_nonempty(cls, value):
        if not value.strip(): raise ValueError("Question text is required")
        return value.strip()

    @field_validator("answer")
    @classmethod
    def answer_nonempty(cls, value):
        if not 1 <= len(value.get_secret_value().strip()) <= 512:
            raise ValueError("Answer must contain 1 to 512 characters")
        return value


class QuestionUpdate(BaseModel):
    task_id: int | None = Field(default=None, ge=1, strict=True)
    question_text: str | None = Field(default=None, min_length=1, max_length=1000)
    hint: str | None = Field(default=None, max_length=2000)
    answer: SecretStr | None = None
    points: int | None = Field(default=None, ge=1, le=1000, strict=True)
    display_order: int | None = Field(default=None, ge=1, le=100, strict=True)

    @field_validator("question_text", "points", "display_order")
    @classmethod
    def supplied_nonempty(cls, value):
        if value is None or isinstance(value, str) and not value.strip():
            raise ValueError("A supplied field must not be empty")
        return value.strip() if isinstance(value, str) else value

    @field_validator("answer")
    @classmethod
    def answer_nonempty(cls, value):
        if value is None or not 1 <= len(value.get_secret_value().strip()) <= 512:
            raise ValueError("Answer must contain 1 to 512 characters")
        return value


class QuestionResponse(BaseModel):
    task_id: int | None = None
    id: int
    question_text: str
    hint: str | None
    points: int
    display_order: int
    class Config:
        from_attributes = True


class QuestionListResponse(BaseModel):
    questions: list[QuestionResponse]
    authoring_locked: bool


class QuestionOrderRequest(BaseModel):
    question_ids: list[int] = Field(min_length=1, max_length=100)


class AnswerSubmission(BaseModel):
    answer: SecretStr
    @field_validator("answer")
    @classmethod
    def answer_nonempty(cls, value):
        if len(value.get_secret_value()) > 4096:
            raise ValueError('Submission is too long')
        return QuestionCreate.answer_nonempty(value)


class ProgressResponse(BaseModel):
    room_id: int
    solved_question_ids: list[int]
    questions_solved: int
    questions_total: int
    points_earned: int
    points_total: int
    completed: bool
    completed_at: datetime | None


class SubmissionResponse(BaseModel):
    correct: bool
    already_solved: bool
    points_awarded: int
    progress: ProgressResponse
