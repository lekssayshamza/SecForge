locals {
  ownership_tags = {
    Project      = "SecForge"
    ManagedBy    = "Terraform"
    ResourceType = "builder"
    BuilderId    = var.builder_id
    RoomId       = var.room_id
    CreatorId    = var.creator_id
    Environment  = "builder"
  }
}
