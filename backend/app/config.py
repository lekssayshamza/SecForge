from pydantic_settings import BaseSettings
from pydantic import SecretStr, model_validator


class Settings(BaseSettings):
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "hide_input_in_errors": True,
    }

    database_url: str = (
        "postgresql+psycopg://secforge:secforge_dev_password"
        "@127.0.0.1:5432/secforge"
    )

    jwt_secret: str
    # Stable dedicated key recommended; empty derives a domain-separated key from JWT_SECRET.
    challenge_answer_key: str = ""
    # Independent AES-256 key (base64url); never derive encryption from JWTs.
    editable_answer_key: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    production: bool = False
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    trusted_hosts: list[str] = ["*"]
    registration_enabled: bool = True
    private_beta_user_ids: list[int] = []
    private_beta_enabled: bool = True
    app_public_url: str = "http://localhost:3000"
    google_oauth_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = ""
    reconciliation_enabled: bool = True
    avatar_storage_dir: str = '/var/lib/secforge/avatars'
    # Direct private-VPC SSH for the cloud backend, never public challenge ingress.
    builder_management_security_group_id: str = ""

    @model_validator(mode="after")
    def validate_production(self):
        if self.production:
            if len(self.jwt_secret) < 32 or not self.challenge_answer_key:
                raise ValueError("Production requires a strong JWT secret and an explicit saved-answer key")
            if "*" in self.trusted_hosts or not self.trusted_hosts:
                raise ValueError("Production requires explicit trusted hosts")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("Production CORS origins must use HTTPS")
            if self.private_beta_enabled and (self.registration_enabled or not self.private_beta_user_ids):
                raise ValueError("Private-beta production requires closed registration and an access list")
            if not self.app_public_url.startswith("https://") and self.google_oauth_enabled:
                raise ValueError("Production OAuth requires an HTTPS application URL")
        if self.google_oauth_enabled:
            if not self.google_client_id or not self.google_client_secret.get_secret_value():
                raise ValueError("Google OAuth requires configured credentials")
            if self.google_redirect_uri != self.app_public_url.rstrip("/") + "/api/auth/google/callback":
                raise ValueError("Google redirect URI must match the application callback")
        return self

    # Terraform/Builder configuration
    builder_terraform_source_dir: str = "/home/hamza/secforge/infrastructure/terraform/builder"
    builder_terraform_workspace_root: str = "/tmp/secforge-builders"
    terraform_plugin_cache_dir: str = "~/.cache/secforge/terraform/plugins"
    aws_region: str = "eu-north-1"
    builder_ami_id: str = "ami-0769f265f707fecc8"
    builder_vpc_id: str = "vpc-0e50eceba146f3a4a"
    builder_subnet_id: str = "subnet-017557ddd10df5804"
    builder_ssh_key_name: str = "CyberRange-Key"
    builder_vpn_cidr: str = "10.8.0.0/24"
    # Terraform binary - use 'terraform' from PATH; minimum version 1.10.0 required for use_lockfile=true
    terraform_binary: str = "terraform"
    terraform_timeout_seconds: int = 600
    # When true, LabManager simulates success without running terraform (for dev/testing)
    terraform_dry_run: bool = False

    # S3 backend for Terraform state
    terraform_state_bucket: str = "secforge-terraform-state"
    terraform_state_key_prefix: str = "builders"

    # Learner lab configuration
    lab_terraform_source_dir: str = "/home/hamza/secforge/infrastructure/terraform/lab"
    lab_terraform_workspace_root: str = "/tmp/secforge-labs"
    lab_terraform_state_key_prefix: str = "labs"

    # Shared VPN gateway management (no key material; enable only when gateway operations are required)
    vpn_enabled: bool = False
    # SSH management may use the already-connected VPN endpoint independently
    # of the public endpoint used in generated learner VPN profiles.
    vpn_gateway_management_host: str = ""
    vpn_gateway_host: str = "13.53.64.174"
    vpn_gateway_user: str = "ubuntu"
    vpn_gateway_ssh_key_path: str = "/home/hamza/aws/CyberRange-Key.pem"
    vpn_client_subnet: str = "10.8.0.0/24"
    vpn_learner_ip_range: str = "10.8.0.64/26"
    # Preserve service/client addresses when obsolete user identities are reset.
    vpn_reserved_client_ips: list[str] = []
    vpn_gateway_port: int = 1194
    lab_private_cidr: str = "10.0.1.0/24"
    vpn_authz_enabled: bool = True
    # Explicitly enabled only after the shared gateway/route preflight.
    # VPC DNS stays local; this grants creator builders public HTTP/HTTPS only.
    builder_internet_enabled: bool = False
    builder_internet_gateway_interface: str = ""
    # Backend management VPN authorization. When enabled, the dedicated
    # system `sf-backend` VPN identity (located by its common name in
    # vpn_identities) is authorized to every currently RUNNING builder
    # private IP. Defaults to OFF so that enabling it (and the resulting
    # live nftables sync on the gateway) is an explicit, documented step;
    # learner/creator authorization is unaffected.
    vpn_backend_authz_enabled: bool = False
    # Backend host route management for the dedicated backend management
    # tunnel. When vpn_backend_authz_enabled is on, the backend host keeps
    # one validated /32 host route per RUNNING builder through this tunnel
    # (via the VPN server address on the tunnel device) using a single
    # narrow root wrapper granted by a NOPASSWD sudoers rule. The learner
    # tunnel (tun0) and its /24 route are never modified, and learner lab
    # addresses are never routed through the backend tunnel.
    backend_route_gateway: str = "10.8.0.1"
    backend_route_dev: str = "tun1"
    backend_route_metric: int = 100
    backend_route_wrapper: str = "/usr/local/libexec/secforge/backend_routes"

    # Builder remote command execution for pre-capture filesystem quiescing.
    # Reuses the shared SSH identity and the hardened VpnManager transport.
    builder_ssh_user: str = "ubuntu"
    builder_ssh_key_path: str = ""  # empty falls back to vpn_gateway_ssh_key_path
    builder_quiesce_timeout_seconds: int = 30

    # Builder reconciliation worker configuration
    builder_reconciliation_interval_seconds: int = 300
    builder_stale_pending_minutes: int = 10
    builder_stale_provisioning_minutes: int = 15

    # Orphan discovery configuration
    orphan_discovery_interval_seconds: int = 3600

    # Room template / AMI capture configuration
    # Maximum time to wait for a newly created AMI to become available. The
    # template row is left in "creating" on timeout and finalized later by the
    # reconciliation worker.
    ami_wait_timeout_seconds: int = 420
    ami_wait_poll_seconds: int = 15
    # Templates still in "creating" after this many minutes are re-checked
    # against EC2 and finalized (or failed) by reconciliation.
    machine_template_creating_minutes: int = 20


settings = Settings()
