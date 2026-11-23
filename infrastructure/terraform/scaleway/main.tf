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

resource "scaleway_k8s_cluster" "gtm" {
  name                        = var.name
  type                        = "kapsule"
  version                     = data.scaleway_k8s_version.latest.name
  cni                         = "cilium"
  private_network_id          = scaleway_vpc_private_network.gtm.id
  delete_additional_resources = false
}

resource "scaleway_k8s_pool" "zone_a" {
  cluster_id  = scaleway_k8s_cluster.gtm.id
  name        = "${var.name}-a"
  node_type   = var.node_type
  zone        = var.zone_a
  size        = 1
  min_size    = 1
  max_size    = var.max_nodes_per_zone
  autoscaling = true
  autohealing = true
}

resource "scaleway_k8s_pool" "zone_b" {
  cluster_id  = scaleway_k8s_cluster.gtm.id
  name        = "${var.name}-b"
  node_type   = var.node_type
  zone        = var.zone_b
  size        = 1
  min_size    = 1
  max_size    = var.max_nodes_per_zone
  autoscaling = true
  autohealing = true
}
