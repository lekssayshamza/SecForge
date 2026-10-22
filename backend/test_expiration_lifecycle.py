#!/usr/bin/env python3
"""
SecForge Builder Expiration/Reconciliation Integration Test

This test verifies the complete real-AWS builder expiration lifecycle:
1. Creates a real builder through the normal application flow
2. Updates its expires_at to ~60-70 seconds in the future
3. Runs reconciliation worker with 10-second interval
4. Verifies complete expiration → stop → destroy lifecycle

Safety: Only operates on the single test builder created by this script.
"""

import os
import sys
import time
import logging
import asyncio
from datetime import datetime, timedelta

# Ensure we use the test environment
os.environ.setdefault("BUILDER_RECONCILIATION_INTERVAL_SECONDS", "10")

# Add project root to path
sys.path.insert(0, "/home/hamza/secforge/backend")

from app.database import SessionLocal
from app.models import BuilderEnvironment, Room, User
from app.lab_manager import LabManager
from app.reconciliation import ReconciliationWorker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def wait_for_builder_running(builder_id, timeout=300):
    """Wait for builder to reach 'running' status."""
    start = time.time()
    while time.time() - start < timeout:
        # Use a fresh session for each poll to avoid stale SQLAlchemy session cache
        db = SessionLocal()
        try:
            builder = db.get(BuilderEnvironment, builder_id)
            if builder:
                if builder.status == "running":
                    logger.info(f"Builder {builder_id} is now running")
                    return True
                if builder.status == "failed":
                    raise RuntimeError(f"Builder {builder_id} failed: {builder.error_message}")
                logger.info(f"Builder {builder_id} status: {builder.status}, waiting...")
        finally:
            db.close()
        time.sleep(5)
    raise TimeoutError(f"Builder {builder_id} did not reach running status within {timeout}s")


async def wait_for_builder_stopped(builder_id, timeout=300):
    """Wait for builder to reach 'stopped' status."""
    start = time.time()
    while time.time() - start < timeout:
        # Use a fresh session for each poll to avoid stale SQLAlchemy session cache
        db = SessionLocal()
        try:
            builder = db.get(BuilderEnvironment, builder_id)
            if builder:
                if builder.status == "stopped":
                    logger.info(f"Builder {builder_id} is now stopped")
                    return True
                if builder.status == "failed":
                    raise RuntimeError(f"Builder {builder_id} failed during stop: {builder.error_message}")
                logger.info(f"Builder {builder_id} status: {builder.status}, waiting...")
        finally:
            db.close()
        await asyncio.sleep(5)
    raise TimeoutError(f"Builder {builder_id} did not reach stopped status within {timeout}s")


def verify_aws_resources(builder_id, instance_id, security_group_id):
    """Verify AWS resources state after stop."""
    import boto3
    
    ec2 = boto3.client("ec2", region_name="eu-north-1")
    
    # Check EC2 terminated
    try:
        ec2_resp = ec2.describe_instances(InstanceIds=[instance_id])
        instance = ec2_resp["Reservations"][0]["Instances"][0]
        state = instance["State"]["Name"]
        if state != "terminated":
            raise AssertionError(f"EC2 {instance_id} not terminated, state: {state}")
        logger.info(f"✅ EC2 {instance_id} terminated")
    except ec2.exceptions.ClientError as e:
        if "InvalidInstanceID.NotFound" in str(e):
            logger.info(f"✅ EC2 {instance_id} not found (terminated)")
        else:
            raise
    
    # Check security group deleted
    try:
        ec2.describe_security_groups(GroupIds=[security_group_id])
        raise AssertionError(f"Security group {security_group_id} still exists")
    except ec2.exceptions.ClientError as e:
        if "InvalidGroup.NotFound" in str(e):
            logger.info(f"✅ Security group {security_group_id} deleted")
        else:
            raise
    
    # Check S3 state exists
    s3 = boto3.client("s3", region_name="eu-north-1")
    try:
        s3.head_object(Bucket="secforge-terraform-state", Key=f"builders/{builder_id}/terraform.tfstate")
        logger.info(f"✅ S3 state object exists for builder {builder_id}")
    except Exception as e:
        raise AssertionError(f"S3 state missing: {e}")


def verify_no_unrelated_resources(builder_id):
    """Verify no unrelated AWS resources were touched."""
    import boto3
    
    ec2 = boto3.client("ec2", region_name="eu-north-1")
    
    # Check no other SecForge builder instances exist in running/pending states
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": ["SecForge"]},
            {"Name": "tag:ManagedBy", "Values": ["Terraform"]},
            {"Name": "tag:ResourceType", "Values": ["builder"]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]}
        ]
    )
    
    other_instances = []
    for res in resp["Reservations"]:
        for inst in res["Instances"]:
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            bid = tags.get("BuilderId", "")
            if bid != f"bld-{builder_id}":
                other_instances.append((inst["InstanceId"], bid, inst["State"]["Name"]))
    
    if other_instances:
        logger.warning(f"⚠️ Other builder instances found: {other_instances}")
    else:
        logger.info(f"✅ No unrelated builder instances running")


