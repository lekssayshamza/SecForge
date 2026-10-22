import logging
import time
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from app.config import settings
from app.vpn import SSHErrorKind, VpnManagerError, sanitize_error

logger = logging.getLogger(__name__)


# Errors that mean the CreateImage request may already have been accepted by
# EC2 even though no response was received. They must never be treated as a
# clean failure that a caller may blindly retry. Definitive errors (validation,
# authorization, endpoint-unreachable) are not included because EC2 never
# applied the request.
_AMBIGUOUS_CREATE_ERRORS = (
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
)

# EC2 ``ClientError`` codes where the outcome is unknown: the service side may
# or may not have applied the request. Throttling and server-side codes can be
# surfaced after EC2 processed the request (a response was lost or truncated),
# so they must be treated as ambiguous rather than as proof of failure.
_AMBIGUOUS_CREATE_CODES = {
    "RequestLimitExceeded",
    "Throttling",
    "TooManyRequestsException",
    "InternalError",
    "InternalFailure",
    "ServiceUnavailable",
    "OperationConflict",
}


def _client_error_code(exc: Optional[Exception]) -> Optional[str]:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None


def _is_ambiguous_create_exception(exc: Optional[Exception]) -> bool:
    if isinstance(exc, _AMBIGUOUS_CREATE_ERRORS):
        return True
    code = _client_error_code(exc)
    return code is not None and code in _AMBIGUOUS_CREATE_CODES


class AMIError(Exception):
    def __init__(
        self,
        message: str,
        original_error: Optional[Exception] = None,
        *,
        image_id: Optional[str] = None,
        ambiguous: bool = False,
    ):
        self.message = message
        self.original_error = original_error
        # ``image_id`` is set when an image is known to exist despite the
        # failure (for example a successful create followed by a failed
        # unfreeze), so callers can persist the reference instead of leaking
        # the resource. ``ambiguous`` marks an outcome EC2 may have applied.
        self.image_id = image_id
        self.ambiguous = ambiguous
        super().__init__(message)


class AMITimeoutError(AMIError):
    pass


class AMITransientError(AMIError):
    """A temporary inability to determine an image's state.

    Raised when describing an AMI fails (throttling, transient network error) or
    returns no record for a known id. Callers must NOT treat this as a definitive
    image failure: the AMI may still exist and become available, so the machine
    should stay in a recoverable state for a later reconciliation pass.
    """


class AmbiguousImageCreateError(AMIError):
    """CreateImage outcome is unknown: EC2 may or may not have created the AMI.

    The caller must recover by looking up the canonical capture tags and must
    not automatically re-issue CreateImage after an inconclusive lookup.
    """

    def __init__(
        self,
        message: str,
        original_error: Optional[Exception] = None,
        *,
        image_id: Optional[str] = None,
    ):
        super().__init__(
            message,
            original_error=original_error,
            image_id=image_id,
            ambiguous=True,
        )


class ImageUnfreezeError(AMIError):
    """The builder could not be unfrozen after the capture call.

    ``image_id`` carries the image returned by EC2 when creation did succeed,
    so the resource reference is preserved even though the invocation failed.
    """


class BuilderFilesystemError(AMIError):
    """A builder remote filesystem quiesce or release step failed."""


