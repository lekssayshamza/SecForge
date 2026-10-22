"""Static deployment and private-token handling checks; no AWS mutations."""
import importlib.util
from pathlib import Path
import os
import pytest
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("token_helper", ROOT / "backend/deploy/cloud/save_duckdns_token.py")
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)


def test_token_directory_rejects_public_permissions(tmp_path):
    directory = tmp_path / "token"; directory.mkdir(mode=0o755)
    with pytest.raises(ValueError): helper.private_directory(directory)


def test_token_directory_rejects_symlink(tmp_path):
    actual = tmp_path / "actual"; actual.mkdir(mode=0o700)
    link = tmp_path / "link"; link.symlink_to(actual)
    with pytest.raises(ValueError): helper.private_directory(link)


def test_token_directory_is_private_and_owned(tmp_path):
    directory = tmp_path / "token"; helper.private_directory(directory)
    assert directory.stat().st_uid == os.getuid()
    assert directory.stat().st_mode & 0o077 == 0


def test_cloud_service_cannot_spawn_multiple_workers():
    service = (ROOT / "backend/deploy/cloud/secforge.service").read_text()
    assert "--workers 1" in service and "--host 127.0.0.1" in service
    assert "--reload" not in service
    assert "User=secforge" in service and "ProtectSystem=strict" in service


def test_same_origin_proxy_preserves_backend_api_prefix():
    caddy = (ROOT / "backend/deploy/cloud/Caddyfile").read_text()
    assert "handle /api/*" in caddy and "handle_path" not in caddy
    assert "reverse_proxy 127.0.0.1:8000" in caddy


def test_application_infrastructure_has_no_paid_managed_stack():
    source = (ROOT / "infrastructure/terraform/application/main.tf").read_text()
    assert 'instance_type = "t3.small"' in source or 'instance_type               = "t3.small"' in source
    assert 'cpu_credits = "standard"' in source
    assert 'http_tokens   = "required"' in source or 'http_tokens = "required"' in source
    for resource in ["aws_db_instance", "aws_lb", "aws_nat_gateway", "aws_eks_cluster"]:
        assert 'resource "' + resource + '"' not in source
    assert "prevent_destroy = true" in source
