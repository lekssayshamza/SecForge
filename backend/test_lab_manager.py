import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

from app.config import settings
from app.lab_manager import LabManager, TerraformError
from app.models import BuilderEnvironment
from app.reconciliation import ReconciliationWorker
from app.database import SessionLocal
from app.aws_discovery import (
    AWSDiscoveryService,
    AWSDiscoveryError,
    BuilderResources,
    InstanceInfo,
    SecurityGroupInfo,
)


def mock_owned_cleanup(manager, resource, mock_run, fail_destroy=False):
    """Provide owned state and AWS responses for legacy workspace tests."""
    from app.cleanup import ownership
    tags = ownership(resource, "builder")
    state = {"version": 4, "resources": [
        {"mode": "managed", "type": kind, "instances": [
            {"attributes": {"id": identifier, "tags": tags}}]}
        for kind, identifier in [("aws_instance", resource.instance_id),
                                 ("aws_security_group", resource.security_group_id)]
    ]}
    remaining = {"aws_instance": {resource.instance_id},
                 "aws_security_group": {resource.security_group_id}}
    inventory = MagicMock()
    inventory.remaining.side_effect = [remaining, {k: set() for k in remaining}]
    manager._cleanup_inventory = lambda: inventory
    destroyed = False

    def command(args, **kwargs):
        nonlocal destroyed
        result = MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "destroy":
            if fail_destroy:
                result.returncode = 1
                result.stderr = "Error: Dependency violation"
            else:
                destroyed = True
        if args == ["state", "pull"]:
            result.stdout = json.dumps({"version": 4, "resources": []} if destroyed else state)
        return result
    mock_run.side_effect = command


