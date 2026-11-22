variable "project_id" {
  description = "Scaleway project ID. Required; no credentials are stored in state by this module."
  type        = string
}

variable "region" {
  description = "Kapsule region"
  type        = string
  default     = "fr-par"
}

variable "name" {
  description = "Name of the dedicated GettingThingsMarkdown Kapsule cluster"
  type        = string
  default     = "gtm"
}

variable "zone_a" {
  description = "Primary Kapsule node pool availability zone"
  type        = string
  default     = "fr-par-1"
}

variable "zone_b" {
  description = "Second node pool in a different availability zone"
  type        = string
  default     = "fr-par-2"
  validation {
    condition     = var.zone_b != var.zone_a
    error_message = "The two pools must occupy different availability zones."
  }
}

variable "node_type" {
  description = "Scaleway node SKU; verify regional availability and costs before apply."
  type        = string
  default     = "DEV1-M"
}

variable "max_nodes_per_zone" {
  description = "Bounded node-pool autoscaling; tenant pod replicas remain 1."
  type        = number
  default     = 5
  validation {
    condition     = var.max_nodes_per_zone >= 1 && var.max_nodes_per_zone <= 100
    error_message = "max_nodes_per_zone must be 1..100."
  }
}
