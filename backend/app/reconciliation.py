import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update, or_, and_
from sqlalchemy.orm import Session

from app.aws_discovery import AWSDiscoveryService, AWSDiscoveryError, BuilderResources
from app.capture_identity import capture_image_tags
from app.config import settings
from app.cleanup import CLEANUP_PREFIX, LEGACY_CLEANUP_ERRORS, cleanup_failed
from app.database import SessionLocal
from app.lab_manager import LabManager, TerraformError
from app.lab_time import utc_now, as_utc
from app.models import (
    BuilderEnvironment,
    CaptureRequest,
    LabInstance,
    MachineTemplate,
    RoomTemplate,
)
from app.ops_lock import OperationInProgress
from app.backend_routes import BackendRouteError, sync_backend_routes
from app.vpn_authz import VpnAuthorizationError, sync_vpn_authz

logger = logging.getLogger(__name__)


def _set_terminal(
    db: Session,
    model,
    row_id: int,
    status: str,
    *,
    allowed_current,
    updates: dict,
) -> None:
    """Atomically move a row to a terminal status.

    The UPDATE is conditional on the row currently being in one of
    ``allowed_current`` statuses, so a stale writer can never clobber a newer
    terminal state (e.g. a "failed"/"stopped" row written by another worker).
    """
    db.execute(
        update(model)
        .where(model.id == row_id, model.status.in_(allowed_current))
        .values(status=status, updated_at=datetime.utcnow(), **updates)
        .execution_options(synchronize_session=False)
    )
    db.commit()


