#!/usr/bin/env python3
"""AMIManager tests: CreateImage/DescribeImages/wait/cleanup with a fake client.

Also covers builder filesystem quiescing before capture: ``sync`` then
``fsfreeze -f /`` before CreateImage (NoReboot=True), and ``fsfreeze -u /``
immediately after the capture call is accepted, including failure paths.
"""

from unittest.mock import MagicMock
from contextlib import contextmanager

import pytest
from botocore.session import get_session
from botocore.validate import validate_parameters
from botocore.exceptions import ClientError, ParamValidationError, ReadTimeoutError

from app.ami_manager import (
    AMIError,
    AMIManager,
    AMITimeoutError,
    AMITransientError,
    AmbiguousImageCreateError,
    BuilderFilesystemError,
    ImageUnfreezeError,
)
from app.config import settings
from app.vpn import VpnManagerError


class FakeEC2:
    def __init__(self, states=None, instances=None, images=None, event_log=None,
                 create_error=None, tag_images=None, describe_error=None):
        self.states = list(states or [])
        self.instances = instances if instances is not None else []
        self.images = images or {}
        self.created = []
        self.deregistered = []
        self.deleted_snapshots = []
        self.describe_images_calls = 0
        self.event_log = event_log
        self.create_error = create_error
        self.tag_images = list(tag_images or [])
        self.tag_filters = []
        self.describe_error = describe_error

    def describe_instances(self, InstanceIds=None):
        return {"Reservations": self.instances}

    def get_console_output(self, **kwargs):
        return {"Output": ""}

    def create_image(self, **kwargs):
        if self.event_log is not None:
            self.event_log.append("create_image")
        self.created.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return {"ImageId": "ami-0created"}

    def describe_images(self, ImageIds=None, Filters=None):
        self.describe_images_calls += 1
        if self.describe_error is not None:
            raise self.describe_error
        if Filters is not None:
            self.tag_filters.append(Filters)
            return {"Images": list(self.tag_images)}
        image_id = ImageIds[0]
        if self.states:
            state = self.states.pop(0)
            return {"Images": [{"ImageId": image_id, "State": state}]}
        return {"Images": [self.images.get(image_id, {"ImageId": image_id, "State": "pending"})]}

    def deregister_image(self, ImageId=None):
        self.deregistered.append(ImageId)

    def delete_snapshot(self, SnapshotId=None):
        self.deleted_snapshots.append(SnapshotId)


class FakeRemote:
    """Records builder remote commands; can fail a specific step by label."""

    def __init__(self, event_log=None, fail_on=None):
        self.event_log = event_log
        self.fail_on = fail_on
        self.calls = []

    def run_builder_command(self, builder_host, remote_args, timeout=None):
        args = list(remote_args)
        self.calls.append({"host": builder_host, "args": args, "timeout": timeout})
        label = self._label(args)
        if self.event_log is not None:
            self.event_log.append(label)
        if self.fail_on == label:
            raise RuntimeError(f"{label} failed")
        return MagicMock(stdout="", stderr="", returncode=0)

    @contextmanager
    def frozen_builder(self, builder_host, timeout=None):
        self.run_builder_command(builder_host, ["sudo", "-n", "fsfreeze", "-f", "/"], timeout)
        try:
            yield
        finally:
            self.run_builder_command(builder_host, ["sudo", "-n", "fsfreeze", "-u", "/"], timeout)

    @staticmethod
    def _label(args):
        if args == ["sync"]:
            return "sync"
        if "fsfreeze" in args and "-f" in args:
            return "freeze"
        if "fsfreeze" in args and "-u" in args:
            return "unfreeze"
        return "other"


def manager_with(fake, remote=None):
    manager = AMIManager(dry_run=False, remote=remote)
    manager._ec2_client = fake
    return manager


def builder_instance():
    return [
        {
            "Instances": [
                {
                    "InstanceId": "i-0builder",
                    "PrivateIpAddress": "10.0.1.100",
                    "Tags": [
                        {"Key": "Project", "Value": "SecForge"},
                        {"Key": "ResourceType", "Value": "builder"},
                    ],
                }
            ]
        }
    ]


