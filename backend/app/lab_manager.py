import functools
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import case, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, object_session

from app.config import settings
from app.cleanup import (
    CLEANUP_PREFIX, CleanupInventory, CleanupVerificationError,
    cleanup_failed, ownership, state_resources,
)
from app.models import BuilderEnvironment, LabInstance
from app.lab_time import utc_now, deadline_passed
from app.ops_lock import builder_lock_key, lab_lock_key, single_flight
from app.network_ports import DEFAULT_PORTS, room_ports, vpn_source
from app.vpn_authz import (
    VpnAuthorizationError,
    authorize_lab_access,
    sync_vpn_authz,
)

logger = logging.getLogger(__name__)


# First-boot user_data injected into learner lab instances that boot from a
# room-template AMI. It regenerates per-instance identity so clones never share
# the builder's machine-id or SSH host keys. It is idempotent (guarded by a
# marker) and never touches challenge state, users, files, or flags.
LAB_FIRST_BOOT_USER_DATA = """#!/bin/bash
set -e

MARKER=/var/lib/secforge/identity-initialized
if [ -f "$MARKER" ]; then
  exit 0
fi

# 1. Regenerate the machine-id via the normal systemd mechanism.
: > /etc/machine-id
if [ -L /var/lib/dbus/machine-id ]; then
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
elif [ -e /var/lib/dbus/machine-id ]; then
  rm -f /var/lib/dbus/machine-id
fi
systemd-machine-id-setup
if [ -e /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
  cp -f /etc/machine-id /var/lib/dbus/machine-id
fi

# 2. Regenerate SSH host keys.
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
ssh-keygen -A

# 3. Keep SSH available.
systemctl enable ssh 2>/dev/null || true
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

mkdir -p /var/lib/secforge
touch "$MARKER"
"""


class TerraformError(Exception):
    def __init__(self, message: str, stdout: str = "", stderr: str = "", returncode: int = -1):
        self.message = message
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(message)


class TemplateResolutionError(Exception):
    """Raised when a saved challenge image cannot be resolved safely.

    This is intentionally distinct from Terraform failures so an authorized test
    launch can fail clearly instead of silently falling back to the global AMI.
    """


def _guard_terraform(kind: str):
    """Serialize Terraform operations per builder/lab instance.

    Acquires a non-blocking single-flight guard for the resource being operated
    on before the method body runs (so no status mutation happens without the
    guard) and releases it via ``finally``. Raises ``OperationInProgress`` when
    another operation already holds the guard for the same resource.
    """

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self, resource, *args, **kwargs):
            key = (
                lab_lock_key(resource.id)
                if kind == "lab"
                else builder_lock_key(resource.id)
            )
            with single_flight(key):
                return method(self, resource, *args, **kwargs)

        return wrapper

    return decorator