async def main_async():
    logger.info("=" * 60)
    logger.info("Starting SecForge Builder Expiration Integration Test")
    logger.info("=" * 60)
    
    # 1. Check environment
    interval = os.environ.get("BUILDER_RECONCILIATION_INTERVAL_SECONDS", "300")
    logger.info(f"Reconciliation interval: {interval}s")
    
    # 2. Create builder through normal application flow
    db = SessionLocal()
    lab_manager = LabManager()
    
    try:
        # Find a suitable draft room for creator 2
        room = db.query(Room).filter(
            Room.creator_id == 2,
            Room.status == "draft"
        ).first()
        
        if not room:
            raise RuntimeError("No suitable draft room found for creator 2")
        
        logger.info(f"Using room: {room.id} ({room.name})")
        
        # Create builder with normal 2-hour TTL
        expires_at = datetime.utcnow() + timedelta(hours=2)
        
        builder = BuilderEnvironment(
            room_id=room.id,
            creator_id=room.creator_id,
            status="pending",
            terraform_state_path="",
            expires_at=expires_at,
        )
        
        db.add(builder)
        db.flush()
        builder_id = builder.id
        logger.info(f"Created builder {builder_id}")
        
        builder.terraform_state_path = f"/tmp/secforge-builders/builder-{builder_id}"
        db.commit()
        db.refresh(builder)
        
        # Start provisioning
        logger.info(f"Starting builder {builder_id}...")
        lab_manager.start_builder(builder)
        db.commit()
        db.refresh(builder)
        
        if builder.status != "running":
            raise RuntimeError(f"Builder failed to start: {builder.status} - {builder.error_message}")
        
        instance_id = builder.instance_id
        private_ip = builder.private_ip
        security_group_id = builder.security_group_id
        
        logger.info(f"✅ Builder {builder_id} running:")
        logger.info(f"   Instance: {instance_id}")
        logger.info(f"   Private IP: {private_ip}")
        logger.info(f"   Security Group: {security_group_id}")
        logger.info(f"   Expires at: {builder.expires_at}")
        
        # 3. Update expires_at to ~70 seconds from now
        new_expires = datetime.utcnow() + timedelta(seconds=70)
        builder.expires_at = new_expires
        db.commit()
        logger.info(f"Updated expires_at to {new_expires} (in ~70 seconds)")
        
        # 3. Start reconciliation worker
        logger.info("Starting reconciliation worker...")
        worker = ReconciliationWorker()
        worker.config["interval_seconds"] = 10  # 10 second interval for test
        worker.config["terraform_dry_run"] = False
        
        # Start the worker in the background
        worker_task = asyncio.create_task(worker.start())
        
        try:
            # Wait for expiration to be detected and stop to complete
            logger.info("Waiting for expiration detection and stop...")
            await wait_for_builder_stopped(builder_id, timeout=300)
            
            # Refresh builder state
            db.refresh(builder)
            
            # Verify final state
            logger.info("=" * 60)
            logger.info("VERIFICATION RESULTS")
            logger.info("=" * 60)
            
            # 1. DB state
            assert builder.status == "stopped", f"Status not stopped: {builder.status}"
            assert builder.instance_id is None, "instance_id should be None"
            assert builder.private_ip is None, "private_ip should be None"
            assert builder.security_group_id is None, "security_group_id should be None"
            assert builder.stopped_at is not None, "stopped_at should be populated"
            logger.info(f"✅ DB state: status={builder.status}, stopped_at={builder.stopped_at}")
            
            # 2. AWS resources
            verify_aws_resources(builder_id, instance_id, security_group_id)
            
            # 3. No unrelated resources
            verify_no_unrelated_resources(builder_id)
            
            # 4. Terraform workspace cleaned
            workspace = f"/tmp/secforge-builders/builder-{builder_id}"
            assert not os.path.exists(workspace), f"Workspace {workspace} still exists"
            logger.info(f"✅ Workspace cleaned: {workspace}")
            
            # 5. S3 state present
            import boto3
            s3 = boto3.client("s3", region_name="eu-north-1")
            resp = s3.head_object(Bucket="secforge-terraform-state", Key=f"builders/{builder_id}/terraform.tfstate")
            assert resp["ContentLength"] > 0
            logger.info(f"✅ S3 state present: builders/{builder_id}/terraform.tfstate")
            
            logger.info("=" * 60)
            logger.info("🎉 ALL VERIFICATIONS PASSED!")
            logger.info("=" * 60)
            
        finally:
            # Stop the reconciliation worker
            await worker.stop()
            
    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()