class TestDryRun:
    def test_dry_run_create_and_wait(self):
        manager = AMIManager(dry_run=True)
        image_id = manager.create_image("i-0builder", "name", tags={})
        assert image_id.startswith("ami-dryrun-")
        manager.wait_until_available(image_id)
        assert manager.get_image_state(image_id) == "available"
        manager.deregister_image(image_id)

    def test_dry_run_skips_builder_quiescing(self):
        remote = FakeRemote()
        fake = FakeEC2(instances=builder_instance())
        manager = AMIManager(dry_run=True, remote=remote)
        manager._ec2_client = fake
        image_id = manager.create_image("i-0builder", "name", tags={})
        assert image_id.startswith("ami-dryrun-")
        assert remote.calls == []
        assert fake.created == []


class TestCreateImage:
    def test_creates_ami_with_no_reboot_and_tags(self, monkeypatch):
        fake = FakeEC2(instances=builder_instance())
        remote = FakeRemote()
        manager = manager_with(fake, remote)

        image_id = manager.create_image(
            "i-0builder", "secforge-room-1-v1", tags={"RoomId": "1"}
        )

        assert image_id == "ami-0created"
        call = fake.created[0]
        assert call["InstanceId"] == "i-0builder"
        assert call["NoReboot"] is True
        assert call["TagSpecifications"][0]["ResourceType"] == "image"
        assert {"Key": "RoomId", "Value": "1"} in call["TagSpecifications"][0]["Tags"]
        assert call["TagSpecifications"][1]["ResourceType"] == "snapshot"
        assert call["TagSpecifications"][1]["Tags"] == call["TagSpecifications"][0]["Tags"]

    def test_rejects_non_secforge_source_instance(self):
        remote = FakeRemote()
        fake = FakeEC2(instances=[{"Instances": [{"InstanceId": "i-x", "Tags": []}]}])
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError):
            manager.create_image("i-x", "name", tags={})
        assert fake.created == []
        assert remote.calls == []

    def test_missing_source_instance_rejected(self):
        remote = FakeRemote()
        fake = FakeEC2(instances=[])
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError):
            manager.create_image("i-missing", "name", tags={})
        assert remote.calls == []

    def test_source_without_private_ip_rejected(self):
        remote = FakeRemote()
        fake = FakeEC2(
            instances=[
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0builder",
                            "Tags": [
                                {"Key": "Project", "Value": "SecForge"},
                                {"Key": "ResourceType", "Value": "builder"},
                            ],
                        }
                    ]
                }
            ]
        )
        manager = manager_with(fake, remote)
        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})
        assert "private IP" in str(exc_info.value)
        assert fake.created == []


class TestWait:
    def test_waits_until_available(self, monkeypatch):
        fake = FakeEC2(instances=builder_instance(), states=["pending", "pending", "available"])
        manager = manager_with(fake, FakeRemote())
        monkeypatch.setattr("app.ami_manager.time.sleep", lambda _s: None)

        manager.wait_until_available("ami-0created", timeout=100, poll_interval=1)
        assert fake.describe_images_calls == 3

    def test_times_out_while_pending(self, monkeypatch):
        fake = FakeEC2(instances=builder_instance())
        manager = manager_with(fake, FakeRemote())
        monkeypatch.setattr("app.ami_manager.time.sleep", lambda _s: None)

        with pytest.raises(AMITimeoutError):
            manager.wait_until_available("ami-0created", timeout=0, poll_interval=1)

    def test_failed_state_raises(self, monkeypatch):
        fake = FakeEC2(instances=builder_instance(), states=["failed"])
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMIError):
            manager.wait_until_available("ami-0created", timeout=100, poll_interval=1)


