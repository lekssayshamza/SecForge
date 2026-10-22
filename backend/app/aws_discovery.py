import logging
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class InstanceInfo:
    instance_id: str
    private_ip: Optional[str]
    state: str
    builder_id: str
    tags: dict


@dataclass
class SecurityGroupInfo:
    group_id: str
    group_name: str
    builder_id: str
    tags: dict


@dataclass
class BuilderResources:
    builder_id: str
    instances: list[InstanceInfo]
    security_groups: list[SecurityGroupInfo]


class AWSDiscoveryError(Exception):
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        self.message = message
        self.original_error = original_error
        super().__init__(message)


class AWSDiscoveryService:
    REQUIRED_TAGS = {
        "Project": "SecForge",
        "ManagedBy": "Terraform",
        "ResourceType": "builder",
    }

    def __init__(self, region: Optional[str] = None, dry_run: bool = False):
        self.region = region or settings.aws_region
        self.dry_run = dry_run
        self._ec2_client = None

    @property
    def ec2_client(self):
        if self._ec2_client is None and not self.dry_run:
            self._ec2_client = boto3.client("ec2", region_name=self.region)
        return self._ec2_client

    def _validate_builder_id(self, builder_id: str) -> Optional[int]:
        if not builder_id.startswith("bld-"):
            return None
        try:
            return int(builder_id[4:])
        except ValueError:
            return None

    def _has_required_tags(self, tags: dict) -> bool:
        for key, expected_value in self.REQUIRED_TAGS.items():
            if tags.get(key) != expected_value:
                return False
        return True

    def _extract_builder_id(self, tags: dict) -> Optional[str]:
        builder_id = tags.get("BuilderId")
        if builder_id and self._validate_builder_id(builder_id) is not None:
            return builder_id
        return None

    def discover_instances(self, builder_id: Optional[str] = None) -> list[InstanceInfo]:
        if self.dry_run:
            logger.debug("Dry-run mode: skipping EC2 instance discovery")
            return []

        try:
            filters = [
                {"Name": "tag:Project", "Values": ["SecForge"]},
                {"Name": "tag:ManagedBy", "Values": ["Terraform"]},
                {"Name": "tag:ResourceType", "Values": ["builder"]},
            ]
            if builder_id:
                filters.append({"Name": "tag:BuilderId", "Values": [builder_id]})

            paginator = self.ec2_client.get_paginator("describe_instances")
            instances = []

            for page in paginator.paginate(Filters=filters):
                for reservation in page.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}

                        if not self._has_required_tags(tags):
                            continue

                        extracted_builder_id = self._extract_builder_id(tags)
                        if not extracted_builder_id:
                            continue

                        if builder_id and extracted_builder_id != builder_id:
                            continue

                        instances.append(InstanceInfo(
                            instance_id=instance["InstanceId"],
                            private_ip=instance.get("PrivateIpAddress"),
                            state=instance["State"]["Name"],
                            builder_id=extracted_builder_id,
                            tags=tags,
                        ))

            return instances

        except (ClientError, BotoCoreError) as e:
            raise AWSDiscoveryError(f"Failed to discover EC2 instances: {e}", e)

    def discover_security_groups(self, builder_id: Optional[str] = None) -> list[SecurityGroupInfo]:
        if self.dry_run:
            logger.debug("Dry-run mode: skipping security group discovery")
            return []

        try:
            filters = [
                {"Name": "tag:Project", "Values": ["SecForge"]},
                {"Name": "tag:ManagedBy", "Values": ["Terraform"]},
                {"Name": "tag:ResourceType", "Values": ["builder"]},
            ]
            if builder_id:
                filters.append({"Name": "tag:BuilderId", "Values": [builder_id]})

            paginator = self.ec2_client.get_paginator("describe_security_groups")
            security_groups = []

            for page in paginator.paginate(Filters=filters):
                for sg in page.get("SecurityGroups", []):
                    tags = {tag["Key"]: tag["Value"] for tag in sg.get("Tags", [])}

                    if not self._has_required_tags(tags):
                        continue

                    extracted_builder_id = self._extract_builder_id(tags)
                    if not extracted_builder_id:
                        continue

                    if builder_id and extracted_builder_id != builder_id:
                        continue

                    security_groups.append(SecurityGroupInfo(
                        group_id=sg["GroupId"],
                        group_name=sg["GroupName"],
                        builder_id=extracted_builder_id,
                        tags=tags,
                    ))

            return security_groups

        except (ClientError, BotoCoreError) as e:
            raise AWSDiscoveryError(f"Failed to discover security groups: {e}", e)

    def discover_builder_resources(self, builder_id: str) -> BuilderResources:
        if not self._validate_builder_id(builder_id):
            raise AWSDiscoveryError(f"Invalid builder_id format: {builder_id}")

        instances = self.discover_instances(builder_id)
        security_groups = self.discover_security_groups(builder_id)

        return BuilderResources(
            builder_id=builder_id,
            instances=instances,
            security_groups=security_groups,
        )

    def discover_all_builder_resources(self) -> list[BuilderResources]:
        instances = self.discover_instances()
        security_groups = self.discover_security_groups()

        builder_resources_map: dict[str, BuilderResources] = {}

        for instance in instances:
            if instance.builder_id not in builder_resources_map:
                builder_resources_map[instance.builder_id] = BuilderResources(
                    builder_id=instance.builder_id,
                    instances=[],
                    security_groups=[],
                )
            builder_resources_map[instance.builder_id].instances.append(instance)

        for sg in security_groups:
            if sg.builder_id not in builder_resources_map:
                builder_resources_map[sg.builder_id] = BuilderResources(
                    builder_id=sg.builder_id,
                    instances=[],
                    security_groups=[],
                )
            builder_resources_map[sg.builder_id].security_groups.append(sg)

        return list(builder_resources_map.values())