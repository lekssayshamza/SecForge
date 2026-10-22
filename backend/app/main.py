import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.config import settings
from app.login_limit import LoginRateLimitMiddleware, PublicRoomRateLimitMiddleware, ProfileWriteRateLimitMiddleware, ProfileReadRateLimitMiddleware, LeaderboardRateLimitMiddleware, TaskRateLimitMiddleware, PathRateLimitMiddleware, DiscoveryRateLimitMiddleware, OperationsRateLimitMiddleware
from sqlalchemy import text

from app.challenges import router as challenges_router
from app.admin import router as admin_router
from app.administration import router as administration_router
from app.creator_analytics import router as analytics_router
from app.infrastructure_visibility import router as infrastructure_router
from app.builder import router as builder_router
from app.creator import router as creator_router
from app.labs import labs_me_router, router as labs_router
from app.rooms import router as rooms_router
from app.public_rooms import router as public_rooms_router
from app.avatars import router as avatars_router
from app.learning import router as learning_router
from app.leaderboard import router as leaderboard_router
from app.room_tasks import router as room_tasks_router
from app.room_workspace import router as workspace_router
from app.builder_ssh import router as builder_ssh_router
from app.login_limit import BuilderAuthRateLimitMiddleware
from app.room_types import require_practical_room
from app.learning_paths import router as paths_router, public_router as public_paths_router
from app.discovery import router as discovery_router
from app.bookmarks import router as bookmarks_router
from app.social import router as social_router
from app.room_media import router as room_media_router
from app.login_limit import RoomMediaRateLimitMiddleware
from app.login_limit import SocialRateLimitMiddleware
from app.reconciliation import reconciliation_lifespan
from app.templates import router as templates_router
from app.backend_routes import sync_backend_routes
from app.vpn_authz import sync_vpn_authz
from app.vpn_routes import router as vpn_router

from app.database import SessionLocal, engine
from app.users import router as users_router
from app.auth import router as auth_router
from app.google_auth import router as google_auth_router

logger = logging.getLogger(__name__)


def _startup_vpn_sync() -> None:
    db = SessionLocal()
    try:
        sync_vpn_authz(db)
        sync_backend_routes(db)
    except Exception:
        logger.warning("Startup VPN synchronization failed", exc_info=True)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with reconciliation_lifespan(app):
        if settings.reconciliation_enabled:
            await asyncio.to_thread(_startup_vpn_sync)
        yield


app = FastAPI(title="SecForge API", lifespan=lifespan,
              docs_url=None if settings.production else "/docs",
              redoc_url=None if settings.production else "/redoc",
              openapi_url=None if settings.production else "/openapi.json")


@app.exception_handler(RequestValidationError)
async def validation_error(request, exception):
    if request.url.path.startswith(("/api/auth/", "/api/users")):
        # FastAPI normally echoes invalid input. Passwords and provider secrets
        # must not appear in error bodies or diagnostics.
        return JSONResponse({"detail": [{"loc": error["loc"], "type": error["type"], "msg": error["msg"]}
                                       for error in exception.errors()]}, status_code=422)
    return await request_validation_exception_handler(request, exception)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exception):
    # SQLAlchemy exception strings can contain bound password hashes/PKCE values.
    # Responses never serialize those diagnostics.
    return JSONResponse({"detail": "Database service is temporarily unavailable. Please retry."}, status_code=503)

app.add_middleware(LoginRateLimitMiddleware, enabled=settings.production)
app.add_middleware(PublicRoomRateLimitMiddleware, enabled=settings.production)
app.add_middleware(ProfileWriteRateLimitMiddleware, enabled=settings.production)
app.add_middleware(ProfileReadRateLimitMiddleware, enabled=settings.production)
app.add_middleware(LeaderboardRateLimitMiddleware, enabled=settings.production)
app.add_middleware(TaskRateLimitMiddleware, enabled=settings.production)
app.add_middleware(BuilderAuthRateLimitMiddleware, enabled=settings.production)
app.add_middleware(PathRateLimitMiddleware, enabled=settings.production)
app.add_middleware(DiscoveryRateLimitMiddleware, enabled=settings.production)
app.add_middleware(OperationsRateLimitMiddleware, enabled=settings.production)
app.add_middleware(SocialRateLimitMiddleware, enabled=settings.production)
app.add_middleware(RoomMediaRateLimitMiddleware, enabled=settings.production)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


app.include_router(users_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(google_auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(administration_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(infrastructure_router, prefix="/api")
app.include_router(creator_router, prefix="/api")
app.include_router(rooms_router, prefix="/api")
app.include_router(public_rooms_router, prefix="/api")
app.include_router(avatars_router, prefix="/api")
app.include_router(learning_router, prefix="/api")
app.include_router(leaderboard_router, prefix="/api")
app.include_router(room_tasks_router, prefix="/api")
app.include_router(room_media_router, prefix="/api")
app.include_router(workspace_router, prefix="/api")
app.include_router(builder_ssh_router, prefix="/api")
app.include_router(paths_router, prefix="/api")
app.include_router(public_paths_router, prefix="/api")
app.include_router(discovery_router, prefix="/api")
app.include_router(bookmarks_router, prefix="/api")
app.include_router(social_router, prefix='/api')
app.include_router(challenges_router, prefix="/api")
app.include_router(templates_router, prefix="/api", dependencies=[Depends(require_practical_room)])
app.include_router(builder_router, prefix="/api", dependencies=[Depends(require_practical_room)])
app.include_router(labs_router, prefix="/api", dependencies=[Depends(require_practical_room)])
app.include_router(labs_me_router, prefix="/api")
app.include_router(vpn_router, prefix="/api")

@app.get("/health")
def health_check():
    if settings.reconciliation_enabled:
        # Inspect the existing worker in this process; never instantiate or start
        # a second worker from a health request. Preserve the public response.
        from app import reconciliation
        worker = reconciliation._reconciliation_worker
        tasks = [getattr(worker, name, None) for name in ('_task', '_orphan_task', '_expiration_task')]
        if worker is None or any(task is None or task.done() for task in tasks):
            raise HTTPException(503, 'Application background processing is unavailable')
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return {
        "status": "ok",
        "database": "connected",
    }