class TestDeregister:
    def test_deregisters_image_and_snapshots(self):
        fake = FakeEC2(
            images={
                "ami-0old": {
                    "ImageId": "ami-0old",
                    "State": "available",
                    "BlockDeviceMappings": [
                        {"Ebs": {"SnapshotId": "snap-1"}},
                        {"Ebs": {"SnapshotId": "snap-2"}},
                    ],
                }
            }
        )
        manager = manager_with(fake, FakeRemote())

        manager.deregister_image("ami-0old")

        assert fake.deregistered == ["ami-0old"]
        assert fake.deleted_snapshots == ["snap-1", "snap-2"]


class TestBuilderQuiescing:
    def test_sync_then_freeze_then_create_then_unfreeze_order(self):
        log = []
        fake = FakeEC2(instances=builder_instance(), event_log=log)
        remote = FakeRemote(event_log=log)
        manager = manager_with(fake, remote)

        manager.create_image("i-0builder", "secforge-room-1-v1", tags={})

        assert log == ["sync", "freeze", "create_image", "unfreeze"]
        assert remote.calls[0]["args"] == ["sync"]
        assert remote.calls[1]["args"] == ["sudo", "-n", "fsfreeze", "-f", "/"]
        assert remote.calls[2]["args"] == ["sudo", "-n", "fsfreeze", "-u", "/"]
        assert all(call["host"] == "10.0.1.100" for call in remote.calls)

    def test_commands_use_bounded_timeout(self):
        fake = FakeEC2(instances=builder_instance())
        remote = FakeRemote()
        manager = manager_with(fake, remote)

        manager.create_image("i-0builder", "name", tags={})

        timeout = settings.builder_quiesce_timeout_seconds
        assert timeout > 0
        assert all(call["timeout"] == timeout for call in remote.calls)
        assert all(call["timeout"] is not None for call in remote.calls)

    def test_create_image_uses_no_reboot_true(self):
        fake = FakeEC2(instances=builder_instance())
        manager = manager_with(fake, FakeRemote())

        manager.create_image("i-0builder", "name", tags={})

        assert fake.created[0]["NoReboot"] is True

    def test_unfreeze_after_create_image_accepted(self):
        log = []
        fake = FakeEC2(
            instances=builder_instance(), event_log=log, create_error=None
        )
        remote = FakeRemote(event_log=log)
        manager = manager_with(fake, remote)

        manager.create_image("i-0builder", "name", tags={})

        assert log.index("create_image") < log.index("unfreeze")

    def test_unfreeze_runs_when_create_image_raises(self):
        log = []
        fake = FakeEC2(
            instances=builder_instance(),
            event_log=log,
            create_error=ClientError(
                error_response={
                    "Error": {"Code": "InvalidParameter", "Message": "boom"}
                },
                operation_name="CreateImage",
            ),
        )
        remote = FakeRemote(event_log=log)
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert "Failed to create AMI" in str(exc_info.value)
        assert log[-1] == "unfreeze"

    def test_unfreeze_not_attempted_when_freeze_fails(self):
        log = []
        fake = FakeEC2(instances=builder_instance(), event_log=log)
        remote = FakeRemote(event_log=log, fail_on="freeze")
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert "freeze" in str(exc_info.value)
        assert fake.created == []
        assert "unfreeze" not in log

    def test_nothing_runs_when_sync_fails(self):
        log = []
        fake = FakeEC2(instances=builder_instance(), event_log=log)
        remote = FakeRemote(event_log=log, fail_on="sync")
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert "flush" in str(exc_info.value)
        assert fake.created == []
        assert "freeze" not in log
        assert "unfreeze" not in log

    def test_unfreeze_failure_reported_clearly(self):
        fake = FakeEC2(instances=builder_instance())
        remote = FakeRemote(fail_on="unfreeze")
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert "could not be unfrozen" in str(exc_info.value)

    def test_unfreeze_failure_mentions_create_failure_when_both(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ClientError(
                error_response={
                    "Error": {"Code": "InvalidParameter", "Message": "boom"}
                },
                operation_name="CreateImage",
            ),
        )
        remote = FakeRemote(fail_on="unfreeze")
        manager = manager_with(fake, remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        message = str(exc_info.value)
        assert "could not be unfrozen" in message
        assert "image creation also failed" in message


class TestErrorTypes:
    def test_builder_filesystem_error_is_ami_error(self):
        assert issubclass(BuilderFilesystemError, AMIError)


class FailingRemote:
    """Remote that always raises the supplied exception on any command."""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def run_builder_command(self, builder_host, remote_args, timeout=None):
        self.calls.append(list(remote_args))
        raise self.error


class TestCaptureDiagnostics:
    """Failed captures must preserve an actionable, accurate cause."""

    def test_flush_failure_preserves_classified_cause(self):
        remote = FailingRemote(
            VpnManagerError(
                "SSH command failed with exit code 255: Connection timed out",
                returncode=255,
                kind="connection",
            )
        )
        manager = manager_with(FakeEC2(instances=builder_instance()), remote)

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        message = str(exc_info.value)
        assert "flush" in message
        assert "connection" in message
        assert "Connection timed out" in message

    def test_create_failure_message_includes_cause(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ClientError(
                error_response={
                    "Error": {"Code": "InvalidParameter", "Message": "bad-param"}
                },
                operation_name="CreateImage",
            ),
        )
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        message = str(exc_info.value)
        assert "Failed to create AMI" in message
        assert "InvalidParameter" in message or "boom" in message or "Invalid" in message


class TestCreateImageRequestContract:
    def test_capture_request_matches_installed_ec2_model(self, monkeypatch):
        """Validate the real request offline, including the capture identity input."""
        from app.capture_identity import capture_client_token, capture_image_tags

        input_shape = (
            get_session()
            .get_service_model("ec2")
            .operation_model("CreateImage")
            .input_shape
        )

        def validated_create_image(**params):
            validate_parameters(params, input_shape)
            return {"ImageId": "ami-offline-contract"}

        # No boto3 client is constructed. Only this validating seam can receive
        # the request; guest commands are recorded by the existing fake remote.
        client = MagicMock(spec=["create_image"])
        client.create_image.side_effect = validated_create_image
        manager = manager_with(client, FakeRemote())
        monkeypatch.setattr(manager, "validate_source_instance", lambda _: "offline-builder")

        assert manager.create_image(
            "i-offline-contract",
            "offline-contract-capture",
            tags=capture_image_tags(
                room_id=1, template_id=1, version=1, builder_id=1, capture_id=1,
            ),
            client_token=capture_client_token(1),
        ) == "ami-offline-contract"


class TestCreateImageIdempotency:
    def test_legacy_capture_label_is_not_forwarded_to_aws(self):
        fake = FakeEC2(instances=builder_instance())
        manager = manager_with(fake, FakeRemote())

        manager.create_image(
            "i-0builder", "name", tags={}, client_token="tok-123"
        )

        assert "ClientToken" not in fake.created[0]

    def test_omits_client_token_by_default(self):
        fake = FakeEC2(instances=builder_instance())
        manager = manager_with(fake, FakeRemote())

        manager.create_image("i-0builder", "name", tags={})

        assert "ClientToken" not in fake.created[0]


class TestCreateImageSafety:
    def test_sdk_automatic_retries_disabled(self, monkeypatch):
        factory = MagicMock()
        monkeypatch.setattr("app.ami_manager.boto3.client", factory)
        manager = AMIManager(dry_run=False)
        assert manager.ec2_client is factory.return_value
        assert factory.call_args.kwargs["config"].retries == {"total_max_attempts": 1}

    def test_parameter_validation_failure_is_definitive_and_unfreezes(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ParamValidationError(report="Invalid offline request"),
        )
        events = []
        fake.event_log = events
        manager = manager_with(fake, FakeRemote(event_log=events))
        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-offline", "offline", tags={})
        assert isinstance(exc_info.value.original_error, ParamValidationError)
        assert exc_info.value.ambiguous is False
        assert events == ["sync", "freeze", "create_image", "unfreeze"]
        assert len(fake.created) == 1

    def test_ambiguous_create_with_invisible_image_never_retries(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ReadTimeoutError(endpoint_url="https://ec2", error="lost response"),
        )
        manager = manager_with(fake, FakeRemote())
        with pytest.raises(AmbiguousImageCreateError):
            manager.create_image("i-offline", "offline", tags={"CaptureRequestId": "1"})
        for _ in range(2):
            with pytest.raises(AmbiguousImageCreateError):
                manager.recover_image("i-offline", "offline", tags={"CaptureRequestId": "1"})
        assert len(fake.created) == 1


class TestAmbiguousCreateOutcome:
    def test_timeout_is_reported_as_ambiguous(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ReadTimeoutError(
                endpoint_url="https://ec2", error="timed out"
            ),
        )
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AmbiguousImageCreateError) as exc_info:
            manager.create_image(
                "i-0builder", "name", tags={}, client_token="tok-1"
            )

        assert exc_info.value.ambiguous is True
        assert exc_info.value.image_id is None
        # The ambiguous call is not retried inside create_image.
        assert len(fake.created) == 1

    def test_definitive_client_error_is_not_ambiguous(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ClientError(
                error_response={
                    "Error": {"Code": "InvalidParameter", "Message": "bad"}
                },
                operation_name="CreateImage",
            ),
        )
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMIError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert not isinstance(exc_info.value, AmbiguousImageCreateError)
        assert exc_info.value.ambiguous is False

    def test_unfreeze_failure_after_success_preserves_image_id(self):
        fake = FakeEC2(instances=builder_instance())
        remote = FakeRemote(fail_on="unfreeze")
        manager = manager_with(fake, remote)

        with pytest.raises(ImageUnfreezeError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert exc_info.value.image_id == "ami-0created"
        assert exc_info.value.ambiguous is False
        assert "could not be unfrozen" in str(exc_info.value)

    def test_unfreeze_failure_with_ambiguous_create_is_recoverable(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ReadTimeoutError(
                endpoint_url="https://ec2", error="timed out"
            ),
        )
        remote = FakeRemote(fail_on="unfreeze")
        manager = manager_with(fake, remote)

        with pytest.raises(ImageUnfreezeError) as exc_info:
            manager.create_image("i-0builder", "name", tags={})

        assert exc_info.value.image_id is None
        assert exc_info.value.ambiguous is True

    def test_recovery_prefers_existing_tagged_image(self):
        fake = FakeEC2(
            instances=builder_instance(),
            tag_images=[
                {"ImageId": "ami-existing", "State": "available",
                 "CreationDate": "2026-01-01T00:00:00.000Z"}
            ],
        )
        manager = manager_with(fake, FakeRemote())

        image_id = manager.recover_image(
            "i-0builder", "name", tags={"RoomTemplateId": "5"},
            client_token="secforge-tmpl-5",
        )

        assert image_id == "ami-existing"
        assert fake.created == []

    def test_recovery_never_reissues_create_when_no_image(self):
        fake = FakeEC2(instances=builder_instance(), tag_images=[])
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AmbiguousImageCreateError):
            manager.recover_image(
                "i-0builder", "name", tags={"RoomTemplateId": "5"},
                client_token="secforge-tmpl-5",
            )
        assert fake.created == []

    def test_recovery_filters_by_capture_tags(self):
        fake = FakeEC2(instances=builder_instance(), tag_images=[])
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AmbiguousImageCreateError):
            manager.recover_image(
                "i-0builder", "name", tags={"RoomTemplateId": "5", "ResourceType": "ami"},
            )

        applied = {f["Name"] for f in fake.tag_filters[0]}
        assert "tag:RoomTemplateId" in applied
        assert "tag:ResourceType" in applied
        assert "state" in applied

    def test_find_image_by_tags_paginates(self):
        class PagedEC2:
            def __init__(self):
                self.tokens = []
                self.filter_pages = []

            def describe_images(self, Filters=None, NextToken=None):
                self.tokens.append(NextToken)
                self.filter_pages.append(set(f["Name"] for f in (Filters or [])))
                if NextToken is None:
                    return {
                        "Images": [
                            {
                                "ImageId": "ami-old",
                                "State": "available",
                                "CreationDate": "2026-01-01T00:00:00.000Z",
                            }
                        ],
                        "NextToken": "tok-page-2",
                    }
                return {
                    "Images": [
                        {
                            "ImageId": "ami-new",
                            "State": "available",
                            "CreationDate": "2026-02-01T00:00:00.000Z",
                        }
                    ]
                }

        fake = PagedEC2()
        manager = manager_with(fake, FakeRemote())

        image_id = manager.find_image_id_by_tags({"RoomTemplateId": "5"})

        # The newest image is chosen across pages.
        assert image_id == "ami-new"
        assert fake.tokens == [None, "tok-page-2"]
        # The exact capture-tag filters are applied on every page.
        for applied in fake.filter_pages:
            assert "tag:RoomTemplateId" in applied
            assert "state" in applied


