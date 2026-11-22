output "cluster_id" {
  value       = scaleway_k8s_cluster.gtm.id
  description = "Kapsule cluster ID used to download kubeconfig"
}

output "pool_ids" {
  value = {
    zone_a = scaleway_k8s_pool.zone_a.id
    zone_b = scaleway_k8s_pool.zone_b.id
  }
  description = "Autoscaled node pools in separate zones"
}

output "deployment_warning" {
  value = "DO NOT APPLY without cost review, image registry, secrets, TLS ingress, backup and quota control."
}