class LabManager:
    def __init__(self):
        self.config = {
            "instance_type": "t3.micro",
            "ami_id": settings.builder_ami_id,
            "subnet_id": settings.builder_subnet_id,
            "vpc_id": settings.builder_vpc_id,
            "ssh_key_name": settings.builder_ssh_key_name,
            "vpn_cidr": settings.builder_vpn_cidr,
            "builder_management_security_group_id": settings.builder_management_security_group_id,
            "terraform_workspace_root": settings.builder_terraform_workspace_root,
            "terraform_source_dir": settings.builder_terraform_source_dir,
            "terraform_plugin_cache_dir": settings.terraform_plugin_cache_dir,
            "aws_region": settings.aws_region,
            "terraform_binary": settings.terraform_binary,
            "terraform_timeout_seconds": settings.terraform_timeout_seconds,
            "terraform_dry_run": settings.terraform_dry_run,
            "terraform_state_bucket": settings.terraform_state_bucket,
            "terraform_state_key_prefix": settings.terraform_state_key_prefix,
            "lab_terraform_source_dir": settings.lab_terraform_source_dir,
            "lab_terraform_workspace_root": settings.lab_terraform_workspace_root,
            "lab_terraform_state_key_prefix": settings.lab_terraform_state_key_prefix,
        }

    def _get_safe_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        # Remove sensitive AWS credentials from env if present
        sensitive_keys = [
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_DEFAULT_PROFILE",
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
        ]
        for key in sensitive_keys:
            env.pop(key, None)
        # Also remove SecForge application secrets
        app_secret_keys = [
            "JWT_SECRET",
            "CHALLENGE_ANSWER_KEY",
            "DATABASE_URL",
        ]
        for key in app_secret_keys:
            env.pop(key, None)
        # Ensure consistent locale
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        # Set Terraform plugin cache directory (expand ~ to absolute path)
        cache_dir = os.path.expanduser(self.config["terraform_plugin_cache_dir"])
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        env["TF_PLUGIN_CACHE_DIR"] = cache_dir
        return env

    def _run_terraform(
        self,
        args: list[str],
        cwd: str,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        dry_run: Optional[bool] = None,
    ) -> subprocess.CompletedProcess:
        use_dry_run = dry_run if dry_run is not None else self.config["terraform_dry_run"]

        if use_dry_run:
            # Simulate success in dry-run mode
            # Simulate terraform output -json for dry-run mode
            if args == ["output", "-json"]:
                simulated_outputs = {
                    "instance_id": {"value": f"i-dryrun-{int(time.time())}"},
                    "private_ip": {"value": "10.0.1.100"},
                    "security_group_id": {"value": "sg-dryrun123"},
                }
                return subprocess.CompletedProcess(
                    args=[self.config["terraform_binary"]] + args,
                    returncode=0,
                    stdout=json.dumps(simulated_outputs),
                    stderr="",
                )
            # Simulate terraform show -json for dry-run mode
            if args == ["show", "-json"]:
                simulated_state = {
                    "values": {
                        "root_module": {
                            "resources": [
                                {
                                    "type": "aws_instance",
                                    "name": "builder",
                                    "values": {
                                        "id": f"i-dryrun-{int(time.time())}",
                                        "private_ip": "10.0.1.100",
                                    }
                                }
                            ]
                        }
                    }
                }
                return subprocess.CompletedProcess(
                    args=[self.config["terraform_binary"]] + args,
                    returncode=0,
                    stdout=json.dumps(simulated_state),
                    stderr="",
                )
            # Simulate success for other commands (init, apply, destroy)
            return subprocess.CompletedProcess(
                args=[self.config["terraform_binary"]] + args,
                returncode=0,
                stdout="",
                stderr="",
            )

        if timeout is None:
            timeout = self.config["terraform_timeout_seconds"]

        cmd = [self.config["terraform_binary"]] + args
        env = self._get_safe_env()

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=capture_output,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result
        except subprocess.TimeoutExpired as e:
            raise TerraformError(
                f"Terraform command timed out after {timeout} seconds",
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                returncode=-1,
            )
        except FileNotFoundError:
            raise TerraformError(
                f"Terraform binary not found: {self.config['terraform_binary']}",
                returncode=-1,
            )
        except Exception as e:
            raise TerraformError(
                f"Failed to execute terraform: {e}",
                returncode=-1,
            )

    def _copy_terraform_source(
        self, workspace_path: Path, source_dir: Optional[str] = None
    ) -> None:
        """Copy Terraform configuration files from the source directory into a workspace.

        State files and the .terraform directory are never copied.
        """
        source_path = Path(source_dir or self.config["terraform_source_dir"])

        if not source_path.exists():
            raise TerraformError(f"Terraform source directory not found: {source_path}")

        for item in source_path.iterdir():
            if item.name in ("terraform.tfstate", "terraform.tfstate.backup", ".terraform"):
                continue
            dest = workspace_path / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # Copy lock file to ensure provider version consistency
        source_lock = source_path / ".terraform.lock.hcl"
        if source_lock.exists():
            dest_lock = workspace_path / ".terraform.lock.hcl"
            shutil.copy2(source_lock, dest_lock)

    def _prepare_workspace(self, builder_env: BuilderEnvironment) -> str:
        workspace_path = Path(builder_env.terraform_state_path)

        # Create workspace directory
        workspace_path.mkdir(parents=True, exist_ok=True)

        # Copy terraform configuration files to workspace
        self._copy_terraform_source(workspace_path)

        # Write Terraform backend configuration for S3 state storage
        backend_config = self._generate_backend_config(builder_env)
        backend_path = workspace_path / "backend.tf"
        backend_path.write_text(backend_config)

        # Write terraform.tfvars
        tfvars_content = self._generate_tfvars(builder_env)
        tfvars_path = workspace_path / "terraform.tfvars"
        tfvars_path.write_text(tfvars_content)

        return str(workspace_path)

    def _cleanup_workspace(self, workspace_path: str) -> None:
        """Safely remove a Terraform workspace directory."""
        path = Path(workspace_path)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def _generate_backend_config(self, builder_env: BuilderEnvironment) -> str:
        """Generate Terraform backend configuration for S3 state storage."""
        state_key = f"{self.config['terraform_state_key_prefix']}/{builder_env.id}/terraform.tfstate"
        return f"""
terraform {{
  backend "s3" {{
    bucket         = "{self.config['terraform_state_bucket']}"
    key            = "{state_key}"
    region         = "{self.config['aws_region']}"
    encrypt        = true
    use_lockfile   = true
  }}
}}
"""

    def _generate_tfvars(self, builder_env: BuilderEnvironment) -> str:
        try:
            # Destroy reconstructs variables but never applies ingress. A changed
            # or invalid room document must not prevent owned-resource cleanup.
            ports = DEFAULT_PORTS if builder_env.status == 'stopping' else room_ports(object_session(builder_env), builder_env, 'builder')
            vpn_source(self.config['vpn_cidr'])
        except ValueError as exc:
            raise TerraformError('Invalid room network policy') from exc
        tags = {
            "Project": "SecForge",
            "ManagedBy": "Terraform",
            "ResourceType": "builder",
            "BuilderId": f"bld-{builder_env.id}",
            "RoomId": str(builder_env.room_id),
            "CreatorId": str(builder_env.creator_id),
            "Environment": "builder",
        }
        tfvars = f"""
builder_id       = "bld-{builder_env.id}"
creator_id       = {builder_env.creator_id}
room_id          = {builder_env.room_id}
ami_id           = "{builder_env.source_ami_id or self.config['ami_id']}"
instance_type    = "{self.config['instance_type']}"
subnet_id        = "{self.config['subnet_id']}"
vpc_id           = "{self.config['vpc_id']}"
ssh_key_name     = "{self.config['ssh_key_name']}"
vpn_cidr         = "{self.config['vpn_cidr']}"
tags             = {json.dumps(tags)}
"""
        management_group = self.config.get("builder_management_security_group_id", "")
        tfvars += f'exposed_ports = {json.dumps(ports)}\n'
        if management_group:
            tfvars += f"management_security_group_id = {json.dumps(management_group)}\n"
        return tfvars

    def _parse_terraform_outputs(self, workspace_path: str) -> Dict[str, Any]:
        result = self._run_terraform(
            ["output", "-json"],
            cwd=workspace_path,
        )
        if result.returncode != 0:
            raise TerraformError(
                "Failed to get terraform outputs",
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )

        try:
            outputs = json.loads(result.stdout)
            # Extract values from Terraform output format
            parsed = {}
            for key, value in outputs.items():
                if isinstance(value, dict) and "value" in value:
                    parsed[key] = value["value"]
                else:
                    parsed[key] = value
            return parsed
        except json.JSONDecodeError as e:
            raise TerraformError(
                f"Failed to parse terraform output JSON: {e}",
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )

    def _validate_outputs(self, outputs: Dict[str, Any]) -> None:
        required = ["instance_id", "private_ip", "security_group_id"]
        for key in required:
            if key not in outputs:
                raise TerraformError(f"Missing required terraform output: {key}")
            if not outputs[key] or not isinstance(outputs[key], str):
                raise TerraformError(f"Invalid terraform output {key}: {outputs[key]}")

    @_guard_terraform("builder")
    def start_builder(self, builder_env: BuilderEnvironment) -> None:
        """Provision the builder environment using Terraform."""
        # Clear any previous error
        builder_env.error_message = None

        # Update status to provisioning
        builder_env.status = "provisioning"

        workspace_path = builder_env.terraform_state_path

        try:
            workspace_path = self._prepare_workspace(builder_env)
            # terraform init
            result = self._run_terraform(
                ["init", "-input=false"],
                cwd=workspace_path,
            )
            if result.returncode != 0:
                raise TerraformError(
                    "Terraform init failed",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )

            # terraform apply
            result = self._run_terraform(
                ["apply", "-input=false", "-auto-approve"],
                cwd=workspace_path,
            )
            if result.returncode != 0:
                raise TerraformError(
                    "Terraform apply failed",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )

            # Get outputs
            outputs = self._parse_terraform_outputs(workspace_path)
            self._validate_outputs(outputs)

            # Persist outputs to builder environment
            builder_env.instance_id = outputs["instance_id"]
            builder_env.private_ip = outputs["private_ip"]
            builder_env.security_group_id = outputs["security_group_id"]
            builder_env.status = "running"
            builder_env.error_message = None

        except TerraformError as e:
            builder_env.status = "failed"
            # Sanitize error message - don't expose secrets
            error_msg = str(e.message)
            builder_env.error_message = error_msg
            raise
        finally:
            # Clean up workspace on both success and failure (init/apply)
            # State is in S3, outputs are persisted to DB
            # Skip cleanup in dry-run mode so tests can inspect generated files
            if workspace_path and builder_env.status in ("running", "failed") and not self.config["terraform_dry_run"]:
                self._cleanup_workspace(workspace_path)

    @_guard_terraform("builder")
    def stop_builder(self, builder_env: BuilderEnvironment) -> None:
        """Retry the same owned Terraform cleanup, preserving Stop intent."""
        self._stop_environment(builder_env, object_session(builder_env), "builder")

    def _cleanup_inventory(self):
        return CleanupInventory(self.config["aws_region"])

    def _cleanup_command(self, args, workspace):
        try:
            result = self._run_terraform(args, cwd=workspace)
        except TerraformError as exc:
            # Execution exceptions can include paths or provider output. Keep
            # only the stage and a safe category in the persisted diagnostic.
            category = "timed out" if exc.returncode == -1 and "timed out" in exc.message else "execution failed"
            raise TerraformError(f"{args[0]} {category}; inspect protected diagnostics") from exc
        if result.returncode:
            # Retain actionable categories without persisting raw provider output.
            text = (result.stderr or "").lower()
            category = next((label for label, marker in [
                ("state lock busy", "state lock"), ("credentials", "credential"),
                ("access denied", "accessdenied"), ("network", "connect"),
                ("provider setup", "provider"),
            ] if marker in text), "inspect protected Terraform diagnostics")
            raise TerraformError(f"{args[0]} failed (exit {result.returncode}; {category})",
                                 stdout=result.stdout, stderr=result.stderr,
                                 returncode=result.returncode)
        return result

    def _destroy_environment(self, resource, kind):
        root = Path(self.config["terraform_workspace_root" if kind == "builder" else "lab_terraform_workspace_root"]).resolve()
        workspace = Path(resource.terraform_state_path).resolve()
        if workspace == root or root not in workspace.parents:
            raise CleanupVerificationError("workspace outside configured environment root; review required")
        expected_backend = (self._generate_backend_config(resource) if kind == "builder"
                            else self._generate_lab_backend_config(resource))
        backend = workspace / "backend.tf"
        if backend.exists() and backend.read_text() != expected_backend:
            raise CleanupVerificationError("workspace remote-state target mismatch; no destroy attempted")
        if not self.config["terraform_dry_run"] and workspace.exists() and any(workspace.iterdir()) and not backend.exists():
            raise CleanupVerificationError("unidentified existing workspace; state ownership review required")
        # A directory left by a failed init is not an initialized workspace.
        if not backend.exists():
            if kind == "builder":
                self._prepare_workspace(resource)
            else:
                self._prepare_lab_workspace(resource, self.config["ami_id"])
        self._cleanup_command(["init", "-reconfigure", "-input=false"], str(workspace))
        if self.config["terraform_dry_run"]:
            self._cleanup_command(["destroy", "-input=false", "-auto-approve"], str(workspace))
            return str(workspace)
        inventory = self._cleanup_inventory()
        expected = ownership(resource, kind)
        remaining = inventory.remaining(expected, resource)
        state = self._pull_cleanup_state(str(workspace))
        owned = (state_resources(state, expected, resource) if state is not None
                 else {"aws_instance": set(), "aws_security_group": set()})
        if any(not remaining[k].issubset(owned[k]) for k in remaining):
            raise CleanupVerificationError("owned AWS resources missing from Terraform state; repair retained state before retry")
        if any(owned.values()):
            self._cleanup_command(["destroy", "-input=false", "-auto-approve"], str(workspace))
        after = self._pull_cleanup_state(str(workspace))
        if after is not None and any(state_resources(after, expected, resource).values()):
            raise CleanupVerificationError("Terraform state still contains managed resources after destroy")
        if any(inventory.remaining(expected, resource).values()):
            raise CleanupVerificationError("AWS resources still exist after destroy; reconciliation will retry")
        return str(workspace)

    def _pull_cleanup_state(self, workspace):
        try:
            result = self._cleanup_command(["state", "pull"], workspace)
        except TerraformError as exc:
            # A Stop before apply may legitimately have no state. Only this
            # explicit Terraform result is absence; permissions/network errors
            # remain failures. AWS absence must still be independently verified.
            if exc.returncode == 1 and "No state file was found" in (exc.stderr + exc.stdout):
                return None
            raise
        return json.loads(result.stdout)

    def _stop_environment(self, resource, db, kind):
        model = BuilderEnvironment if kind == "builder" else LabInstance
        if db is not None:
            db.refresh(resource)
        if resource.status == "stopped":
            return
        eligible = ("pending", "provisioning", "running", "stopping")
        if resource.status not in eligible and not cleanup_failed(resource):
            return
        updates = {"status": "stopping", "updated_at": datetime.utcnow()}
        if kind == "lab":
            updates["terraform_state_path"] = resource.terraform_state_path or self.lab_workspace_path(resource)
        if db is not None:
            claimed = db.execute(update(model).where(model.id == resource.id, model.status == resource.status)
                                 .values(**updates))
            db.commit()
            db.refresh(resource)
            if not claimed.rowcount: return
            try: sync_vpn_authz(db)
            except VpnAuthorizationError:
                logger.warning("VPN authorization removal failed; reconciliation will retry")
        else:
            for key, value in updates.items(): setattr(resource, key, value)
        try:
            workspace = self._destroy_environment(resource, kind)
        except Exception as exc:
            message = CLEANUP_PREFIX + (str(exc.message) if isinstance(exc, TerraformError)
                                       else str(exc) if isinstance(exc, CleanupVerificationError)
                                       else f"{type(exc).__name__}; inspect protected diagnostics")
            values = {"error_message": message, "updated_at": datetime.utcnow()}
            if db is not None:
                db.execute(update(model).where(model.id == resource.id, model.status == "stopping").values(**values))
                db.commit()
                db.refresh(resource)
            else:
                for key, value in values.items(): setattr(resource, key, value)
            raise TerraformError(message) from exc
        values = {"status": "stopped", "instance_id": None, "private_ip": None,
                  "security_group_id": None, "error_message": None,
                  "stopped_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
        completed = True
        if db is not None:
            try:
                completed = bool(db.execute(update(model).where(model.id == resource.id, model.status == "stopping")
                                            .values(**values)).rowcount)
                db.commit()
                db.refresh(resource)
            except SQLAlchemyError as exc:
                db.rollback()
                # The committed Stop and workspace survive. A retry verifies
                # absence instead of assuming a lost completion commit means
                # that Terraform must be destroyed again.
                raise TerraformError(CLEANUP_PREFIX + "database completion failed; retry verification") from exc
        else:
            for key, value in values.items(): setattr(resource, key, value)
        if completed:
            self._cleanup_workspace(workspace)

    def get_builder_status(self, builder_env: BuilderEnvironment) -> Dict[str, Any]:
        """Get the current status of the builder from Terraform state."""
        workspace_path = builder_env.terraform_state_path

        if not Path(workspace_path).exists():
            return {"status": "not_initialized"}

        try:
            # Check if terraform state exists
            result = self._run_terraform(
                ["show", "-json"],
                cwd=workspace_path,
            )
            if result.returncode != 0:
                return {"status": "unknown", "error": "terraform show failed"}

            state = json.loads(result.stdout)
            # Extract instance state if available
            return {"status": "initialized", "state": state}
        except FileNotFoundError:
            return {"status": "not_initialized"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def lab_workspace_path(self, lab: LabInstance) -> str:
        """Local Terraform workspace path for a learner lab instance."""
        return str(Path(self.config["lab_terraform_workspace_root"]) / f"lab-{lab.id}")

    def lab_state_key(self, lab: LabInstance) -> str:
        """S3 state key for a learner lab instance."""
        prefix = self.config["lab_terraform_state_key_prefix"]
        return f"{prefix}/{lab.id}/terraform.tfstate"

    def _generate_lab_backend_config(self, lab: LabInstance) -> str:
        """Generate Terraform backend configuration for a learner lab instance."""
        return f"""
terraform {{
  backend "s3" {{
    bucket         = "{self.config['terraform_state_bucket']}"
    key            = "{self.lab_state_key(lab)}"
    region         = "{self.config['aws_region']}"
    encrypt        = true
    use_lockfile   = true
  }}
}}
"""

    def _generate_lab_user_data(self) -> str:
        """First-boot identity regeneration for template-based learner labs."""
        return LAB_FIRST_BOOT_USER_DATA

    def _generate_lab_tfvars(
        self,
        lab: LabInstance,
        ami_id: Optional[str] = None,
        user_data: Optional[str] = None,
    ) -> str:
        try:
            ports = DEFAULT_PORTS if lab.status == 'stopping' else room_ports(object_session(lab), lab, 'lab')
            vpn_source(self.config['vpn_cidr'])
        except ValueError as exc:
            raise TerraformError('Invalid room network policy') from exc
        tags = {
            "Project": "SecForge",
            "ManagedBy": "Terraform",
            "ResourceType": "lab",
            "LabInstanceId": f"lab-{lab.id}",
            "RoomId": str(lab.room_id),
            "LearnerId": str(lab.learner_id),
            "Environment": "learner-lab",
        }
        if not ami_id:
            raise TemplateResolutionError("A saved challenge image is required")
        resolved_ami = ami_id
        return f"""
lab_instance_id  = "lab-{lab.id}"
learner_id       = {lab.learner_id}
room_id          = {lab.room_id}
ami_id           = "{resolved_ami}"
instance_type    = "{self.config['instance_type']}"
subnet_id        = "{self.config['subnet_id']}"
vpc_id           = "{self.config['vpc_id']}"
ssh_key_name     = "{self.config['ssh_key_name']}"
vpn_cidr         = "{self.config['vpn_cidr']}"
user_data        = {json.dumps(user_data or "")}
exposed_ports    = {json.dumps(ports)}
tags             = {json.dumps(tags)}
"""

    def _prepare_lab_workspace(
        self,
        lab: LabInstance,
        ami_id: Optional[str] = None,
        user_data: Optional[str] = None,
    ) -> str:
        workspace_path = Path(lab.terraform_state_path)

        workspace_path.mkdir(parents=True, exist_ok=True)
        self._copy_terraform_source(
            workspace_path, self.config["lab_terraform_source_dir"]
        )

        backend_path = workspace_path / "backend.tf"
        backend_path.write_text(self._generate_lab_backend_config(lab))

        tfvars_path = workspace_path / "terraform.tfvars"
        tfvars_path.write_text(self._generate_lab_tfvars(lab, ami_id, user_data))

        return str(workspace_path)

    def _sanitize_error_message(self, message: str) -> str:
        """Redact secrets and bound the length of a Terraform error message."""
        if not message:
            return "Terraform operation failed"

        sanitized = re.sub(
            r"(?i)\b(aws_secret_access_key|aws_session_token|aws_access_key_id"
            r"|password|secret|token)\s*[=:]\s*\S+",
            r"\1=[REDACTED]",
            message,
        )
        sanitized = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "AKIA[REDACTED]", sanitized)
        sanitized = re.sub(r"(?i)\bbearer\s+\S+", "bearer [REDACTED]", sanitized)

        if len(sanitized) > 500:
            sanitized = sanitized[:497] + "..."
        return sanitized

    def _record_lab_provisioning_failure(
        self, db: Session, lab: LabInstance, message: str
    ) -> bool:
        """Fail provisioning without erasing a committed request to stop it."""
        lab_id = lab.id
        db.rollback()
        failed = db.execute(
            update(LabInstance)
            .where(LabInstance.id == lab_id,
                   LabInstance.status.in_(("provisioning", "stopping")))
            .values(
                status=case(((LabInstance.status == "provisioning") & LabInstance.session_started_at.is_not(None), "stopping"),
                            (LabInstance.status == "provisioning", "failed"),
                            else_=LabInstance.status),
                error_message=(CLEANUP_PREFIX if lab.session_started_at is not None else "") + self._sanitize_error_message(message),
                updated_at=case((LabInstance.status == "provisioning", datetime.utcnow()),
                                else_=LabInstance.updated_at),
            )
        )
        db.commit()
        db.refresh(lab)
        return bool(failed.rowcount and lab.status == "failed")

    @_guard_terraform("lab")
    def start_lab_instance(
        self,
        lab: LabInstance,
        db: Session,
        room_template_id: Optional[int] = None,
        expected_ami_id: Optional[str] = None,
    ) -> None:
        """Revalidate the selected saved image before any Terraform operation."""
        db.refresh(lab)
        if lab.status != "pending":
            return
        if lab.session_started_at is not None and deadline_passed(lab):
            db.execute(update(LabInstance).where(
                LabInstance.id == lab.id, LabInstance.status == "pending", LabInstance.expires_at <= utc_now(),
            ).values(status="stopping", expired_at=utc_now(), updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
            db.commit()
            db.refresh(lab)
            return
        lab_id = lab.id
        workspace_path = lab.terraform_state_path or self.lab_workspace_path(lab)
        provisioning_started = False
        cleanup_workspace = False
        try:
            from app.templates import resolve_lab_ami

            try:
                resolved_ami = resolve_lab_ami(
                    db, lab.room_id, room_template_id, expected_ami_id
                )
            except SQLAlchemyError as exc:
                db.rollback()
                raise TemplateResolutionError("Unable to resolve the room challenge image") from exc
            if not resolved_ami:
                message = (
                    "Selected template version has no available AMI"
                    if room_template_id is not None
                    else "Room has no available saved challenge image"
                )
                raise TemplateResolutionError(message)
            if expected_ami_id is not None and resolved_ami != expected_ami_id:
                raise TemplateResolutionError("Selected challenge image changed before provisioning")

            # A stop request may have committed during resolution. Claim only a
            # pending lab, rather than resurrecting a stopped/stopping instance.
            claimed = db.execute(
                update(LabInstance)
                .where(LabInstance.id == lab_id, LabInstance.status == "pending")
                .values(status="provisioning", error_message=None,
                        terraform_state_path=workspace_path, updated_at=datetime.utcnow())
            )
            db.commit()
            db.refresh(lab)
            if not claimed.rowcount:
                return
            provisioning_started = True
            resolved_user_data = self._generate_lab_user_data()
            workspace_path = self._prepare_lab_workspace(
                lab, resolved_ami, resolved_user_data
            )

            result = self._run_terraform(
                ["init", "-input=false"],
                cwd=workspace_path,
            )
            if result.returncode != 0:
                raise TerraformError(
                    "Terraform init failed",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )

            result = self._run_terraform(
                ["apply", "-input=false", "-auto-approve"],
                cwd=workspace_path,
            )
            if result.returncode != 0:
                raise TerraformError(
                    "Terraform apply failed",
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                )

            outputs = self._parse_terraform_outputs(workspace_path)
            self._validate_outputs(outputs)

            # Record cleanup metadata even if Stop committed during apply, but
            # never revive a terminal row or replace its newer metadata.
            db.execute(
                update(LabInstance)
                .where(LabInstance.id == lab_id,
                       LabInstance.status.in_(("provisioning", "stopping")))
                .values(instance_id=outputs["instance_id"],
                        private_ip=outputs["private_ip"],
                        security_group_id=outputs["security_group_id"],
                        updated_at=LabInstance.updated_at)
            )
            db.commit()
            db.refresh(lab)
            if lab.status != "provisioning":
                return
            if lab.session_started_at is not None and deadline_passed(lab):
                db.execute(update(LabInstance).where(
                    LabInstance.id == lab_id, LabInstance.status == "provisioning",
                    LabInstance.expires_at <= utc_now(),
                ).values(status="stopping", expired_at=utc_now(), updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
                db.commit()
                db.refresh(lab)
                return  # Workspace/outputs stay available to expiration recovery.
            authorize_lab_access(db, lab)
            completed = db.execute(
                update(LabInstance)
                .where(LabInstance.id == lab_id, LabInstance.status == "provisioning",
                       (LabInstance.session_started_at.is_(None) | (LabInstance.expires_at > utc_now())))
                .values(status="running", error_message=None, updated_at=datetime.utcnow()).execution_options(synchronize_session=False)
            )
            db.commit()
            db.refresh(lab)
            cleanup_workspace = bool(completed.rowcount)

        except TemplateResolutionError as exc:
            # Roll back failed reads before recording failure, and never replace
            # a stop or another terminal transition with this failed launch.
            db.rollback()
            db.execute(
                update(LabInstance)
                .where(LabInstance.id == lab_id, LabInstance.status == "pending")
                .values(status="failed", error_message=str(exc), updated_at=datetime.utcnow())
            )
            db.commit()
            db.refresh(lab)
            raise
        except VpnAuthorizationError as e:
            cleanup_workspace = self._record_lab_provisioning_failure(db, lab, str(e))
            raise
        except TerraformError as e:
            cleanup_workspace = self._record_lab_provisioning_failure(db, lab, str(e.message))
            raise
        finally:
            if (
                provisioning_started
                and cleanup_workspace
                and lab.status in ("running", "failed")
                and not self.config["terraform_dry_run"]
            ):
                self._cleanup_workspace(workspace_path)

    @_guard_terraform("lab")
    def stop_lab_instance(self, lab: LabInstance, db: Session) -> None:
        """Retry cleanup through the same guarded, conditional lifecycle."""
        self._stop_environment(lab, db, "lab")