class TestServiceCodeAmbiguity:
    @pytest.mark.parametrize(
        "code",
        [
            "RequestLimitExceeded",
            "Throttling",
            "TooManyRequestsException",
            "InternalError",
            "InternalFailure",
            "ServiceUnavailable",
            "OperationConflict",
        ],
    )
    def test_service_and_throttle_codes_are_ambiguous(self, code):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ClientError(
                error_response={"Error": {"Code": code, "Message": "boom"}},
                operation_name="CreateImage",
            ),
        )
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AmbiguousImageCreateError) as exc_info:
            manager.create_image(
                "i-0builder",
                "name",
                tags={"RoomTemplateId": "5"},
                client_token="secforge-tmpl-5",
            )

        assert exc_info.value.ambiguous is True
        assert exc_info.value.image_id is None
        # The ambiguous call is not re-issued inside create_image.
        assert len(fake.created) == 1

    def test_image_exists_despite_ambiguous_error_is_recovered(self):
        fake = FakeEC2(
            instances=builder_instance(),
            create_error=ClientError(
                error_response={
                    "Error": {"Code": "ServiceUnavailable", "Message": "boom"}
                },
                operation_name="CreateImage",
            ),
            tag_images=[
                {
                    "ImageId": "ami-created-despite-error",
                    "State": "available",
                    "CreationDate": "2026-03-01T00:00:00.000Z",
                }
            ],
        )
        manager = manager_with(fake, FakeRemote())
        tags = {"RoomTemplateId": "7", "Version": "1", "CaptureRequestId": "11"}

        with pytest.raises(AmbiguousImageCreateError):
            manager.create_image(
                "i-0builder",
                "name",
                tags=tags,
                client_token="secforge-tmpl-7",
            )

        # The exact capture tags locate the image EC2 did create, so recovery
        # returns it without issuing a second CreateImage.
        recovered = manager.recover_image(
            "i-0builder",
            "name",
            tags=tags,
            client_token="secforge-tmpl-7",
        )
        assert recovered == "ami-created-despite-error"
        assert len(fake.created) == 1


