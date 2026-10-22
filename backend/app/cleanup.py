"""Read-only ownership and absence checks around the existing Terraform destroy."""
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


CLEANUP_PREFIX = "Cleanup retry required: "
LEGACY_CLEANUP_ERRORS = (
    "Failed to reinitialize Terraform workspace for destroy",
    "Terraform destroy failed",
    "Terraform init -reconfigure failed",
)


class CleanupVerificationError(Exception):
    pass


def cleanup_failed(resource):
    message = resource.error_message or ""
    return resource.status == "failed" and (
        message.startswith(CLEANUP_PREFIX) or message in LEGACY_CLEANUP_ERRORS
    )


def ownership(resource, kind):
    return {
        "Project": "SecForge", "ManagedBy": "Terraform", "ResourceType": kind,
        "RoomId": str(resource.room_id),
        ("BuilderId" if kind == "builder" else "LabInstanceId"):
            f"{'bld' if kind == 'builder' else 'lab'}-{resource.id}",
        ("CreatorId" if kind == "builder" else "LearnerId"):
            str(resource.creator_id if kind == "builder" else resource.learner_id),
    }


def state_resources(state, expected, resource):
    """Reject foreign/shared managed resources, including stale state identities."""
    if (not isinstance(state, dict) or state.get("version") != 4
            or not isinstance(state.get("resources", []), list)):
        raise CleanupVerificationError("invalid Terraform state; inspect retained workspace")
    found = {"aws_instance": set(), "aws_security_group": set()}
    for item in state.get("resources", []):
        if not isinstance(item, dict):
            raise CleanupVerificationError("invalid Terraform resource structure")
        if item.get("mode") == "data":
            continue
        kind = item.get("type")
        if item.get("mode") != "managed" or kind not in found:
            raise CleanupVerificationError("unexpected managed resource in state; ownership review required")
        if not isinstance(item.get("instances"), list):
            raise CleanupVerificationError("invalid Terraform instance structure")
        for instance in item["instances"]:
            if not isinstance(instance, dict) or not isinstance(instance.get("attributes"), dict):
                raise CleanupVerificationError("invalid Terraform instance attributes")
            a = instance.get("attributes", {})
            tags = a.get("tags_all") or a.get("tags") or {}
            if (not isinstance(a.get("id"), str) or not a["id"] or not isinstance(tags, dict)
                    or any(tags.get(k) != v for k, v in expected.items())):
                raise CleanupVerificationError("state ownership mismatch; no destroy attempted")
            # Retained/deposed instances must not be silently ignored.
            found[kind].add(a["id"])
    for kind, identifier in [("aws_instance", resource.instance_id),
                             ("aws_security_group", resource.security_group_id)]:
        if identifier and found[kind] and found[kind] != {identifier}:
            raise CleanupVerificationError("state identity differs from application record; review required")
    return found


class CleanupInventory:
    """Discover only this environment; never terminate resources directly."""
    def __init__(self, region):
        self.client = boto3.client(
            "ec2", region_name=region,
            config=Config(connect_timeout=5, read_timeout=10,
                          retries={"total_max_attempts": 1}),
        )

    def remaining(self, expected, resource):
        filters = [{"Name": "tag:" + k, "Values": [v]} for k, v in expected.items()]
        instances, groups = {}, {}
        for operation, key, target in [
            ("describe_instances", "Reservations", instances),
            ("describe_security_groups", "SecurityGroups", groups),
        ]:
            token = None
            for _ in range(10):
                args = {"Filters": filters, "MaxResults": 1000}
                if token: args["NextToken"] = token
                response = getattr(self.client, operation)(**args)
                values = response.get(key, [])
                if key == "Reservations":
                    values = [i for r in values for i in r.get("Instances", [])]
                for value in values:
                    actual = {t["Key"]: t["Value"] for t in value.get("Tags", [])}
                    if any(actual.get(k) != v for k, v in expected.items()):
                        raise CleanupVerificationError("AWS ownership inventory mismatch; no destroy attempted")
                    target[value["InstanceId" if target is instances else "GroupId"]] = value
                token = response.get("NextToken")
                if not token: break
            else:
                raise CleanupVerificationError("ownership inventory exceeded bounded pagination")
        # Tags alone cannot hide a still-existing recorded resource whose tags changed.
        for identifier, operation, arg, key, target, absent in [
            (resource.instance_id, "describe_instances", "InstanceIds", "Reservations", instances, "InvalidInstanceID.NotFound"),
            (resource.security_group_id, "describe_security_groups", "GroupIds", "SecurityGroups", groups, "InvalidGroup.NotFound"),
        ]:
            if not identifier: continue
            try:
                response = getattr(self.client, operation)(**{arg: [identifier]})
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == absent: continue
                raise
            values = response.get(key, [])
            if key == "Reservations": values = [i for r in values for i in r.get("Instances", [])]
            for value in values:
                actual = {t["Key"]: t["Value"] for t in value.get("Tags", [])}
                if any(actual.get(k) != v for k, v in expected.items()):
                    raise CleanupVerificationError("recorded AWS resource ownership changed; review required")
                target[value["InstanceId" if target is instances else "GroupId"]] = value
        alive = {iid for iid, i in instances.items() if i.get("State", {}).get("Name") != "terminated"}
        for gid in groups:
            interfaces = self.client.describe_network_interfaces(
                Filters=[{"Name": "group-id", "Values": [gid]}]
            )
            if interfaces.get("NextToken"):
                raise CleanupVerificationError("ambiguous security-group attachment inventory")
            for eni in interfaces.get("NetworkInterfaces", []):
                if eni.get("Attachment", {}).get("InstanceId") not in alive:
                    raise CleanupVerificationError("security group has an unrelated attachment; review required")
        return {"aws_instance": alive, "aws_security_group": set(groups)}