class ReconciliationWorker:
    def __init__(self):
        self.config = {
            "interval_seconds": settings.builder_reconciliation_interval_seconds,
            "lab_expiration_interval_seconds": 10,
            "stale_pending_minutes": settings.builder_stale_pending_minutes,
            "stale_provisioning_minutes": settings.builder_stale_provisioning_minutes,
            "terraform_dry_run": settings.terraform_dry_run,
            "orphan_discovery_interval_seconds": settings.orphan_discovery_interval_seconds,
            "machine_template_creating_minutes": settings.machine_template_creating_minutes,
            "stopping_grace_seconds": (
                settings.terraform_timeout_seconds
                + settings.builder_reconciliation_interval_seconds
            ),
        }
        self._task: Optional[asyncio.Task] = None
        self._orphan_task: Optional[asyncio.Task] = None
        self._expiration_task: Optional[asyncio.Task] = None
        self._expiration_cleanups: dict[int, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        self._orphan_stop_event = asyncio.Event()
        self._discovery_service = AWSDiscoveryService(dry_run=settings.terraform_dry_run)

    async def start(self):
        """Start the reconciliation worker."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._orphan_stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        self._orphan_task = asyncio.create_task(self._run_orphan_loop())
        self._expiration_task = asyncio.create_task(self._run_expiration_loop())
        logger.info("Builder reconciliation worker started")

    async def stop(self):
        """Stop the reconciliation worker."""
        if self._task is None:
            return
        self._stop_event.set()
        self._orphan_stop_event.set()
        if self._expiration_task is not None:
            await self._expiration_task
        if self._expiration_cleanups:
            # Destruction uses the existing bounded Terraform operations and
            # operation lock. Do not cancel an in-flight destroy on shutdown.
            await asyncio.gather(*self._expiration_cleanups.values(), return_exceptions=True)
            self._expiration_cleanups.clear()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Reconciliation worker did not stop gracefully")
        try:
            await asyncio.wait_for(self._orphan_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Orphan discovery worker did not stop gracefully")
        self._task = None
        self._orphan_task = None
        self._expiration_task = None
        logger.info("Builder reconciliation worker stopped")

    async def _run_expiration_loop(self):
        """Fast access revocation in this worker, independent of slow Terraform.

        Own sessions/connections; no additional process or reconciliation worker.
        Startup immediately recovers deadlines missed while the backend was down.
        """
        while not self._stop_event.is_set():
            try:
                ids = await asyncio.to_thread(self._expiration_step)
                if self._stop_event.is_set():
                    break  # Committed claims are recovered on the next startup.
                from app.labs import destroy_lab_background
                for lab_id in ids:
                    if lab_id in self._expiration_cleanups:
                        continue
                    task = asyncio.create_task(asyncio.to_thread(destroy_lab_background, lab_id))
                    self._expiration_cleanups[lab_id] = task
                    task.add_done_callback(lambda task, lab_id=lab_id: self._expiration_cleanups.pop(lab_id, None))
            except Exception:
                logger.warning("Learner expiration check failed; retrying", exc_info=False)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config['lab_expiration_interval_seconds'])
            except asyncio.TimeoutError:
                pass

    def _expiration_step(self):
        """Commit Stop claims first, revoke access, then return cleanup work."""
        with SessionLocal() as db:
            now = utc_now()
            ids = db.execute(update(LabInstance).where(
                LabInstance.session_started_at.is_not(None),
                LabInstance.expires_at <= now,
                LabInstance.status.in_(("pending", "provisioning", "running")),
            ).values(status="stopping", expired_at=now, updated_at=datetime.utcnow())
              .returning(LabInstance.id).execution_options(synchronize_session=False)).scalars().all()
            db.commit()
            db.execute(update(LabInstance).where(
                LabInstance.session_started_at.is_not(None), LabInstance.status == "stopping",
                LabInstance.expires_at <= now, LabInstance.expired_at.is_(None),
            ).values(expired_at=now, updated_at=LabInstance.updated_at).execution_options(synchronize_session=False))
            db.commit()
            # Keep retrying a failed gateway sync while a timed session is live
            # or cleaning up. desired_pairs independently excludes deadlines.
            active = db.scalar(select(LabInstance.id).where(
                LabInstance.session_started_at.is_not(None),
                LabInstance.status.in_(("pending", "provisioning", "running", "stopping")),
            ).limit(1))
            if ids or active is not None:
                try:
                    sync_vpn_authz(db)
                except VpnAuthorizationError:
                    logger.warning("Expired lab access revocation failed; retrying within 10 seconds")
            # Also recover a Stop claimed immediately before a crash, or while
            # provisioning held the operation lock. Failed destroys respect the
            # existing reconciliation retry interval; lock deferrals retry fast.
            retries = db.scalars(select(LabInstance.id).where(
                LabInstance.status == "stopping", LabInstance.expired_at.is_not(None),
                or_(LabInstance.error_message.is_(None), LabInstance.updated_at <= datetime.utcnow() - timedelta(seconds=self.config['interval_seconds'])),
            )).all()
            return list(set(ids) | set(retries))

    async def _run_loop(self):
        """Main reconciliation loop."""
        while not self._stop_event.is_set():
            try:
                await self._reconcile_once()
            except Exception as e:
                logger.error(f"Error during reconciliation: {e}", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config["interval_seconds"],
                )
            except asyncio.TimeoutError:
                continue

    async def _run_orphan_loop(self):
        """Orphan discovery loop."""
        while not self._orphan_stop_event.is_set():
            try:
                await self._discover_orphans()
            except Exception as e:
                logger.error(f"Error during orphan discovery: {e}", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._orphan_stop_event.wait(),
                    timeout=self.config["orphan_discovery_interval_seconds"],
                )
            except asyncio.TimeoutError:
                continue

    async def _reconcile_once(self):
        """Run a single reconciliation pass."""
        logger.debug("Starting builder reconciliation pass")

        db = SessionLocal()
        try:
            await self._handle_expired_running_builders(db)
            await self._handle_stale_pending_builders(db)
            await self._handle_stale_provisioning_builders(db)
            await self._handle_crashed_stopping_builders(db)
            await self._handle_expired_running_labs(db)
            await self._handle_stale_pending_labs(db)
            await self._handle_stale_provisioning_labs(db)
            await self._handle_crashed_stopping_labs(db)
            await self._handle_stale_creating_machines(db)
            from app.network_sync import sync_running_networks
            sync_running_networks(db)
            try:
                sync_vpn_authz(db)
            except VpnAuthorizationError as exc:
                logger.warning(
                    "VPN authorization sync failed during reconciliation: %s", exc
                )
            try:
                sync_backend_routes(db)
            except BackendRouteError as exc:
                logger.warning(
                    "Backend host route sync failed during reconciliation: %s", exc
                )
        finally:
            db.close()

    def _claim_lab_for_stop(self, db: Session, lab_id: int, expired_before=None) -> Optional[LabInstance]:
        """Atomically claim a learner lab instance for stopping."""
        lab = db.execute(
            select(LabInstance)
            .where(LabInstance.id == lab_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

        if lab is None:
            return None

        if expired_before is not None and as_utc(lab.expires_at) > expired_before:
            db.rollback()
            return None

        if lab.status not in ("provisioning", "running", "stopping") and not cleanup_failed(lab):
            return None

        values = dict(status="stopping", updated_at=datetime.utcnow())
        if expired_before is not None and lab.session_started_at is not None:
            values["expired_at"] = expired_before
        query = update(LabInstance).where(LabInstance.id == lab_id, LabInstance.status == lab.status)
        if expired_before is not None:
            query = query.where(LabInstance.expires_at <= expired_before)
        claimed = db.execute(
            query.values(**values).execution_options(synchronize_session=False)
        )
        db.commit()
        if not claimed.rowcount:
            return None
        db.refresh(lab)
        return lab

    def _mark_lab_failed(self, db: Session, lab_id: int, error_message: str):
        _set_terminal(
            db,
            LabInstance,
            lab_id,
            "failed",
            allowed_current=("pending", "provisioning", "running", "stopping", "failed"),
            updates={"error_message": error_message},
        )

    def _mark_lab_stopped(self, db: Session, lab_id: int):
        _set_terminal(
            db,
            LabInstance,
            lab_id,
            "stopped",
            allowed_current=("provisioning", "running", "stopping"),
            updates={
                "instance_id": None,
                "private_ip": None,
                "security_group_id": None,
                "error_message": None,
                "stopped_at": datetime.utcnow(),
            },
        )

    def _claim_builder_for_stop(self, db: Session, builder_id: int) -> Optional[BuilderEnvironment]:
        """
        Atomically claim a builder for stopping.
        Returns the builder if successfully claimed, None otherwise.
        Uses a short transaction to avoid holding lock during Terraform.
        """
        builder = db.execute(
            select(BuilderEnvironment)
            .where(BuilderEnvironment.id == builder_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

        if builder is None:
            return None

        if builder.status not in ("provisioning", "running", "stopping") and not cleanup_failed(builder):
            return None

        claimed = db.execute(update(BuilderEnvironment)
                             .where(BuilderEnvironment.id == builder.id,
                                    BuilderEnvironment.status == builder.status)
                             .values(status="stopping", updated_at=datetime.utcnow()))
        db.commit()
        if not claimed.rowcount:
            return None
        db.refresh(builder)
        return builder

    def _mark_cleanup_retry(self, db: Session, model, row_id: int, message: str):
        """Keep cleanup eligible without clobbering a newer terminal state."""
        db.rollback()
        # Raw unexpected errors/provider output are not safe API diagnostics.
        safe = message if message.startswith(CLEANUP_PREFIX) else CLEANUP_PREFIX + "cleanup operation failed; inspect protected diagnostics"
        db.execute(update(model).where(model.id == row_id, model.status == "stopping")
                   .values(error_message=safe, updated_at=datetime.utcnow()))
        db.commit()

    def _mark_builder_failed(self, db: Session, builder_id: int, error_message: str):
        """Mark a builder as failed with error message."""
        _set_terminal(
            db,
            BuilderEnvironment,
            builder_id,
            "failed",
            allowed_current=("pending", "provisioning", "running", "stopping", "failed"),
            updates={"error_message": error_message},
        )

    def _mark_builder_stopped(self, db: Session, builder_id: int):
        """Mark a builder as stopped after successful destroy."""
        _set_terminal(
            db,
            BuilderEnvironment,
            builder_id,
            "stopped",
            allowed_current=("provisioning", "running", "stopping"),
            updates={
                "instance_id": None,
                "private_ip": None,
                "security_group_id": None,
                "error_message": None,
                "stopped_at": datetime.utcnow(),
            },
        )

    async def _handle_expired_running_builders(self, db: Session):
        """Find and stop builders that have expired."""
        now = datetime.utcnow()
        expired_builders = db.execute(
            select(BuilderEnvironment)
            .where(
                BuilderEnvironment.status == "running",
                BuilderEnvironment.expires_at < now,
            )
        ).scalars().all()

        for builder in expired_builders:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_builder_for_stop(db, builder.id)
                if claimed is None:
                    logger.debug(f"Builder {builder.id} already claimed or not eligible")
                    continue

                logger.info(f"Builder {builder.id} expired, stopping")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_builder, claimed)
            except TerraformError as e:
                logger.error(f"Failed to stop expired builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Builder {builder.id} stop already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error stopping builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, "unexpected cleanup error")

    async def _handle_stale_pending_builders(self, db: Session):
        """Find and clean up stale pending builders."""
        threshold = datetime.utcnow() - timedelta(minutes=self.config["stale_pending_minutes"])
        stale_builders = db.execute(
            select(BuilderEnvironment)
            .where(
                BuilderEnvironment.status == "pending",
                BuilderEnvironment.created_at < threshold,
            )
        ).scalars().all()

        for builder in stale_builders:
            if self._stop_event.is_set():
                break
            try:
                builder.status = "failed"
                builder.error_message = (
                    f"Builder stale in pending status for more than "
                    f"{self.config['stale_pending_minutes']} minutes"
                )
                builder.updated_at = datetime.utcnow()
                db.commit()
                logger.warning(f"Builder {builder.id} marked failed: stale pending")
            except Exception as e:
                logger.error(f"Error handling stale pending builder {builder.id}: {e}")

    async def _handle_stale_provisioning_builders(self, db: Session):
        """Find and clean up stale provisioning builders."""
        threshold = datetime.utcnow() - timedelta(minutes=self.config["stale_provisioning_minutes"])
        stale_builders = db.execute(
            select(BuilderEnvironment)
            .where(
                BuilderEnvironment.status == "provisioning",
                BuilderEnvironment.updated_at < threshold,
            )
        ).scalars().all()

        for builder in stale_builders:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_builder_for_stop(db, builder.id)
                if claimed is None:
                    logger.debug(f"Builder {builder.id} already claimed or not eligible")
                    continue

                logger.info(f"Builder {builder.id} stale in provisioning, stopping")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_builder, claimed)
            except TerraformError as e:
                logger.error(f"Failed to stop stale provisioning builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Builder {builder.id} stop already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error stopping builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, "unexpected cleanup error")

    async def _handle_crashed_stopping_builders(self, db: Session):
        """Find and retry builders that were in stopping state when app crashed."""
        grace = datetime.utcnow() - timedelta(
            seconds=self.config["stopping_grace_seconds"]
        )
        stopped_builders = db.execute(
            select(BuilderEnvironment)
            .where(
                or_(BuilderEnvironment.status == "stopping",
                    and_(BuilderEnvironment.status == "failed",
                         or_(BuilderEnvironment.error_message.in_(LEGACY_CLEANUP_ERRORS),
                             BuilderEnvironment.error_message.startswith(CLEANUP_PREFIX)))),
                BuilderEnvironment.updated_at < grace,
            )
        ).scalars().all()

        for builder in stopped_builders:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_builder_for_stop(db, builder.id)
                if claimed is None:
                    logger.debug(f"Builder {builder.id} already claimed or not eligible")
                    continue

                logger.info(f"Builder {builder.id} was stopping, retrying destroy")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_builder, claimed)
            except TerraformError as e:
                logger.error(f"Failed to retry stopping builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Builder {builder.id} destroy already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error retrying stopping builder {builder.id}: {e}")
                self._mark_cleanup_retry(db, BuilderEnvironment, builder.id, "unexpected cleanup error")

    async def _handle_expired_running_labs(self, db: Session):
        """Find and stop learner lab instances that have expired."""
        now = utc_now()
        expired_labs = db.execute(
            select(LabInstance)
            .where(
                LabInstance.status == "running",
                LabInstance.expires_at <= now,
            )
        ).scalars().all()

        for lab in expired_labs:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_lab_for_stop(db, lab.id, expired_before=now)
                if claimed is None:
                    logger.debug(f"Lab {lab.id} already claimed or not eligible")
                    continue

                logger.info(f"Lab {lab.id} expired, stopping")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_lab_instance, claimed, db)
            except TerraformError as e:
                logger.error(f"Failed to stop expired lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Lab {lab.id} stop already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error stopping lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, "unexpected cleanup error")

    async def _handle_stale_pending_labs(self, db: Session):
        """Find learner lab instances stuck in pending status."""
        threshold = datetime.utcnow() - timedelta(minutes=self.config["stale_pending_minutes"])
        stale_labs = db.execute(
            select(LabInstance)
            .where(
                LabInstance.status == "pending",
                LabInstance.created_at < threshold,
            )
        ).scalars().all()

        for lab in stale_labs:
            if self._stop_event.is_set():
                break
            try:
                failed = db.execute(
                    update(LabInstance)
                    .where(LabInstance.id == lab.id, LabInstance.status == "pending")
                    .values(status="failed", updated_at=datetime.utcnow(), error_message=(
                        f"Lab instance stale in pending status for more than "
                        f"{self.config['stale_pending_minutes']} minutes"
                    ))
                )
                db.commit()
                if failed.rowcount:
                    logger.warning(f"Lab {lab.id} marked failed: stale pending")
            except Exception as e:
                logger.error(f"Error handling stale pending lab {lab.id}: {e}")

    async def _handle_stale_provisioning_labs(self, db: Session):
        """Find learner lab instances stuck in provisioning status."""
        threshold = datetime.utcnow() - timedelta(minutes=self.config["stale_provisioning_minutes"])
        stale_labs = db.execute(
            select(LabInstance)
            .where(
                LabInstance.status == "provisioning",
                LabInstance.updated_at < threshold,
            )
        ).scalars().all()

        for lab in stale_labs:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_lab_for_stop(db, lab.id)
                if claimed is None:
                    logger.debug(f"Lab {lab.id} already claimed or not eligible")
                    continue

                logger.info(f"Lab {lab.id} stale in provisioning, stopping")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_lab_instance, claimed, db)
            except TerraformError as e:
                logger.error(f"Failed to stop stale provisioning lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Lab {lab.id} stop already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error stopping lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, "unexpected cleanup error")

    async def _handle_crashed_stopping_labs(self, db: Session):
        """Find and retry learner lab instances that crashed while stopping."""
        grace = datetime.utcnow() - timedelta(
            seconds=self.config["stopping_grace_seconds"]
        )
        stopping_labs = db.execute(
            select(LabInstance)
            .where(
                or_(LabInstance.status == "stopping",
                    and_(LabInstance.status == "failed",
                         or_(LabInstance.error_message.in_(LEGACY_CLEANUP_ERRORS),
                             LabInstance.error_message.startswith(CLEANUP_PREFIX)))),
                LabInstance.updated_at < grace,
            )
        ).scalars().all()

        for lab in stopping_labs:
            if self._stop_event.is_set():
                break
            try:
                claimed = self._claim_lab_for_stop(db, lab.id)
                if claimed is None:
                    logger.debug(f"Lab {lab.id} already claimed or not eligible")
                    continue

                logger.info(f"Lab {lab.id} was stopping, retrying destroy")
                lab_manager = LabManager()
                await asyncio.to_thread(lab_manager.stop_lab_instance, claimed, db)
            except TerraformError as e:
                logger.error(f"Failed to retry stopping lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, str(e.message))
            except OperationInProgress:
                logger.debug(f"Lab {lab.id} destroy already in progress, skipping")
            except Exception as e:
                logger.error(f"Unexpected error retrying stopping lab {lab.id}: {e}")
                self._mark_cleanup_retry(db, LabInstance, lab.id, "unexpected cleanup error")

    def _make_ami_manager(self):
        """Build the AMI manager used for reconciliation.

        Separated so tests can inject a fake without touching AWS.
        """
        from app.ami_manager import AMIManager

        return AMIManager(dry_run=self.config["terraform_dry_run"])

    async def _handle_stale_creating_machines(self, db: Session):
        """Finalize or recover room-template AMIs left non-terminal.

        Rows with a known AMI id are finalized by polling its state. Rows whose
        creation outcome is unknown (no AMI id, e.g. an ambiguous CreateImage)
        are recovered using canonical tags. Inconclusive lookups stay recoverable;
        creation is never re-issued because EC2 has no idempotency token.

        All AWS/SSH work happens in a worker thread with its own database
        session, so the asyncio event loop is never blocked and no live session
        is shared across threads. Blocking calls stay sequential, preserving the
        existing ordering and state-transition guarantees.
        """
        grace = datetime.utcnow() - timedelta(
            minutes=self.config["machine_template_creating_minutes"]
        )
        machine_ids = db.execute(
            select(MachineTemplate.id).where(
                MachineTemplate.status.in_(("creating", "unknown")),
                MachineTemplate.updated_at < grace,
            )
        ).scalars().all()

        if not machine_ids:
            return

        engine = db.get_bind()
        # Release the loop session's connection before handing work to a thread.
        db.commit()

        for machine_id in machine_ids:
            if self._stop_event.is_set():
                break
            await asyncio.to_thread(self._process_creating_machine, engine, machine_id)

    def _process_creating_machine(self, engine, machine_id: int) -> None:
        """Finalize or recover one machine in a dedicated thread and session."""
        db = Session(bind=engine)
        try:
            machine = db.get(MachineTemplate, machine_id)
            if machine is None or machine.status not in ("creating", "unknown"):
                return
            manager = self._make_ami_manager()
            if machine.ami_id:
                self._finalize_machine_from_state(db, manager, machine)
            else:
                self._recover_machine_image(db, manager, machine)
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the pass
            logger.error(
                "Unexpected error reconciling machine template %s: %s",
                machine_id,
                exc,
                exc_info=True,
            )
            db.rollback()
        finally:
            db.close()

    def _finalize_machine_from_state(self, db: Session, manager, machine) -> None:
        """Finalize a machine whose AMI id is known by polling EC2 state.

        A transient describe error leaves the row recoverable for a later pass;
        only a definitive EC2 state (failed/deregistered) is terminal.
        """
        from app.ami_manager import AMIError

        try:
            state = manager.get_image_state(machine.ami_id)
        except AMIError as exc:
            logger.warning(
                "Transient error checking AMI %s for machine template %s: %s",
                machine.ami_id,
                machine.id,
                exc,
            )
            return

        if state == "available":
            _set_terminal(
                db,
                MachineTemplate,
                machine.id,
                "available",
                allowed_current=("creating", "unknown"),
                updates={"error_message": None},
            )
        elif state in ("failed", "deregistered"):
            _set_terminal(
                db,
                MachineTemplate,
                machine.id,
                "failed",
                allowed_current=("creating", "unknown"),
                updates={"error_message": "AMI creation failed"},
            )

    def _fail_machine(self, db: Session, machine, message: str) -> None:
        """Mark a machine failed only when recovery is provably impossible."""
        _set_terminal(
            db,
            MachineTemplate,
            machine.id,
            "failed",
            allowed_current=("creating", "unknown"),
            updates={"error_message": message},
        )

    def _persist_recovered_image(self, db: Session, machine, image_id: str) -> None:
        """Attach a recovered AMI id and keep the row moving toward "available"."""
        machine.ami_id = image_id
        machine.updated_at = datetime.utcnow()
        if machine.status == "unknown":
            machine.status = "creating"
        machine.error_message = None
        db.commit()
        logger.info("Recovered AMI %s for machine template %s", image_id, machine.id)

    def _finalize_recovered_image(
        self, db: Session, manager, machine
    ) -> None:
        """Settle a just-recovered image on the same reconciliation pass.

        ``_persist_recovered_image`` resets ``updated_at`` to now, which would
        otherwise postpone finalization until the row ages past the stale-record
        grace period again. Polling the image state here lets an already
        available (or definitively failed) recovery reach its terminal state
        immediately. A transient describe error or a still-pending image leaves
        the machine "creating"; ``updated_at`` was just refreshed so a later
        pass finalizes it without any further action from this one.
        """
        self._finalize_machine_from_state(db, manager, machine)

    def _recover_machine_image(self, db: Session, manager, machine) -> None:
        """Recover an image for a machine whose creation outcome is unknown.

        Recovery is scoped to the exact capture (room/template/version/builder
        and capture id) and never cleans up or terminates anything.

        Look up an image by the exact capture tags, even if the builder is gone.
        A tag miss cannot distinguish delayed visibility from a request that
        never reached AWS. Keep the row recoverable and never re-issue creation.
        Found images are finalized with the existing conditional state updates.
        """
        from app.ami_manager import AMIError

        template = db.get(RoomTemplate, machine.room_template_id)
        if template is None:
            self._fail_machine(
                db,
                machine,
                "Capture outcome unknown; template record is missing",
            )
            return

        capture = db.scalar(
            select(CaptureRequest).where(
                CaptureRequest.room_template_id == template.id
            )
        )
        tags = capture_image_tags(
            room_id=template.room_id,
            template_id=template.id,
            version=template.version,
            builder_id=machine.source_builder_environment_id,
            capture_id=capture.id if capture else None,
        )

        # 1. Reconcile by exact capture tags (authoritative, side-effect free).
        try:
            existing = manager.find_image_id_by_tags(tags)
        except AMIError as exc:
            logger.warning(
                "Transient error searching for a capture AMI for machine "
                "template %s: %s",
                machine.id,
                exc,
            )
            return
        if existing:
            self._persist_recovered_image(db, machine, existing)
            self._finalize_recovered_image(db, manager, machine)
            return

        logger.debug(
            "Capture outcome for machine template %s remains unknown; "
            "no image found by canonical tags, deferring recovery",
            machine.id,
        )

    async def _discover_orphans(self):
        """Discover and report orphan AWS resources."""
        if self.config["terraform_dry_run"]:
            logger.debug("Dry-run mode: skipping orphan discovery")
            return

        logger.debug("Starting orphan discovery pass")

        try:
            all_resources = await asyncio.to_thread(
                self._discovery_service.discover_all_builder_resources
            )
        except AWSDiscoveryError as e:
            logger.error(f"AWS discovery failed: {e}")
            return
        except Exception as e:
            logger.error(f"Unexpected error during AWS discovery: {e}")
            return

        if not all_resources:
            logger.debug("No SecForge builder resources found in AWS")
            return

        db = SessionLocal()
        try:
            for resources in all_resources:
                if self._orphan_stop_event.is_set():
                    break

                builder_id = resources.builder_id
                builder_db_id = self._validate_builder_id(builder_id)
                if builder_db_id is None:
                    logger.warning(f"Invalid BuilderId format in AWS: {builder_id}")
                    continue

                builder = db.get(BuilderEnvironment, builder_db_id)

                if builder is None:
                    # Orphan candidate: AWS resources exist but no DB record
                    for instance in resources.instances:
                        logger.warning(
                            f"Orphan detected: BuilderId={builder_id} "
                            f"InstanceId={instance.instance_id} "
                            f"State={instance.state} Region={self._discovery_service.region} "
                            f"Reason=No matching BuilderEnvironment record"
                        )
                    for sg in resources.security_groups:
                        logger.warning(
                            f"Orphan detected: BuilderId={builder_id} "
                            f"SecurityGroupId={sg.group_id} "
                            f"GroupName={sg.group_name} Region={self._discovery_service.region} "
                            f"Reason=No matching BuilderEnvironment record"
                        )
                    continue

                # DB record exists - check status
                if builder.status in ("running", "stopping"):
                    logger.debug(
                        f"Builder {builder_id} is active (status={builder.status}), "
                        f"not an orphan"
                    )
                elif builder.status in ("stopped", "failed"):
                    logger.info(
                        f"Builder {builder_id} found in AWS with status={builder.status}, "
                        f"will be handled by normal reconciliation"
                    )
                else:
                    logger.warning(
                        f"Builder {builder_id} found in AWS with unexpected status={builder.status}"
                    )

        finally:
            db.close()

    def _validate_builder_id(self, builder_id: str) -> Optional[int]:
        if not builder_id.startswith("bld-"):
            return None
        try:
            return int(builder_id[4:])
        except ValueError:
            return None


_reconciliation_worker: Optional[ReconciliationWorker] = None


def get_reconciliation_worker() -> ReconciliationWorker:
    global _reconciliation_worker
    if _reconciliation_worker is None:
        _reconciliation_worker = ReconciliationWorker()
    return _reconciliation_worker


@asynccontextmanager
async def reconciliation_lifespan(app):
    """FastAPI lifespan context manager for reconciliation worker."""
    if not settings.reconciliation_enabled:
        yield
        return
    worker = get_reconciliation_worker()
    await worker.start()
    try:
        yield
    finally:
        await worker.stop()
