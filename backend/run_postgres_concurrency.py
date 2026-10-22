"""Explicit test-only entry point. No PostgreSQL operation happens on import."""

import argparse
import base64
import os
from pathlib import Path
import secrets
import sys
from unittest.mock import patch

from test_support import postgres_harness as harness


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run only the disposable PostgreSQL lab concurrency tests")
    parser.add_argument("--allow-disposable-postgres", action="store_true", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--include-challenges", action="store_true", help="Also run isolated learning concurrency and migration tests")
    parser.add_argument("--include-profiles", action="store_true", help="Also run isolated profile uniqueness and migration tests")
    parser.add_argument("--include-lab-time", action="store_true", help="Also run isolated learner timing, extension and expiration tests")
    parser.add_argument("--include-auth", action="store_true", help="Also run isolated authentication races and migration tests")
    parser.add_argument("--include-workspace", action="store_true", help="Also verify draft publication and image selection interleavings")
    parser.add_argument("--include-social", action="store_true", help="Verify social concurrency and additive migration")
    parser.add_argument("--include-room-media", action="store_true", help="Verify private lesson media migrations and publication locks")
    parser.add_argument("--include-network-ports", action="store_true", help="Verify additive room port migration on disposable PostgreSQL")
    parser.add_argument("--include-editable-answers", action="store_true", help="Verify encrypted answer migration and lifecycle")
    parser.add_argument("--include-learner-answers", action="store_true", help="Verify encrypted accepted submissions and credit")
    args = parser.parse_args(argv)
    harness.validate_port(args.port)
    cluster = harness.DisposablePostgres(args.port)
    result = 1
    previous_cwd = Path.cwd()
    backend = Path(__file__).resolve().parent
    try:
        cluster.allocate()
        cluster.start()
        # A clear environment and empty working directory prevent .env and inherited
        # application/libpq/cloud configuration from influencing test destinations.
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "AWS_EC2_METADATA_DISABLED": "true",
            "DATABASE_URL": cluster.sqlalchemy_url().render_as_string(hide_password=False),
            "JWT_SECRET": secrets.token_urlsafe(40), "TERRAFORM_DRY_RUN": "true",
            "EDITABLE_ANSWER_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "VPN_ENABLED": "false", "VPN_AUTHZ_ENABLED": "false",
            "VPN_BACKEND_AUTHZ_ENABLED": "false", "BUILDER_SSH_KEY_PATH": "",
            "VPN_GATEWAY_SSH_KEY_PATH": "", "VPN_GATEWAY_HOST": "offline.invalid",
            "BUILDER_AMI_ID": "ami-offline", "BUILDER_VPC_ID": "vpc-offline",
            "BUILDER_SUBNET_ID": "subnet-offline", "BUILDER_SSH_KEY_NAME": "offline",
            "TERRAFORM_STATE_BUCKET": "offline-test-only",
            "LAB_TERRAFORM_WORKSPACE_ROOT": str(cluster.root / "work" / "labs"),
            "BUILDER_TERRAFORM_WORKSPACE_ROOT": str(cluster.root / "work" / "builders"),
        }
        os.chdir(cluster.root / "work")
        with patch.dict(os.environ, environment, clear=True), harness.offline_guards(cluster):
            harness.ACTIVE_CLUSTER = cluster
            import pytest
            # Fixed target, normal pytest exit status, no skips/xfails/failure filtering.
            targets = [str(backend / "test_postgres_lab_concurrency.py")]
            if args.include_challenges: targets.append(str(backend / "test_postgres_challenges.py"))
            if args.include_profiles: targets.append(str(backend / "test_postgres_profiles.py"))
            if args.include_lab_time: targets.append(str(backend / "test_postgres_lab_time.py"))
            if args.include_auth: targets.append(str(backend / "test_postgres_auth.py"))
            if args.include_workspace: targets.append(str(backend / "test_postgres_workspace.py"))
            if args.include_social: targets.append(str(backend / "test_postgres_social.py"))
            if args.include_room_media: targets.append(str(backend / "test_postgres_room_media.py"))
            if args.include_network_ports: targets.append(str(backend / "test_postgres_network_ports.py"))
            if args.include_editable_answers: targets.append(str(backend / "test_postgres_editable_answers.py"))
            if args.include_learner_answers: targets.append(str(backend / "test_postgres_learner_answers.py"))
            result = pytest.main([
                *targets, "-v", "--tb=short",
                "-p", "no:cacheprovider", "--basetemp", str(cluster.root / "work" / "pytest"),
                "--confcutdir", str(backend),
            ])
    except Exception:
        # Do not print raw exceptions/DSNs/server logs.
        print("Disposable PostgreSQL verification failed; no external target is permitted.", file=sys.stderr)
        result = 1
    finally:
        harness.ACTIVE_CLUSTER = None
        os.chdir(previous_cwd)
        try:
            cluster.close()
        except Exception:
            print("Owned cluster cleanup could not be proven safe; temporary artifacts retained.", file=sys.stderr)
            result = 1
    return result


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