class AMIManager:
    """Backend-only wrapper around the EC2 image APIs.

    AWS credentials are only ever used here, inside the backend process. They
    are never returned to clients. ``dry_run`` mirrors ``AWSDiscoveryService``:
    the EC2 client is not created and operations are simulated, which keeps the
    test suite and local development free of AWS calls.
    """

    REQUIRED_SOURCE_TAGS = {
        "Project": "SecForge",
        "ResourceType": "builder",
    }
    SUPPORTED_STATES = {"pending", "available", "failed", "deregistered"}

    def __init__(
        self,
        region: Optional[str] = None,
        dry_run: Optional[bool] = None,
        remote: Optional[object] = None,
    ):
        self.region = region or settings.aws_region
        self.dry_run = settings.terraform_dry_run if dry_run is None else dry_run
        self._ec2_client = None
        self._remote = remote

    @property
    def ec2_client(self):
        if self._ec2_client is None and not self.dry_run:
            # CreateImage has no supported idempotency token. An SDK retry after
            # a lost response could create another image, so make one attempt.
            self._ec2_client = boto3.client(
                "ec2", region_name=self.region,
                config=Config(retries={"total_max_attempts": 1},
                              connect_timeout=10, read_timeout=30),
            )
        return self._ec2_client

    @property
    def remote_client(self):
        """Hardened remote-command runner used for builder filesystem quiescing.

        Defaults lazily to ``VpnManager`` (the existing SSH transport); tests
        inject a fake.
        """
        if self._remote is None:
            from app.vpn import VpnManager

            self._remote = VpnManager()
        return self._remote

    @staticmethod
    def _to_tag_list(tags: dict) -> list[dict]:
        return [{"Key": str(k), "Value": str(v)} for k, v in tags.items()]

    def validate_source_instance(self, instance_id: str) -> None:
        """Refuse to capture anything that is not a SecForge-managed builder."""
        if self.dry_run:
            return

        try:
            response = self.ec2_client.describe_instances(
                InstanceIds=[instance_id]
            )
        except (ClientError, BotoCoreError) as exc:
            raise AMIError(
                "Unable to verify the source instance", original_error=exc
            )

        reservations = response.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise AMIError("Source instance not found")

        instance = reservations[0]["Instances"][0]
        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
        if not all(
            tags.get(k) == v for k, v in self.REQUIRED_SOURCE_TAGS.items()
        ):
            raise AMIError(
                "Source instance is not a SecForge-managed builder"
            )
        private_ip = instance.get("PrivateIpAddress")
        if not private_ip:
            raise AMIError("Source instance has no private IP address")
        return private_ip

    def _run_builder(self, host: str, remote_args: list[str]) -> None:
        """Run a bounded, hardened remote command on the builder."""
        try:
            self.remote_client.run_builder_command(
                host,
                remote_args,
                timeout=settings.builder_quiesce_timeout_seconds,
            )
        except VpnManagerError as exc:
            detail = sanitize_error(exc)
            if exc.kind != SSHErrorKind.UNKNOWN.value:
                detail = f"{exc.kind}: {detail}"
            raise BuilderFilesystemError(  # noqa: B904
                f"Builder remote command failed: {detail}",
                original_error=exc,
            )
        except Exception as exc:
            raise BuilderFilesystemError(  # noqa: B904
                f"Builder remote command failed: {sanitize_error(exc)}",
                original_error=exc,
            )

    def _flush_builder(self, host: str) -> None:
        """Durably flush builder page cache to EBS before capture."""
        try:
            self._run_builder(host, ["sync"])
        except BuilderFilesystemError as exc:
            raise AMIError(
                "Failed to flush builder filesystem before image capture: "
                f"{exc}",
                original_error=exc,
            ) from None

    def create_image(
        self,
        instance_id: str,
        name: str,
        tags: Optional[dict] = None,
        no_reboot: bool = True,
        client_token: Optional[str] = None,
    ) -> str:
        """Capture using an instance-verified SSH pin and the existing guard."""
        if self.dry_run:
            return f"ami-dryrun-{int(time.time())}"
        builder_host = self.validate_source_instance(instance_id)
        from app.vpn import VpnManager

        remote = self.remote_client
        if isinstance(remote, VpnManager):
            try:
                console = self.ec2_client.get_console_output(
                    InstanceId=instance_id, Latest=True,
                ).get("Output", "")
            except (ClientError, BotoCoreError) as exc:
                raise AMIError("Builder console verification unavailable before image capture: "
                               + sanitize_error(exc),
                               original_error=exc) from None
            try:
                with remote.verified_builder_host(builder_host, console):
                    return self._create_image(
                        instance_id, name, tags, no_reboot, client_token,
                        _builder_host=builder_host,
                    )
            except VpnManagerError as exc:
                raise AMIError("Builder SSH trust verification failed before image capture: "
                               + sanitize_error(exc)
                               + ". A reboot may have removed the boot fingerprint from the EC2 "
                               "console; an administrator must restore the builder console identity.",
                               original_error=exc) from None
        # Explicit offline adapters implement their own remote transport.
        return self._create_image(instance_id, name, tags, no_reboot, client_token,
                                  _builder_host=builder_host)

    def _create_image(
        self,
        instance_id: str,
        name: str,
        tags: Optional[dict] = None,
        no_reboot: bool = True,
        client_token: Optional[str] = None,
        _builder_host: Optional[str] = None,
    ) -> str:
        """Create an AMI from ``instance_id`` and return its image ID.

        ``NoReboot=True`` keeps the builder running and connected so the creator
        is not interrupted while the image is being captured. Because the guest
        is not rebooted, the root filesystem is first ``sync``'d and frozen with
        ``fsfreeze -f /`` so recently written challenge data is durable in the
        EBS snapshot. Freeze and release use one pre-authenticated privileged
        SSH session, so release never needs a fresh login into a frozen guest.
        The filesystem is unfrozen immediately after the capture call returns
        (even on failure), never while awaiting image availability. A guest
        deadline also releases it if the backend disconnects or crashes.

        ``client_token`` is retained for internal caller compatibility only;
        EC2 CreateImage does not support it and it is never sent to AWS.

        Ambiguous failures (a timeout, dropped connection, or a throttling /
        server-side EC2 error where the request may have been applied) raise
        :class:`AmbiguousImageCreateError` instead of a plain failure, so
        callers recover by exact tags without re-issuing creation. If the
        capture call succeeds but the builder cannot be
        unfrozen, the returned image ID is preserved on
        :class:`ImageUnfreezeError` rather than discarded.
        """
        if self.dry_run:
            return f"ami-dryrun-{int(time.time())}"

        create_kwargs = {
            "InstanceId": instance_id,
            "Name": name,
            "Description": f"SecForge room template: {name}",
            "NoReboot": no_reboot,
            "TagSpecifications": [
                {
                    "ResourceType": "image",
                    "Tags": self._to_tag_list(tags or {}),
                },
                # Keep retention permissions scoped to owned capture resources.
                # CreateImage supports tagging its EBS snapshots atomically too.
                {
                    "ResourceType": "snapshot",
                    "Tags": self._to_tag_list(tags or {}),
                },
            ],
        }

        builder_host = _builder_host or self.validate_source_instance(instance_id)
        self._flush_builder(builder_host)
        quiesce = self.remote_client.frozen_builder(
            builder_host, timeout=settings.builder_quiesce_timeout_seconds,
        )
        try:
            quiesce.__enter__()
        except Exception as exc:
            raise AMIError(
                f"Failed to freeze builder filesystem before image capture: {sanitize_error(exc)}",
                original_error=exc,
            ) from None

        create_error: Optional[Exception] = None
        unfreeze_error: Optional[BuilderFilesystemError] = None
        response = None
        try:
            response = self.ec2_client.create_image(**create_kwargs)
        except (ClientError, BotoCoreError) as exc:
            create_error = exc
        finally:
            try:
                quiesce.__exit__(None, None, None)
            except Exception as exc:
                unfreeze_error = BuilderFilesystemError(
                    f"Builder filesystem release failed: {sanitize_error(exc)}",
                    original_error=exc,
                )

        image_id = response.get("ImageId") if response else None
        ambiguous = _is_ambiguous_create_exception(create_error)

        if unfreeze_error is not None:
            message = (
                "Builder filesystem could not be unfrozen after image capture: "
                f"{unfreeze_error}"
            )
            if create_error is not None:
                message += "; image creation also failed"
            elif image_id is None:
                message += "; image creation outcome is unknown"
            raise ImageUnfreezeError(
                message,
                original_error=unfreeze_error,
                image_id=image_id,
                ambiguous=ambiguous or image_id is None,
            ) from None

        if create_error is not None:
            if ambiguous:
                raise AmbiguousImageCreateError(
                    "Image creation outcome is unknown: "
                    f"{sanitize_error(create_error)}",
                    original_error=create_error,
                ) from None
            raise AMIError(
                f"Failed to create AMI: {sanitize_error(create_error)}",
                original_error=create_error,
            )

        if not image_id:
            raise AmbiguousImageCreateError(
                "EC2 accepted image creation but returned no image ID"
            )
        return image_id

    def find_image_id_by_tags(self, tags: dict) -> Optional[str]:
        """Return the newest image matching every supplied tag, if any.

        Used to recover an ambiguous capture without re-issuing ``CreateImage``:
        an image that EC2 did create is discoverable by its capture tags even if
        the original response was lost.

        ``describe_images`` pages its results, so matching pages are followed
        until ``NextToken`` is exhausted. The tag filters are applied on every
        page, so an unrelated image can never be associated with the capture.
        """
        if self.dry_run:
            return None

        filters = [
            {"Name": f"tag:{key}", "Values": [str(value)]}
            for key, value in tags.items()
        ]
        filters.append({"Name": "state", "Values": ["pending", "available"]})
        images: list[dict] = []
        next_token: Optional[str] = None
        while True:
            params: dict = {"Filters": filters}
            if next_token:
                params["NextToken"] = next_token
            try:
                response = self.ec2_client.describe_images(**params)
            except (ClientError, BotoCoreError) as exc:
                raise AMIError(
                    "Failed to search for an existing AMI by tags",
                    original_error=exc,
                )
            images.extend(response.get("Images", []))
            next_token = response.get("NextToken")
            if not next_token:
                break

        if not images:
            return None
        images.sort(key=lambda image: image.get("CreationDate", ""), reverse=True)
        return images[0].get("ImageId")

    def recover_image(
        self,
        instance_id: str,
        name: str,
        tags: Optional[dict] = None,
        client_token: Optional[str] = None,
    ) -> str:
        """Recover an image from an ambiguous capture without duplicating it.

        Return an image matching the capture tags. A missing image may simply
        not be visible yet; leave the outcome uncertain rather than creating
        another image. Other arguments are retained for caller compatibility.
        """
        existing = self.find_image_id_by_tags(tags or {})
        if existing:
            return existing
        raise AmbiguousImageCreateError(
            "Capture outcome unknown; no image found by capture tags"
        )

    def get_image_state(self, image_id: str) -> Optional[str]:
        if self.dry_run:
            return "available"

        try:
            response = self.ec2_client.describe_images(ImageIds=[image_id])
        except (ClientError, BotoCoreError) as exc:
            raise AMIError("Failed to describe AMI", original_error=exc)

        images = response.get("Images", [])
        if not images:
            return None
        state = images[0].get("State")
        if state in ("available", "failed", "deregistered"):
            return state
        return "pending"

    def wait_until_available(
        self,
        image_id: str,
        timeout: Optional[int] = None,
        poll_interval: Optional[int] = None,
    ) -> None:
        timeout = settings.ami_wait_timeout_seconds if timeout is None else timeout
        poll_interval = (
            settings.ami_wait_poll_seconds if poll_interval is None else poll_interval
        )

        if self.dry_run:
            return

        deadline = time.monotonic() + timeout
        while True:
            try:
                state = self.get_image_state(image_id)
            except AMIError as exc:
                # A describe failure (throttling, transient network error) is
                # temporary and must not be mistaken for a failed image.
                raise AMITransientError(
                    f"Unable to determine state of AMI {image_id}: {exc}",
                    original_error=getattr(exc, "original_error", None),
                ) from exc
            if state == "available":
                return
            if state in ("failed", "deregistered"):
                raise AMIError(f"AMI {image_id} did not become available")
            if state is None:
                # The image is not (yet) visible. Treat as transient so a
                # just-created AMI is not orphaned by eventual consistency.
                if time.monotonic() >= deadline:
                    raise AMITimeoutError(
                        f"AMI {image_id} is still not visible after {timeout} seconds"
                    )
                time.sleep(poll_interval)
                continue

            if time.monotonic() >= deadline:
                raise AMITimeoutError(
                    f"AMI {image_id} is still pending after {timeout} seconds"
                )
            time.sleep(poll_interval)

    def deregister_image(self, image_id: str) -> None:
        """Best-effort AMI cleanup: deregister the image and its snapshots."""
        if self.dry_run:
            return

        try:
            response = self.ec2_client.describe_images(ImageIds=[image_id])
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Unable to describe AMI %s for cleanup: %s", image_id, exc)
            response = {"Images": []}

        snapshot_ids = []
        for image in response.get("Images", []):
            for mapping in image.get("BlockDeviceMappings", []):
                snapshot_id = mapping.get("Ebs", {}).get("SnapshotId")
                if snapshot_id:
                    snapshot_ids.append(snapshot_id)

        try:
            self.ec2_client.deregister_image(ImageId=image_id)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Unable to deregister AMI %s: %s", image_id, exc)
            return

        for snapshot_id in snapshot_ids:
            try:
                self.ec2_client.delete_snapshot(SnapshotId=snapshot_id)
            except (ClientError, BotoCoreError) as exc:
                logger.warning(
                    "Unable to delete snapshot %s for AMI %s: %s",
                    snapshot_id,
                    image_id,
                    exc,
                )