class TestWaitTransientVsDefinitive:
    def test_describe_failure_is_transient(self):
        fake = FakeEC2(
            states=None,
            describe_error=ClientError(
                error_response={
                    "Error": {"Code": "Throttling", "Message": "slow down"}
                },
                operation_name="DescribeImages",
            ),
        )
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMITransientError) as exc_info:
            manager.wait_until_available("ami-0created", timeout=1, poll_interval=0)

        assert isinstance(exc_info.value, AMIError)

    def test_missing_image_is_not_immediately_definitive(self):
        # An empty DescribeImages response (not yet visible) must not be
        # reported as a definitive failure; it becomes a timeout instead.
        fake = FakeEC2(images={})
        fake.images = {}
        fake.describe_images = lambda ImageIds=None, Filters=None: {"Images": []}
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMITimeoutError):
            manager.wait_until_available("ami-0created", timeout=0, poll_interval=0)

    def test_failed_state_is_definitive(self):
        fake = FakeEC2(states=["failed"])
        manager = manager_with(fake, FakeRemote())

        with pytest.raises(AMIError) as exc_info:
            manager.wait_until_available("ami-0created", timeout=5, poll_interval=0)

        assert not isinstance(exc_info.value, AMITimeoutError)
        assert not isinstance(exc_info.value, AMITransientError)
