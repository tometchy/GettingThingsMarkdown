namespace GettingThingsMarkdown.Client.Shared;

/// <summary>Portable contract; desktop and mobile provide native implementations.
/// The browser build can display account state but cannot spawn a Syncthing daemon.</summary>
public interface IEmbeddedSyncEngine : IAsyncDisposable
{
    Task StartAsync(CancellationToken ct = default);
    Task<string> DeviceIdAsync(CancellationToken ct = default);
    Task ConfigureFolderAsync(string path, string folderId, string remoteId,
        string remoteAddress, CancellationToken ct = default);
    Task<string> StatusAsync(string folderId, CancellationToken ct = default);
    Task<string?> ExportRecoverySecretAsync(string folderId, CancellationToken ct = default);
    Task<string> RestoreSnapshotAsync(AccountApi api, string revisionSha,
        string destinationParent, CancellationToken ct = default);
    Task ImportRecoverySecretAsync(string folderId, string key, CancellationToken ct = default);
    Task<string> EnsureMailPublicKeyAsync(Guid accountId, CancellationToken ct = default);
    Task<int> ImportMailTasksAsync(AccountApi api, Guid accountId, string workspaceRoot,
        CancellationToken ct = default);
    Task<string?> ExportMailPrivateKeyAsync(Guid accountId, CancellationToken ct = default);
    Task ImportMailPrivateKeyAsync(Guid accountId, string privateKeyPem, CancellationToken ct = default);
}

public static class ClientRuntime
{
    public static Func<IEmbeddedSyncEngine>? EmbeddedSyncEngineFactory { get; set; }
}

public record ServerPairing(string FolderId, string ServerDeviceId, string Address,
    string EncryptionMode);
