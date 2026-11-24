provider "scaleway" {
  project_id = var.project_id
  region     = var.region
  zone       = var.zone_a
}

data "scaleway_k8s_version" "latest" {
  name = "latest"
}

resource "scaleway_vpc_private_network" "gtm" {
  name = "${var.name}-private-network"
}

resource "scaleway_vpc_public_gateway" "egress" {
  name = "${var.name}-egress"
  type = "VPC-GW-S"
}

resource "scaleway_vpc_gateway_network" "egress" {
  gateway_id         = scaleway_vpc_public_gateway.egress.id
  private_network_id = scaleway_vpc_private_network.gtm.id
  enable_masquerade  = true
  ipam_config {
    push_default_route = true
  }
}

resource "scaleway_k8s_cluster" "gtm" {
  name                        = var.name
  type                        = "kapsule"
  version                     = data.scaleway_k8s_version.latest.name
  cni                         = "cilium"
  private_network_id          = scaleway_vpc_private_network.gtm.id
  delete_additional_resources = false
}

resource "scaleway_k8s_pool" "zone_a" {
  cluster_id         = scaleway_k8s_cluster.gtm.id
  name               = "${var.name}-a"
  node_type          = var.node_type
  zone               = var.zone_a
  size               = 1
  min_size           = 1
  max_size           = var.max_nodes_per_zone
  autoscaling        = true
  autohealing        = true
  public_ip_disabled = true

  depends_on = [scaleway_vpc_gateway_network.egress]
}

resource "scaleway_k8s_pool" "zone_b" {
  cluster_id         = scaleway_k8s_cluster.gtm.id
  name               = "${var.name}-b"
  node_type          = var.node_type
  zone               = var.zone_b
  size               = 1
  min_size           = 1
  max_size           = var.max_nodes_per_zone
  autoscaling        = true
  autohealing        = true
  public_ip_disabled = true

  depends_on = [scaleway_vpc_gateway_network.egress]
}

# No public Kubernetes API access after bootstrap. Deploy automation from a
# private network using a dedicated scoped identity, not owner kubeconfig.
resource "scaleway_k8s_acl" "gtm" {
  cluster_id    = scaleway_k8s_cluster.gtm.id
  no_ip_allowed = true
}
