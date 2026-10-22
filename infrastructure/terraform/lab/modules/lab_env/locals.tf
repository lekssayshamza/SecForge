locals {
  ownership_tags = {
    Project       = "SecForge"
    ManagedBy     = "Terraform"
    ResourceType  = "lab"
    LabInstanceId = var.lab_instance_id
    RoomId        = var.room_id
    LearnerId     = var.learner_id
    Environment   = "learner-lab"
  }
}