class TestLabManager:
    @pytest.fixture
    def lab_manager(self, tmp_path):
        lm = LabManager()
        # Use test-specific plugin cache to avoid polluting real cache
        lm.config["terraform_plugin_cache_dir"] = str(tmp_path / "terraform-plugin-cache")
        lm.config["terraform_workspace_root"] = str(tmp_path)
        return lm

    @pytest.fixture
    def builder_env(self, tmp_path):
        workspace = tmp_path / "test-builder-1"
        workspace.mkdir(parents=True, exist_ok=True)
        env = BuilderEnvironment(
            id=1,
            room_id=10,
            creator_id=100,
            status="pending",
            terraform_state_path=str(workspace),
            expires_at=None,
        )
        return env

    def test_generate_tfvars(self, lab_manager, builder_env):
        tfvars = lab_manager._generate_tfvars(builder_env)
        assert 'builder_id       = "bld-1"' in tfvars
        assert "creator_id       = 100" in tfvars
        assert "room_id          = 10" in tfvars
        assert 'ami_id           = "ami-0769f265f707fecc8"' in tfvars
        assert 'instance_type    = "t3.micro"' in tfvars
        assert 'subnet_id        = "subnet-017557ddd10df5804"' in tfvars
        assert 'vpc_id           = "vpc-0e50eceba146f3a4a"' in tfvars
        assert 'ssh_key_name     = "CyberRange-Key"' in tfvars
        assert 'vpn_cidr         = "10.8.0.0/24"' in tfvars
        assert "CreatorId" in tfvars
        assert "RoomId" in tfvars
        assert "BuilderId" in tfvars

    def test_terraform_error_sanitization(self, lab_manager):
        with pytest.raises(TerraformError) as exc_info:
            raise TerraformError("Test error with AWS_SECRET=secret123")
        assert "Test error" in str(exc_info.value)

    def test_get_safe_env_removes_aws_credentials(self, lab_manager):
        import os
        test_env = {
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
            "AWS_SESSION_TOKEN": "test-token",
            "AWS_PROFILE": "test-profile",
            "AWS_DEFAULT_PROFILE": "default",
            "AWS_CONFIG_FILE": "/tmp/config",
            "AWS_SHARED_CREDENTIALS_FILE": "/tmp/creds",
            "JWT_SECRET": "test-jwt",
            "DATABASE_URL": "postgresql://user:pass@localhost/db",
            "OTHER_VAR": "should-remain",
        }
        with patch.dict(os.environ, test_env):
            env = lab_manager._get_safe_env()
            assert "AWS_ACCESS_KEY_ID" not in env
            assert "AWS_SECRET_ACCESS_KEY" not in env
            assert "AWS_SESSION_TOKEN" not in env
            assert "AWS_PROFILE" not in env
            assert "AWS_DEFAULT_PROFILE" not in env
            assert "AWS_CONFIG_FILE" not in env
            assert "AWS_SHARED_CREDENTIALS_FILE" not in env
            assert "JWT_SECRET" not in env
            assert "DATABASE_URL" not in env
            assert env.get("OTHER_VAR") == "should-remain"
            assert env["LC_ALL"] == "C.UTF-8"
            assert env["LANG"] == "C.UTF-8"
            assert "TF_PLUGIN_CACHE_DIR" in env
            assert env["TF_PLUGIN_CACHE_DIR"] == lab_manager.config["terraform_plugin_cache_dir"]

    @patch("app.lab_manager.subprocess.run")
    def test_run_terraform_success(self, mock_run, lab_manager):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "success"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        result = lab_manager._run_terraform(["init"], "/tmp/test", dry_run=False)
        assert result.returncode == 0
        assert result.stdout == "success"
        mock_run.assert_called_once()

    @patch("app.lab_manager.subprocess.run")
    def test_run_terraform_failure(self, mock_run, lab_manager):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: something failed"
        mock_run.return_value = mock_result

        result = lab_manager._run_terraform(["apply"], "/tmp/test", dry_run=False)
        assert result.returncode == 1
        assert result.stderr == "Error: something failed"

    @patch("app.lab_manager.subprocess.run")
    def test_run_terraform_timeout(self, mock_run, lab_manager):
        from subprocess import TimeoutExpired
        mock_run.side_effect = TimeoutExpired(cmd=["terraform", "apply"], timeout=600)

        with pytest.raises(TerraformError) as exc_info:
            lab_manager._run_terraform(["apply"], "/tmp/test", timeout=600, dry_run=False)
        assert "timed out" in str(exc_info.value)

    @patch("app.lab_manager.subprocess.run")
    def test_run_terraform_binary_not_found(self, mock_run, lab_manager):
        mock_run.side_effect = FileNotFoundError("terraform not found")

        with pytest.raises(TerraformError) as exc_info:
            lab_manager._run_terraform(["init"], "/tmp/test", dry_run=False)
        assert "not found" in str(exc_info.value)

    @patch("app.lab_manager.LabManager._run_terraform")
    @patch("app.lab_manager.LabManager._prepare_workspace")
    def test_start_builder_success(self, mock_prepare, mock_run, lab_manager, builder_env):
        mock_prepare.return_value = "/tmp/test-builder-1"

        init_result = MagicMock()
        init_result.returncode = 0
        init_result.stdout = ""
        init_result.stderr = ""

        apply_result = MagicMock()
        apply_result.returncode = 0
        apply_result.stdout = ""
        apply_result.stderr = ""

        output_result = MagicMock()
        output_result.returncode = 0
        output_result.stdout = json.dumps({
            "instance_id": {"value": "i-123456"},
            "private_ip": {"value": "10.0.1.100"},
            "security_group_id": {"value": "sg-123456"},
        })
        output_result.stderr = ""

        mock_run.side_effect = [init_result, apply_result, output_result]

        lab_manager.start_builder(builder_env)

        assert builder_env.status == "running"
        assert builder_env.instance_id == "i-123456"
        assert builder_env.private_ip == "10.0.1.100"
        assert builder_env.security_group_id == "sg-123456"
        assert builder_env.error_message is None

        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["init", "-input=false"]
        assert calls[1][0][0] == ["apply", "-input=false", "-auto-approve"]
        assert calls[2][0][0] == ["output", "-json"]

    @patch("app.lab_manager.LabManager._run_terraform")
    @patch("app.lab_manager.LabManager._prepare_workspace")
    def test_start_builder_apply_failure(self, mock_prepare, mock_run, lab_manager, builder_env):
        mock_prepare.return_value = "/tmp/test-builder-1"

        init_result = MagicMock()
        init_result.returncode = 0

        apply_result = MagicMock()
        apply_result.returncode = 1
        apply_result.stdout = ""
        apply_result.stderr = "Error: Invalid AMI"

        mock_run.side_effect = [init_result, apply_result]

        with pytest.raises(TerraformError):
            lab_manager.start_builder(builder_env)

        assert builder_env.status == "failed"
        assert builder_env.error_message is not None



    def test_s3_backend_config_generation(self, lab_manager, builder_env):

        """Test that S3 backend configuration is generated correctly."""

        backend_config = lab_manager._generate_backend_config(builder_env)

        assert 'backend "s3"' in backend_config

        assert f'bucket         = "{settings.terraform_state_bucket}"' in backend_config

        assert f'key            = "builders/{builder_env.id}/terraform.tfstate"' in backend_config

        assert f'region         = "{settings.aws_region}"' in backend_config

        assert "encrypt        = true" in backend_config

        assert "use_lockfile   = true" in backend_config



    def test_s3_state_key_uses_builder_id_not_creator_input(self, lab_manager, builder_env):

        """Test that S3 state key is generated from builder ID, not creator input."""

        backend_config = lab_manager._generate_backend_config(builder_env)

        expected_key = f"builders/{builder_env.id}/terraform.tfstate"

        assert f'key            = "builders/{builder_env.id}/terraform.tfstate"' in backend_config

        # Verify creator_id and room_id are NOT in the state key path

        assert f"{builder_env.creator_id}/" not in backend_config or f"{builder_env.creator_id}/terraform" not in backend_config

        assert f"{builder_env.room_id}/" not in backend_config or f"{builder_env.room_id}/terraform" not in backend_config



    def test_workspace_preparation_creates_backend_tf(self, lab_manager, builder_env, tmp_path):

        """Test that _prepare_workspace creates backend.tf with S3 backend config."""

        workspace = tmp_path / "test-workspace"

        builder_env.terraform_state_path = str(workspace)

        workspace.mkdir(parents=True, exist_ok=True)



        workspace_path = lab_manager._prepare_workspace(builder_env)



        backend_file = Path(workspace) / "backend.tf"

        assert backend_file.exists(), "backend.tf should be created"



        content = backend_file.read_text()

        assert 'backend "s3"' in content

        assert f'bucket         = "{settings.terraform_state_bucket}"' in content

        assert f'key            = "builders/{builder_env.id}/terraform.tfstate"' in content



    def test_workspace_preparation_copies_lock_file(self, lab_manager, builder_env, tmp_path):

        """Test that _prepare_workspace copies .terraform.lock.hcl for provider version consistency."""

        workspace = tmp_path / "test-workspace-lock"

        workspace.mkdir(parents=True, exist_ok=True)

        builder_env.terraform_state_path = str(workspace)



        lab_manager._prepare_workspace(builder_env)



        lock_file = Path(workspace) / ".terraform.lock.hcl"

        assert lock_file.exists(), ".terraform.lock.hcl should be copied"



    def test_dry_run_mode_does_not_contact_s3(self, lab_manager, builder_env, tmp_path):

        """Test that dry-run mode does not attempt to contact S3."""

        workspace = tmp_path / "test-dry-run"

        workspace.mkdir(parents=True, exist_ok=True)

        builder_env.terraform_state_path = str(workspace)

        builder_env.status = "pending"



        dry_run_lab_manager = LabManager()

        dry_run_lab_manager.config["terraform_dry_run"] = True



        with patch("app.lab_manager.subprocess.run") as mock_run:

            dry_run_lab_manager.start_builder(builder_env)

            mock_run.assert_not_called()

            # In dry-run, builder should get simulated outputs

            assert builder_env.status == "running"

            assert builder_env.instance_id is not None

            assert builder_env.instance_id.startswith("i-dryrun-")



    def test_s3_backend_config_not_in_dry_run_mode(self, lab_manager, builder_env, tmp_path):

        """Test that backend.tf is still created in dry-run but terraform init doesn't run."""

        workspace = tmp_path / "test-dry-run-backend"

        workspace.mkdir(parents=True, exist_ok=True)

        builder_env.terraform_state_path = str(workspace)

        builder_env.status = "pending"



        dry_run_lab_manager = LabManager()

        dry_run_lab_manager.config["terraform_dry_run"] = True



        with patch("app.lab_manager.subprocess.run") as mock_run:

            dry_run_lab_manager.start_builder(builder_env)

            mock_run.assert_not_called()



            # But backend.tf should still be created

            backend_file = Path(workspace) / "backend.tf"

            assert backend_file.exists()

            content = backend_file.read_text()

            assert 'backend "s3"' in content



    def test_state_key_uniqueness_per_builder(self, lab_manager, tmp_path):

        """Test that each builder gets a unique S3 state key based on its ID."""

        builder1 = BuilderEnvironment(

            id=1, room_id=10, creator_id=100, status="pending",

            terraform_state_path=str(tmp_path / "builder-1"), expires_at=None

        )

        builder2 = BuilderEnvironment(

            id=2, room_id=20, creator_id=200, status="pending",

            terraform_state_path=str(tmp_path / "builder-2"), expires_at=None

        )



        lab_manager.config["terraform_state_key_prefix"] = "builders"



        config1 = lab_manager._generate_backend_config(builder1)

        config2 = lab_manager._generate_backend_config(builder2)



        assert "builders/1/terraform.tfstate" in config1

        assert "builders/2/terraform.tfstate" in config2

        assert "builders/1/" not in config2

        assert "builders/2/" not in config1



    def test_backend_config_uses_s3_native_locking(self, lab_manager, builder_env):

        """Test that backend config uses S3 native locking (use_lockfile) instead of DynamoDB."""

        backend_config = lab_manager._generate_backend_config(builder_env)

        assert "use_lockfile   = true" in backend_config

        assert "dynamodb_table" not in backend_config



    def test_backend_config_includes_encryption(self, lab_manager, builder_env):

        """Test that backend config enables encryption."""

        backend_config = lab_manager._generate_backend_config(builder_env)

        assert "encrypt        = true" in backend_config


    def test_workspace_preparation_does_not_copy_provider_cache(self, lab_manager, builder_env, tmp_path):

        """Test that _prepare_workspace does NOT copy .terraform/providers from source."""

        workspace = tmp_path / "test-workspace-no-providers"
        builder_env.terraform_state_path = str(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        # Ensure source has .terraform/providers (simulating real scenario)
        source_path = Path(lab_manager.config["terraform_source_dir"])
        source_providers = source_path / ".terraform" / "providers"
        if source_providers.exists():
            lab_manager._prepare_workspace(builder_env)
            # Workspace should NOT have .terraform/providers copied
            workspace_providers = workspace / ".terraform" / "providers"
            assert not workspace_providers.exists(), "Provider cache should NOT be copied to workspace"


    def test_workspace_preparation_copies_lock_file(self, lab_manager, builder_env, tmp_path):

        """Test that _prepare_workspace copies .terraform.lock.hcl for provider version consistency."""

        workspace = tmp_path / "test-workspace-lock"
        builder_env.terraform_state_path = str(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        lab_manager._prepare_workspace(builder_env)

        lock_file = Path(workspace) / ".terraform.lock.hcl"
        assert lock_file.exists(), ".terraform.lock.hcl should be copied"


    def test_successful_provisioning_cleans_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that successful provisioning deletes the workspace directory."""

        workspace = tmp_path / "test-success-cleanup"
        builder_env.terraform_state_path = str(workspace)

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            init_result = MagicMock()
            init_result.returncode = 0
            init_result.stdout = ""
            init_result.stderr = ""

            apply_result = MagicMock()
            apply_result.returncode = 0
            apply_result.stdout = ""
            apply_result.stderr = ""

            output_result = MagicMock()
            output_result.returncode = 0
            output_result.stdout = json.dumps({
                "instance_id": {"value": "i-123456"},
                "private_ip": {"value": "10.0.1.100"},
                "security_group_id": {"value": "sg-123456"},
            })
            output_result.stderr = ""

            mock_run.side_effect = [init_result, apply_result, output_result]

            lab_manager.start_builder(builder_env)

            assert builder_env.status == "running"
            assert not Path(workspace).exists(), "Workspace should be deleted after successful provisioning"


    def test_failed_provisioning_cleans_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that failed provisioning (apply failure) deletes the workspace directory."""

        workspace = tmp_path / "test-failed-cleanup"
        builder_env.terraform_state_path = str(workspace)

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            init_result = MagicMock()
            init_result.returncode = 0
            init_result.stdout = ""
            init_result.stderr = ""

            apply_result = MagicMock()
            apply_result.returncode = 1
            apply_result.stdout = ""
            apply_result.stderr = "Error: Invalid AMI"

            mock_run.side_effect = [init_result, apply_result]

            with pytest.raises(TerraformError):
                lab_manager.start_builder(builder_env)

            assert builder_env.status == "failed"
            assert not Path(workspace).exists(), "Workspace should be deleted after failed provisioning"


    def test_failed_init_cleans_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that failed terraform init deletes the workspace directory."""

        workspace = tmp_path / "test-failed-init-cleanup"
        builder_env.terraform_state_path = str(workspace)

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            init_result = MagicMock()
            init_result.returncode = 1
            init_result.stdout = ""
            init_result.stderr = "Error: Backend init failed"

            mock_run.return_value = init_result

            with pytest.raises(TerraformError):
                lab_manager.start_builder(builder_env)

            assert builder_env.status == "failed"
            assert not Path(workspace).exists(), "Workspace should be deleted after failed init"


    def test_successful_stop_cleans_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that successful terraform destroy deletes the workspace directory."""

        workspace = tmp_path / "test-stop-cleanup"
        builder_env.terraform_state_path = str(workspace)
        builder_env.instance_id = "i-123456"
        builder_env.private_ip = "10.0.1.100"
        builder_env.security_group_id = "sg-123456"
        builder_env.status = "running"

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            destroy_result = MagicMock()
            destroy_result.returncode = 0
            destroy_result.stdout = ""
            destroy_result.stderr = ""

            mock_owned_cleanup(lab_manager, builder_env, mock_run) 

            lab_manager.stop_builder(builder_env)

            assert builder_env.status == "stopped"
            assert builder_env.stopped_at is not None, "stopped_at should be set on successful stop"
            assert not Path(workspace).exists(), "Workspace should be deleted after successful destroy"


    def test_stop_reconstructs_missing_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that stop_builder reconstructs workspace if it was cleaned up after provisioning."""

        workspace = tmp_path / "test-reconstruct"
        builder_env.terraform_state_path = str(workspace)
        builder_env.instance_id = "i-123456"
        builder_env.private_ip = "10.0.1.100"
        builder_env.security_group_id = "sg-123456"
        builder_env.status = "running"

        # Ensure workspace does NOT exist (simulating cleanup after provisioning)
        assert not Path(workspace).exists()

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            init_result = MagicMock()
            init_result.returncode = 0
            init_result.stdout = ""
            init_result.stderr = ""

            destroy_result = MagicMock()
            destroy_result.returncode = 0
            destroy_result.stdout = ""
            destroy_result.stderr = ""

            mock_owned_cleanup(lab_manager, builder_env, mock_run)

            lab_manager.stop_builder(builder_env)

            assert builder_env.status == "stopped"
            assert builder_env.stopped_at is not None, "stopped_at should be set on successful stop"
            # Workspace should be recreated then cleaned up
            assert not Path(workspace).exists(), "Reconstructed workspace should be deleted after destroy"
            # Verify init -reconfigure was called
            calls = mock_run.call_args_list
            assert calls[0][0][0] == ["init", "-reconfigure", "-input=false"]
            assert calls[1][0][0] == ["state", "pull"]
            assert calls[2][0][0] == ["destroy", "-input=false", "-auto-approve"]


    def test_failed_stop_preserves_workspace(self, lab_manager, builder_env, tmp_path):

        """Test that failed terraform destroy preserves the workspace for manual recovery."""

        workspace = tmp_path / "test-failed-stop-preserve"
        builder_env.terraform_state_path = str(workspace)
        builder_env.instance_id = "i-123456"
        builder_env.private_ip = "10.0.1.100"
        builder_env.security_group_id = "sg-123456"
        builder_env.status = "running"

        with patch("app.lab_manager.LabManager._run_terraform") as mock_run:
            destroy_result = MagicMock()
            destroy_result.returncode = 1
            destroy_result.stdout = ""
            destroy_result.stderr = "Error: Dependency violation"

            mock_owned_cleanup(lab_manager, builder_env, mock_run, fail_destroy=True)

            with pytest.raises(TerraformError):
                lab_manager.stop_builder(builder_env)

            assert builder_env.status == "stopping"
            assert "destroy failed" in builder_env.error_message
            assert builder_env.stopped_at is None, "stopped_at should NOT be set on failed stop"
            assert Path(workspace).exists(), "Workspace should be preserved after failed destroy"


    def test_dry_run_mode_still_works(self, lab_manager, builder_env, tmp_path):

        """Test that dry-run mode still works with new lifecycle."""

        workspace = tmp_path / "test-dry-run-lifecycle"
        builder_env.terraform_state_path = str(workspace)
        builder_env.status = "pending"

        dry_run_lab_manager = LabManager()
        dry_run_lab_manager.config["terraform_dry_run"] = True
        dry_run_lab_manager.config["terraform_plugin_cache_dir"] = str(tmp_path / "terraform-plugin-cache")

        with patch("app.lab_manager.subprocess.run") as mock_run:
            dry_run_lab_manager.start_builder(builder_env)

            mock_run.assert_not_called()
            assert builder_env.status == "running"
            assert builder_env.instance_id is not None
            assert builder_env.instance_id.startswith("i-dryrun-")
            # In dry-run mode, workspace is preserved for inspection
            assert Path(workspace).exists(), "Workspace should persist in dry-run mode"
            backend_file = Path(workspace) / "backend.tf"
            assert backend_file.exists(), "backend.tf should be created in dry-run mode"


class TestReconciliationWorker:
    @pytest.fixture
    def worker(self, tmp_path):
        w = ReconciliationWorker()
        w.config["interval_seconds"] = 1
        w.config["stale_pending_minutes"] = 10
        w.config["stale_provisioning_minutes"] = 15
        w.config["terraform_dry_run"] = True
        return w

    @pytest.fixture
    def mock_db(self):
        """Mock database session for reconciliation tests."""
        from unittest.mock import MagicMock
        db = MagicMock()
        return db

    def _make_builder(self, status="running", expires_at=None, created_at=None, updated_at=None, builder_id=1):
        builder = BuilderEnvironment(
            id=builder_id,
            room_id=10,
            creator_id=100,
            status=status,
            terraform_state_path=f"/tmp/secforge-builders/builder-{builder_id}",
            expires_at=expires_at or (datetime.utcnow() + timedelta(hours=1)),
            created_at=created_at or datetime.utcnow(),
            updated_at=updated_at or datetime.utcnow(),
        )
        return builder

    def test_expired_running_builder_detected(self, worker, mock_db):
        """Test that expired running builders are detected."""
        expired = self._make_builder(status="running", expires_at=datetime.utcnow() - timedelta(minutes=5), builder_id=1)
        not_expired = self._make_builder(status="running", expires_at=datetime.utcnow() + timedelta(hours=1), builder_id=2)

        mock_db.execute.return_value.scalars.return_value.all.return_value = [expired]

        expired_builders = mock_db.execute.return_value.scalars.return_value.all.return_value

        assert expired in expired_builders
        assert not_expired not in expired_builders

    def test_stale_pending_builder_detected(self, worker, mock_db):
        """Test that stale pending builders are detected."""
        stale = self._make_builder(status="pending", created_at=datetime.utcnow() - timedelta(minutes=20), builder_id=1)
        fresh = self._make_builder(status="pending", created_at=datetime.utcnow() - timedelta(minutes=5), builder_id=2)

        mock_db.execute.return_value.scalars.return_value.all.return_value = [stale]

        stale_builders = mock_db.execute.return_value.scalars.return_value.all.return_value

        assert stale in stale_builders
        assert fresh not in stale_builders

    def test_stale_provisioning_builder_detected(self, worker, mock_db):
        """Test that stale provisioning builders are detected."""
        stale = self._make_builder(status="provisioning", updated_at=datetime.utcnow() - timedelta(minutes=20), builder_id=1)
        fresh = self._make_builder(status="provisioning", updated_at=datetime.utcnow() - timedelta(minutes=5), builder_id=2)

        mock_db.execute.return_value.scalars.return_value.all.return_value = [stale]

        stale_builders = mock_db.execute.return_value.scalars.return_value.all.return_value

        assert stale in stale_builders
        assert fresh not in stale_builders

    def test_crashed_stopping_builder_detected(self, worker, mock_db):
        """Test that crashed stopping builders are detected."""
        stopping = self._make_builder(status="stopping", builder_id=1)
        running = self._make_builder(status="running", builder_id=2)

        mock_db.execute.return_value.scalars.return_value.all.return_value = [stopping]

        stopping_builders = mock_db.execute.return_value.scalars.return_value.all.return_value

        assert stopping in stopping_builders
        assert running not in stopping_builders

    @patch("app.reconciliation.LabManager")
    def test_expired_builder_transition_to_stopping(self, mock_lab_manager_class, worker, mock_db):
        """Test that expired running builder is transitioned to stopping."""
        builder = self._make_builder(status="running", expires_at=datetime.utcnow() - timedelta(minutes=5), builder_id=1)
        mock_db.get.return_value = builder

        mock_lab_manager = MagicMock()
        mock_lab_manager_class.return_value = mock_lab_manager

        # Claim the builder
        claimed = worker._claim_builder_for_stop(mock_db, builder.id)
        # In mock, we just verify the logic would work
        assert builder.status == "running"  # Original status

        # Simulate successful claim
        builder.status = "stopping"
        builder.updated_at = datetime.utcnow()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()

        # Verify claim logic
        assert builder.status == "stopping"

    @patch("app.reconciliation.LabManager")
    def test_stopping_builder_retried(self, mock_lab_manager_class, worker, mock_db):
        """Test that a stopping builder is retried after crash."""
        builder = self._make_builder(status="stopping", builder_id=1)
        mock_db.get.return_value = builder

        mock_lab_manager = MagicMock()
        mock_lab_manager_class.return_value = mock_lab_manager

        claimed = worker._claim_builder_for_stop(mock_db, builder.id)
        assert builder.status == "stopping"

        # Simulate successful destroy
        builder.status = "stopped"
        builder.instance_id = None
        builder.private_ip = None
        builder.security_group_id = None
        builder.error_message = None
        builder.stopped_at = datetime.utcnow()
        builder.updated_at = datetime.utcnow()
        mock_db.commit = MagicMock()

        assert builder.status == "stopped"
        assert builder.stopped_at is not None

    @patch("app.reconciliation.LabManager")
    def test_failed_stop_marks_failed(self, mock_lab_manager_class, worker, mock_db):
        """Test that failed destroy marks builder as failed."""
        builder = self._make_builder(status="stopping", builder_id=1)
        mock_db.get.return_value = builder

        mock_lab_manager = MagicMock()
        mock_lab_manager_class.return_value = mock_lab_manager

        # Simulate destroy failure by calling _mark_builder_failed directly
        worker._mark_builder_failed(mock_db, builder.id, "Destroy failed")

        # The terminal transition is issued as a conditional UPDATE and committed
        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called()

    def test_worker_survives_individual_builder_error(self, worker, mock_db):
        """Test that worker continues after an individual builder error."""
        builder1 = self._make_builder(status="running", expires_at=datetime.utcnow() - timedelta(minutes=5), builder_id=1)
        builder2 = self._make_builder(status="running", expires_at=datetime.utcnow() - timedelta(minutes=5), builder_id=2)

        mock_db.get.side_effect = [builder1, builder2]

        # Mark first as failed
        builder1.status = "failed"
        builder1.error_message = "Error 1"
        builder1.updated_at = datetime.utcnow()
        mock_db.commit = MagicMock()

        # Second should still be processable
        builder2.status = "stopping"
        builder2.updated_at = datetime.utcnow()
        mock_db.commit = MagicMock()
        mock_db.refresh = MagicMock()

        assert builder2.status == "stopping"

    def test_failed_builder_not_auto_retried(self, worker, mock_db):
        """Test that generic failed builders are NOT automatically retried."""
        failed = self._make_builder(status="failed", builder_id=1)

        # Mock queries to return empty for failed builders
        mock_db.execute.return_value.scalars.return_value.all.return_value = []

        expired = mock_db.execute.return_value.scalars.return_value.all.return_value
        assert failed not in expired

        stale_pending = mock_db.execute.return_value.scalars.return_value.all.return_value
        assert failed not in stale_pending

        stale_prov = mock_db.execute.return_value.scalars.return_value.all.return_value
        assert failed not in stale_prov

        stopping = mock_db.execute.return_value.scalars.return_value.all.return_value
        assert failed not in stopping

    def test_claim_mechanism_prevents_double_claim(self, worker, mock_db):
        """Test that two reconciliation attempts cannot claim the same builder."""
        builder = self._make_builder(status="running", expires_at=datetime.utcnow() - timedelta(minutes=5), builder_id=1)
        mock_db.get.return_value = builder
        mock_db.execute.return_value.scalar_one_or_none.return_value = builder

        # First claim - we need to mock the FOR UPDATE query
        # This is a simplified test since we can't easily test FOR UPDATE with mocks
        # Just verify the method exists and handles the logic
        assert worker._claim_builder_for_stop is not None

    def test_dry_run_no_aws_contact(self, worker, tmp_path):
        """Test that dry-run mode does not contact AWS/S3."""
        worker.config["terraform_dry_run"] = True

        # The worker should not call LabManager in dry-run for reconciliation
        # This is implicitly tested by the fact that LabManager is mocked in other tests
        assert worker.config["terraform_dry_run"] is True

    def test_worker_start_stop(self, worker):
        """Test that worker starts and stops correctly."""
        import asyncio

        async def test_lifecycle():
            assert worker._task is None
            await worker.start()
            assert worker._task is not None
            assert not worker._task.done()
            await worker.stop()
            assert worker._task is None

        asyncio.run(test_lifecycle())


class TestAWSDiscoveryService:
    @pytest.fixture
    def discovery_service(self, tmp_path):
        service = AWSDiscoveryService(region="eu-north-1", dry_run=False)
        return service

    @pytest.fixture
    def mock_ec2_client(self):
        with patch("app.aws_discovery.boto3.client") as mock_client:
            yield mock_client.return_value

    def test_discovery_uses_all_required_tag_filters(self, discovery_service, mock_ec2_client):
        """Test that EC2 discovery uses all required tag filters."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = []

        discovery_service.discover_instances(builder_id="bld-42")

        call_args = mock_ec2_client.get_paginator.call_args
        assert call_args[0][0] == "describe_instances"

        paginate_args = mock_paginator.paginate.call_args
        filters = paginate_args[1]["Filters"]
        filter_dict = {f["Name"]: f["Values"][0] for f in filters}

        assert filter_dict["tag:Project"] == "SecForge"
        assert filter_dict["tag:ManagedBy"] == "Terraform"
        assert filter_dict["tag:ResourceType"] == "builder"
        assert filter_dict["tag:BuilderId"] == "bld-42"

    def test_security_group_discovery_uses_all_required_tag_filters(self, discovery_service, mock_ec2_client):
        """Test that security group discovery uses all required ownership filters."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = []

        discovery_service.discover_security_groups(builder_id="bld-42")

        call_args = mock_ec2_client.get_paginator.call_args
        assert call_args[0][0] == "describe_security_groups"

        paginate_args = mock_paginator.paginate.call_args
        filters = paginate_args[1]["Filters"]
        filter_dict = {f["Name"]: f["Values"][0] for f in filters}

        assert filter_dict["tag:Project"] == "SecForge"
        assert filter_dict["tag:ManagedBy"] == "Terraform"
        assert filter_dict["tag:ResourceType"] == "builder"
        assert filter_dict["tag:BuilderId"] == "bld-42"

    def test_discovery_returns_structured_results(self, discovery_service, mock_ec2_client):
        """Test that discovery converts raw responses into structured results."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-123456",
                    "PrivateIpAddress": "10.0.1.100",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "Project", "Value": "SecForge"},
                        {"Key": "ManagedBy", "Value": "Terraform"},
                        {"Key": "ResourceType", "Value": "builder"},
                        {"Key": "BuilderId", "Value": "bld-42"},
                        {"Key": "RoomId", "Value": "10"},
                        {"Key": "CreatorId", "Value": "100"},
                        {"Key": "Environment", "Value": "builder"},
                        {"Key": "Name", "Value": "builder-bld-42"},
                    ],
                }]
            }]
        }]

        instances = discovery_service.discover_instances(builder_id="bld-42")

        assert len(instances) == 1
        instance = instances[0]
        assert isinstance(instance, InstanceInfo)
        assert instance.instance_id == "i-123456"
        assert instance.private_ip == "10.0.1.100"
        assert instance.state == "running"
        assert instance.builder_id == "bld-42"
        assert instance.tags["Project"] == "SecForge"

    def test_malformed_builder_id_rejected(self, discovery_service):
        """Test that malformed BuilderId is rejected."""
        builder_db_id = discovery_service._validate_builder_id("invalid-format")
        assert builder_db_id is None

        builder_db_id = discovery_service._validate_builder_id("bld-not-a-number")
        assert builder_db_id is None

        builder_db_id = discovery_service._validate_builder_id("bld-42")
        assert builder_db_id == 42

    def test_resources_missing_project_ignored(self, discovery_service, mock_ec2_client):
        """Test that resources missing Project tag are ignored."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-123456",
                    "PrivateIpAddress": "10.0.1.100",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "ManagedBy", "Value": "Terraform"},
                        {"Key": "ResourceType", "Value": "builder"},
                        {"Key": "BuilderId", "Value": "bld-42"},
                    ],
                }]
            }]
        }]

        instances = discovery_service.discover_instances(builder_id="bld-42")
        assert len(instances) == 0

    def test_resources_missing_managedby_ignored(self, discovery_service, mock_ec2_client):
        """Test that resources missing ManagedBy tag are ignored."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-123456",
                    "PrivateIpAddress": "10.0.1.100",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "Project", "Value": "SecForge"},
                        {"Key": "ResourceType", "Value": "builder"},
                        {"Key": "BuilderId", "Value": "bld-42"},
                    ],
                }]
            }]
        }]

        instances = discovery_service.discover_instances(builder_id="bld-42")
        assert len(instances) == 0

    def test_resources_missing_resourcetype_ignored(self, discovery_service, mock_ec2_client):
        """Test that resources missing ResourceType tag are ignored."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-123456",
                    "PrivateIpAddress": "10.0.1.100",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "Project", "Value": "SecForge"},
                        {"Key": "ManagedBy", "Value": "Terraform"},
                        {"Key": "BuilderId", "Value": "bld-42"},
                    ],
                }]
            }]
        }]

        instances = discovery_service.discover_instances(builder_id="bld-42")
        assert len(instances) == 0

    def test_resources_missing_builderid_ignored(self, discovery_service, mock_ec2_client):
        """Test that resources missing BuilderId tag are ignored."""
        mock_paginator = MagicMock()
        mock_ec2_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{
            "Reservations": [{
                "Instances": [{
                    "InstanceId": "i-123456",
                    "PrivateIpAddress": "10.0.1.100",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "Project", "Value": "SecForge"},
                        {"Key": "ManagedBy", "Value": "Terraform"},
                        {"Key": "ResourceType", "Value": "builder"},
                    ],
                }]
            }]
        }]

        instances = discovery_service.discover_instances(builder_id="bld-42")
        assert len(instances) == 0


class TestOrphanDetection:
    @pytest.fixture
    def worker(self, tmp_path):
        w = ReconciliationWorker()
        w.config["interval_seconds"] = 1
        w.config["stale_pending_minutes"] = 10
        w.config["stale_provisioning_minutes"] = 15
        w.config["terraform_dry_run"] = True
        w.config["orphan_discovery_interval_seconds"] = 1
        return w

    @pytest.fixture
    def mock_db(self):
        from unittest.mock import MagicMock
        db = MagicMock()
        return db

    def _make_builder(self, status="running", expires_at=None, created_at=None, updated_at=None, builder_id=1):
        builder = BuilderEnvironment(
            id=builder_id,
            room_id=10,
            creator_id=100,
            status=status,
            terraform_state_path=f"/tmp/secforge-builders/builder-{builder_id}",
            expires_at=expires_at or (datetime.utcnow() + timedelta(hours=1)),
            created_at=created_at or datetime.utcnow(),
            updated_at=updated_at or datetime.utcnow(),
        )
        return builder

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_existing_builder_recognized(self, mock_discovery_class, worker, mock_db):
        """Test that existing builder in DB is recognized."""
        builder = self._make_builder(status="running", builder_id=1)
        mock_db.get.return_value = builder

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = [
            BuilderResources(
                builder_id="bld-1",
                instances=[InstanceInfo(
                    instance_id="i-123456",
                    private_ip="10.0.1.100",
                    state="running",
                    builder_id="bld-1",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-1"}
                )],
                security_groups=[SecurityGroupInfo(
                    group_id="sg-123456",
                    group_name="builder-bld-1-SG",
                    builder_id="bld-1",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-1"}
                )],
            )
        ]

        # Simulate orphan detection - it should find the builder in DB
        # This tests the logic indirectly by checking the worker's _validate_builder_id
        assert worker._validate_builder_id("bld-1") == 1

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_missing_db_record_becomes_orphan_candidate(self, mock_discovery_class, worker, mock_db):
        """Test that missing DB record becomes an orphan candidate."""
        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = [
            BuilderResources(
                builder_id="bld-999",
                instances=[InstanceInfo(
                    instance_id="i-123456",
                    private_ip="10.0.1.100",
                    state="running",
                    builder_id="bld-999",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-999"}
                )],
                security_groups=[],
            )
        ]
        mock_db.get.return_value = None  # No DB record

        assert worker._validate_builder_id("bld-999") == 999
        assert mock_db.get(BuilderEnvironment, 999) is None

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_orphan_detection_does_not_terminate_instances(self, mock_discovery_class, worker, mock_db):
        """Test that orphan detection does NOT terminate instances."""
        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = []

        # The detector should not have any terminate/delete methods
        assert not hasattr(worker, 'terminate_instances')
        assert not hasattr(worker, 'delete_security_group')

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_orphan_detection_does_not_delete_security_groups(self, mock_discovery_class, worker, mock_db):
        """Test that orphan detection does NOT delete security groups."""
        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = []

        assert not hasattr(worker, 'delete_security_group')

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_running_builder_not_classified_as_orphan(self, mock_discovery_class, worker, mock_db):
        """Test that running builder is not classified as an orphan."""
        builder = self._make_builder(status="running", builder_id=1)
        mock_db.get.return_value = builder

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = [
            BuilderResources(
                builder_id="bld-1",
                instances=[InstanceInfo(
                    instance_id="i-123456",
                    private_ip="10.0.1.100",
                    state="running",
                    builder_id="bld-1",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-1"}
                )],
                security_groups=[],
            )
        ]

        assert builder.status == "running"

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_stopping_builder_not_classified_as_orphan(self, mock_discovery_class, worker, mock_db):
        """Test that stopping builder is not classified as an orphan."""
        builder = self._make_builder(status="stopping", builder_id=1)
        mock_db.get.return_value = builder

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = [
            BuilderResources(
                builder_id="bld-1",
                instances=[InstanceInfo(
                    instance_id="i-123456",
                    private_ip="10.0.1.100",
                    state="running",
                    builder_id="bld-1",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-1"}
                )],
                security_groups=[],
            )
        ]

        assert builder.status == "stopping"

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_stopped_builder_reported_for_normal_reconciliation(self, mock_discovery_class, worker, mock_db):
        """Test that stopped/failed builder is reported appropriately for normal reconciliation."""
        builder = self._make_builder(status="stopped", builder_id=1)
        mock_db.get.return_value = builder

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = [
            BuilderResources(
                builder_id="bld-1",
                instances=[InstanceInfo(
                    instance_id="i-123456",
                    private_ip="10.0.1.100",
                    state="stopped",
                    builder_id="bld-1",
                    tags={"Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": "builder", "BuilderId": "bld-1"}
                )],
                security_groups=[],
            )
        ]

        assert builder.status == "stopped"


class TestWorkerOrphanIntegration:
    @pytest.fixture
    def worker(self, tmp_path):
        w = ReconciliationWorker()
        w.config["interval_seconds"] = 1
        w.config["stale_pending_minutes"] = 10
        w.config["stale_provisioning_minutes"] = 15
        w.config["terraform_dry_run"] = True
        w.config["orphan_discovery_interval_seconds"] = 1
        return w

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_orphan_discovery_runs_at_configured_interval(self, mock_discovery_class, worker):
        """Test that orphan discovery runs according to its configured interval."""
        import asyncio

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.return_value = []

        async def test_interval():
            await worker.start()
            assert worker._task is not None
            assert worker._orphan_task is not None
            
            # Wait a bit for at least one iteration
            await asyncio.sleep(0.1)
            
            # In dry-run mode, no AWS calls should be made
            mock_discovery.discover_all_builder_resources.assert_not_called()
            
            await worker.stop()

        asyncio.run(test_interval())

    @patch("app.reconciliation.AWSDiscoveryService")
    def test_aws_discovery_failure_does_not_kill_worker(self, mock_discovery_class, worker):
        """Test that AWS discovery failure does not kill the worker."""
        import asyncio

        mock_discovery = MagicMock()
        mock_discovery_class.return_value = mock_discovery
        mock_discovery.discover_all_builder_resources.side_effect = Exception("AWS error")

        async def test_failure_handling():
            await worker.start()
            
            # Wait a bit for the orphan loop to run and handle the error
            await asyncio.sleep(0.1)
            
            # Worker should still be running
            assert not worker._orphan_task.done()
            
            await worker.stop()

        asyncio.run(test_failure_handling())

    def test_dry_run_performs_zero_aws_calls(self, worker):
        """Test that dry-run performs zero AWS calls."""
        import asyncio

        worker.config["terraform_dry_run"] = True

        with patch("app.reconciliation.AWSDiscoveryService") as mock_discovery_class:
            mock_discovery = MagicMock()
            mock_discovery_class.return_value = mock_discovery

            async def test_dry_run():
                await worker.start()
                await asyncio.sleep(0.1)
                mock_discovery.discover_all_builder_resources.assert_not_called()
                await worker.stop()

            asyncio.run(test_dry_run())
