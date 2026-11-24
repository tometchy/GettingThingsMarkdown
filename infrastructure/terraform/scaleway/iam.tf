# Daily operators have no standing access to instances, Kubernetes or IAM.
# This policy does not override other memberships: assign dedicated identities.
resource "scaleway_iam_group" "observability" {
  name     = "${var.name}-observability"
  user_ids = var.observability_operator_user_ids
}

resource "scaleway_iam_policy" "observability" {
  name     = "${var.name}-observability-readonly"
  group_id = scaleway_iam_group.observability.id

  rule {
    project_ids          = [var.project_id]
    permission_set_names = ["ObservabilityReadOnly"]
  }
}
