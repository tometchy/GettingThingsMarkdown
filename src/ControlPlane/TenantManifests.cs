using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;

namespace GettingThingsMarkdown.ControlPlane;

public static class TenantManifests
{
    private static object Meta(string name, string? ns = null, object? labels = null)
        => new { name, @namespace = ns, labels };

    public static IEnumerable<KubeObject> Build(Guid id, string plan, int devVolumeGi,
        IDictionary<string, string>? secrets, string syncImage, string historyImage, bool lfsEnabled = false, bool wormEnabled = false,
        string? syncStorageClass = null, string? historyStorageClass = null,
        int? quotaGiBOverride = null)
    {
        var ns = TenantNames.Namespace(id);
        var labels = new Dictionary<string, string> { ["gtm.io/tenant"] = ns };
        if (!PlanCatalog.IsValid(plan)) throw new ArgumentException("Unsupported plan.", nameof(plan));
        if (quotaGiBOverride is <= 0 or > 10240)
            throw new ArgumentOutOfRangeException(nameof(quotaGiBOverride));
        string size = (devVolumeGi > 0 ? devVolumeGi :
            quotaGiBOverride ?? checked((int)(PlanCatalog.Quotas[plan] / (1024L * 1024 * 1024)))) + "Gi";
        yield return KubeObject.From("v1", "Namespace", ns, null, new
        {
            apiVersion = "v1", kind = "Namespace",
            metadata = new { name = ns, labels = new Dictionary<string, string> {
                ["gtm.io/managed"] = "true", ["gtm.io/tenant"] = ns } }
        });
        if (secrets is not null)
        {
            yield return KubeObject.From("v1", "Secret", "gtm-credentials", ns, new
            {
                apiVersion = "v1", kind = "Secret", metadata = Meta("gtm-credentials", ns),
                type = "Opaque",
                data = secrets.ToDictionary(x => x.Key,
                    x => Convert.ToBase64String(Encoding.UTF8.GetBytes(x.Value)))
            });
        }
        foreach (var role in new[] { "sync", "history" })
        {
            var storageClass = role == "sync" ? syncStorageClass : historyStorageClass;
            var volumeSpec = new Dictionary<string, object>
            {
                ["accessModes"] = new[] { "ReadWriteOnce" },
                ["resources"] = new { requests = new Dictionary<string, string> {
                    ["storage"] = lfsEnabled && role == "history"
                        ? (devVolumeGi > 0 ? "2Gi" : "8Gi")
                        : size
                } }
            };
            if (!string.IsNullOrWhiteSpace(storageClass))
                volumeSpec["storageClassName"] = storageClass;
            yield return KubeObject.From("v1", "PersistentVolumeClaim", role + "-data", ns, new
            {
                apiVersion = "v1", kind = "PersistentVolumeClaim",
                metadata = Meta(role + "-data", ns),
                spec = volumeSpec
            });
        }

        yield return KubeObject.From("v1", "Service", "sync", ns, new
        {
            apiVersion = "v1", kind = "Service", metadata = Meta("sync", ns),
            spec = new { selector = new Dictionary<string, string> {
                ["gtm.io/tenant"] = ns, ["gtm.io/role"] = "sync" },
                ports = new object[] { new { name = "api", port = 8384, targetPort = 8384 },
                    new { name = "tcp", port = 22000, targetPort = 22000 } } }
        });
        yield return KubeObject.From("v1", "Service", "history", ns, new
        {
            apiVersion = "v1", kind = "Service", metadata = Meta("history", ns),
            spec = new { selector = new Dictionary<string, string> {
                ["gtm.io/tenant"] = ns, ["gtm.io/role"] = "history" },
                ports = new[] { new { name = "https", port = 3000, targetPort = 3000 } } }
        });

        foreach (var role in new[] { "sync", "history" })
        {
            var opposite = role == "sync" ? "history" : "sync";
            var selector = new Dictionary<string, string> {
                ["gtm.io/tenant"] = ns, ["gtm.io/role"] = role
            };
            var podAnti = new
            {
                requiredDuringSchedulingIgnoredDuringExecution = new[] { new
                {
                    labelSelector = new { matchLabels = new Dictionary<string, string> {
                        ["gtm.io/tenant"] = ns, ["gtm.io/role"] = opposite } },
                    topologyKey = "kubernetes.io/hostname"
                }},
                preferredDuringSchedulingIgnoredDuringExecution = new[] { new
                {
                    weight = 100,
                    podAffinityTerm = new
                    {
                        labelSelector = new { matchLabels = new Dictionary<string, string> {
                            ["gtm.io/tenant"] = ns, ["gtm.io/role"] = opposite } },
                        topologyKey = "topology.kubernetes.io/zone"
                    }
                }}
            };
            string historyHost = $"history.{ns}.svc.cluster.local";
            object[] env = role == "sync"
                ? new object[]
                {
                    FromSecret("GTM_SYNCTHING_API_KEY", "sync-api-key"),
                    FromSecret("GTM_GIT_PASSWORD", "sync-pass"),
                    new { name = "GTM_GIT_URL", value = $"https://{historyHost}:3000/archive/history.git" },
                    new { name = "GTM_GIT_USERNAME", value = "sync" },
                    new { name = "GTM_GIT_LFS_ENABLED", value = lfsEnabled ? "true" : "false" }
                }
                : new object[]
                {
                    FromSecret("GTM_GITEA_ADMIN_PASSWORD", "admin-pass"),
                    FromSecret("GTM_GITEA_SYNC_PASSWORD", "sync-pass"),
                    FromSecret("GITEA__security__SECRET_KEY", "gitea-secret-key"),
                    FromSecret("GITEA__security__INTERNAL_TOKEN", "gitea-internal-token"),
                    FromSecret("GITEA__oauth2__JWT_SECRET", "gitea-oauth-jwt"),
                    new { name = "GITEA__server__PROTOCOL", value = "https" },
                    new { name = "GITEA__server__DOMAIN", value = historyHost },
                    new { name = "GITEA__server__ROOT_URL", value = $"https://{historyHost}:3000/" },
                    new { name = "GITEA__server__CERT_FILE", value = "/tls/tls.crt" },
                    new { name = "GITEA__server__KEY_FILE", value = "/tls/tls.key" },
                    new { name = "GITEA__security__INSTALL_LOCK", value = "true" },
                    new { name = "GITEA__service__DISABLE_REGISTRATION", value = "true" },
                    new { name = "GITEA__service__REQUIRE_SIGNIN_VIEW", value = "true" },
                    new { name = "GITEA__server__DISABLE_SSH", value = "true" },
                    new { name = "GITEA__repository__FORCE_PRIVATE", value = "true" },
                    new { name = "GITEA__database__DB_TYPE", value = "sqlite3" },
                    new { name = "GITEA__actions__ENABLED", value = "false" }
                };
            var initContainers = role == "sync" ? new object[] { new {
                name = "prepare-volume", image = "busybox:1.37",
                command = new[] { "/bin/sh", "-ec",
                    "mkdir -p /data/files /data/git /data/syncthing && chown 1000:1000 /data /data/files /data/git /data/syncthing && chmod 0770 /data/files /data/git /data/syncthing" },
                securityContext = new {
                    runAsUser = 0, runAsNonRoot = false, allowPrivilegeEscalation = false,
                    capabilities = new { drop = new[] { "ALL" }, add = new[] { "CHOWN", "DAC_OVERRIDE", "FOWNER" } }
                },
                volumeMounts = new[] { new { name = "data", mountPath = "/data" } }
            } } : Array.Empty<object>();
            if (lfsEnabled && role == "history")
            {
                env = env.Concat(new object[]
                {
                    new { name = "GITEA__server__LFS_START_SERVER", value = "true" },
                    new { name = "GITEA__storage__STORAGE_TYPE", value = "minio" },
                    FromLfsSecret("GITEA__storage__MINIO_ENDPOINT", "endpoint"),
                    FromLfsSecret("GITEA__storage__MINIO_ACCESS_KEY_ID", "access-key-id"),
                    FromLfsSecret("GITEA__storage__MINIO_SECRET_ACCESS_KEY", "secret-access-key"),
                    FromLfsSecret("GITEA__storage__MINIO_BUCKET", "bucket"),
                    FromLfsSecret("GITEA__storage__MINIO_LOCATION", "location"),
                    new { name = "GITEA__storage__MINIO_USE_SSL", value = "true" },
                    new { name = "GITEA__storage__MINIO_INSECURE_SKIP_VERIFY", value = "false" },
                    new { name = "GITEA__storage__MINIO_BUCKET_LOOKUP_TYPE", value = "path" },
                    new { name = "GITEA__storage__MINIO_BASE_PATH", value = $"tenants/{ns}/" },
                    new { name = "GITEA__lfs__MINIO_BASE_PATH", value = $"tenants/{ns}/lfs/" },
                    new { name = "GTM_LFS_PREFIX", value = $"tenants/{ns}/lfs" }
                }).ToArray();
            }
            if (wormEnabled && lfsEnabled && role == "history")
            {
                env = env.Concat(new object[]
                {
                    new { name = "GTM_WORM_BACKUP_ENABLED", value = "true" },
                    new { name = "GTM_TENANT_NAMESPACE", value = ns },
                    FromWormSecret("GTM_WORM_ENDPOINT", "endpoint-url"),
                    FromWormSecret("GTM_WORM_BUCKET", "bucket"),
                    FromWormSecret("GTM_WORM_REGION", "region"),
                    FromWormSecret("GTM_WORM_ACCESS_KEY_ID", "access-key-id"),
                    FromWormSecret("GTM_WORM_SECRET_ACCESS_KEY", "secret-access-key"),
                    FromWormSecret("GTM_WORM_BACKUP_PASSPHRASE", "backup-passphrase")
                }).ToArray();
            }
            var mounts = role == "sync"
                ? new object[] {
                    new { name = "data", mountPath = "/data" },
                    new { name = "history-ca", mountPath = "/tls", readOnly = true }
                }
                : new object[] {
                    new { name = "data", mountPath = "/var/lib/gitea" },
                    new { name = "gitea-config", mountPath = "/etc/gitea" },
                    new { name = "history-tls", mountPath = "/tls", readOnly = true }
                };
            var volumes = role == "sync"
                ? new object[] {
                    new { name = "data", persistentVolumeClaim = new { claimName = "sync-data" } },
                    new { name = "history-ca", secret = new { secretName = "history-ca" } }
                }
                : new object[] {
                    new { name = "data", persistentVolumeClaim = new { claimName = "history-data" } },
                    new { name = "gitea-config", emptyDir = new { } },
                    new { name = "history-tls", secret = new { secretName = "history-tls" } }
                };
            yield return KubeObject.From("apps/v1", "StatefulSet", role, ns, new
            {
                apiVersion = "apps/v1", kind = "StatefulSet",
                metadata = Meta(role, ns, selector),
                spec = new
                {
                    serviceName = role, replicas = 1, selector = new { matchLabels = selector },
                    template = new
                    {
                        metadata = new { labels = selector },
                        spec = new
                        {
                            securityContext = new { runAsNonRoot = true, runAsUser = 1000, runAsGroup = 1000, fsGroup = 1000 },
                            affinity = new { podAntiAffinity = podAnti },
                            topologySpreadConstraints = new[] { new {
                                maxSkew = 1,
                                topologyKey = "topology.kubernetes.io/zone",
                                whenUnsatisfiable = "ScheduleAnyway",
                                labelSelector = new { matchLabels = new Dictionary<string,string> {
                                    ["gtm.io/tenant"] = ns
                                } }
                            } },
                            initContainers,
                            containers = new[] { new
                            {
                                name = role, image = role == "sync" ? syncImage : historyImage,
                                imagePullPolicy = "IfNotPresent",
                                env, volumeMounts = mounts,
                                resources = new { requests = new { cpu = "100m", memory = "256Mi" },
                                    limits = new { cpu = "1", memory = "1Gi" } },
                                ports = role == "sync"
                                    ? new[] { new { name = "api", containerPort = 8384 }, new { name = "tcp", containerPort = 22000 } }
                                    : new[] { new { name = "https", containerPort = 3000 } },
                                readinessProbe = role == "sync"
                                    ? new { tcpSocket = (object)new { port = 8384 }, initialDelaySeconds = 15, periodSeconds = 10 }
                                    : new { tcpSocket = (object)new { port = 3000 }, initialDelaySeconds = 25, periodSeconds = 10 }
                            }},
                            volumes
                        }
                    }
                }
            });
        }

        // Narrow the Syncthing administration API to the shared management namespace.
        // P2P uses port 22000. Git history is reachable only from A and the control plane.
        yield return KubeObject.From("networking.k8s.io/v1", "NetworkPolicy", "restrict-tenant", ns, new
        {
            apiVersion = "networking.k8s.io/v1", kind = "NetworkPolicy",
            metadata = Meta("restrict-tenant", ns),
            spec = new
            {
                podSelector = new { }, policyTypes = new[] { "Ingress" },
                ingress = new object[] {
                    new {
                        from = new object[] { new { namespaceSelector = new { matchLabels =
                            new Dictionary<string, string> { ["kubernetes.io/metadata.name"] = "gtm-system" } } } },
                        ports = new object[] { new { protocol = "TCP", port = 8384 }, new { protocol = "TCP", port = 3000 } }
                    },
                    new {
                        from = new object[] { new { podSelector = new { matchLabels =
                            new Dictionary<string,string> { ["gtm.io/role"] = "sync" } } } },
                        ports = new object[] { new { protocol = "TCP", port = 3000 } }
                    },
                    new {
                        // Omitted source selector admits external Syncthing peers on 22000.
                        ports = new object[] { new { protocol = "TCP", port = 22000 } }
                    }
                }
            }
        });
    }

    private static object FromSecret(string name, string key) => new
    {
        name, valueFrom = new { secretKeyRef = new { name = "gtm-credentials", key } }
    };

    private static object FromWormSecret(string name, string key) => new
    {
        name, valueFrom = new { secretKeyRef = new { name = "gtm-worm-credentials", key } }
    };

    private static object FromLfsSecret(string name, string key) => new
    {
        name, valueFrom = new { secretKeyRef = new { name = "gtm-lfs-credentials", key } }
    };

    public static Dictionary<string, string> NewSecrets()
        => new()
        {
            ["admin-pass"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(24)),
            ["sync-pass"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(24)),
            ["sync-api-key"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(24)),
            ["gitea-secret-key"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)),
            ["gitea-internal-token"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)),
            ["gitea-oauth-jwt"] = Convert.ToHexString(RandomNumberGenerator.GetBytes(32))
        };
}
