using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using GettingThingsMarkdown.Client.Shared;
using GettingThingsMarkdown.Automations;

namespace GettingThingsMarkdown.Client.Desktop;

/// <summary>A packaged, native Syncthing child process. Its identity and folder
/// encryption secrets live exclusively under the client's local profile.</summary>
public sealed class LocalSyncthingEngine : IEmbeddedSyncEngine
{
    private readonly string _home;
    private readonly string _keys;
    private HttpClient? _api;
    private Process? _process;
    private string? _apiKey;
    private FileSystemWatcher? _watcher;
    private string? _workspaceRoot;
    private readonly SemaphoreSlim _automationGate = new(1, 1);

    public LocalSyncthingEngine()
    {
        var baseDir = Path.Combine(Environment.GetFolderPath(
            Environment.SpecialFolder.LocalApplicationData), "GettingThingsMarkdown");
        _home = Path.Combine(baseDir, "syncthing");
        _keys = Path.Combine(baseDir, "keys");
        Directory.CreateDirectory(_home);
        Directory.CreateDirectory(_keys);
        RestrictPermissions(_home, true);
        RestrictPermissions(_keys, true);
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_api is not null) return;
        int port;
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        _apiKey = Convert.ToHexString(RandomNumberGenerator.GetBytes(24));
        var binary = GetBinary();
        var start = new ProcessStartInfo(binary)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = false,
            RedirectStandardError = false
        };
        start.ArgumentList.Add("serve");
        start.ArgumentList.Add("--home=" + _home);
        start.ArgumentList.Add("--no-browser");
        start.ArgumentList.Add("--gui-address=127.0.0.1:" + port);
        start.ArgumentList.Add("--gui-apikey=" + _apiKey);
        _process = Process.Start(start) ?? throw new InvalidOperationException(
            "Unable to launch embedded Syncthing.");
        _api = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}/"),
            Timeout = TimeSpan.FromSeconds(10) };
        _api.DefaultRequestHeaders.Add("X-API-Key", _apiKey);
        for (int n = 0; n < 60; n++)
        {
            ct.ThrowIfCancellationRequested();
            if (_process.HasExited)
                throw new InvalidOperationException("Embedded Syncthing exited unexpectedly.");
            try
            {
                using var response = await _api.GetAsync("rest/system/status", ct);
                if (response.IsSuccessStatusCode) return;
            }
            catch (HttpRequestException) { }
            await Task.Delay(300, ct);
        }
        throw new TimeoutException("Embedded Syncthing did not start.");
    }

    private static string GetBinary()
    {
        var overridePath = Environment.GetEnvironmentVariable("GTM_SYNCTHING_BINARY");
        if (!string.IsNullOrWhiteSpace(overridePath) && File.Exists(overridePath))
            return Path.GetFullPath(overridePath);
        var binary = RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "syncthing.exe" : "syncthing";
        var bundled = Path.Combine(AppContext.BaseDirectory, "native",
            RuntimeInformation.RuntimeIdentifier, binary);
        if (!File.Exists(bundled))
            throw new FileNotFoundException(
                "Bundled Syncthing is missing. Run packaging/linux/bundle-syncthing.sh before launching desktop.",
                bundled);
        return bundled;
    }

    public async Task<string> DeviceIdAsync(CancellationToken ct = default)
    {
        var api = _api ?? throw new InvalidOperationException("Syncthing not started.");
        using var data = await api.GetAsync("rest/system/status", ct);
        data.EnsureSuccessStatusCode();
        using var json = JsonDocument.Parse(await data.Content.ReadAsStringAsync(ct));
        return json.RootElement.GetProperty("myID").GetString()!;
    }

    public async Task ConfigureFolderAsync(string path, string folderId,
        string remoteId, string remoteAddress, CancellationToken ct = default)
    {
        var api = _api ?? throw new InvalidOperationException("Syncthing not started.");
        path = Path.GetFullPath(path);
        if (!Directory.Exists(path) || path == Path.GetPathRoot(path))
            throw new ArgumentException("Select an existing non-root directory.");
        if (string.IsNullOrWhiteSpace(remoteAddress) ||
            !remoteAddress.StartsWith("tcp://", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("A reachable tcp:// address is required. On local kind use ./dev.sh expose-sync.");

        var device = await api.GetFromJsonAsync<JsonObject>("rest/config/defaults/device", ct)
            ?? throw new InvalidOperationException("Missing Syncthing default device config.");
        device["deviceID"] = remoteId;
        device["name"] = "GTMD encrypted server";
        device["untrusted"] = true;
        device["addresses"] = new JsonArray(remoteAddress);
        using (var response = await api.PostAsJsonAsync("rest/config/devices", device, ct))
        {
            if (!response.IsSuccessStatusCode && response.StatusCode != HttpStatusCode.Conflict)
                response.EnsureSuccessStatusCode();
        }

        var folder = await api.GetFromJsonAsync<JsonObject>("rest/config/defaults/folder", ct)
            ?? throw new InvalidOperationException("Missing Syncthing default folder config.");
        folder["id"] = folderId;
        folder["label"] = "GettingThingsMarkdown";
        folder["path"] = path;
        folder["type"] = "sendreceive";
        var key = await GetOrCreateKeyAsync(folderId, ct);
        var devices = folder["devices"] as JsonArray ?? new JsonArray();
        var myId = await DeviceIdAsync(ct);
        if (!devices.Any(d => d?["deviceID"]?.ToString() == myId))
            devices.Add(new JsonObject { ["deviceID"] = myId });
        if (!devices.Any(d => d?["deviceID"]?.ToString() == remoteId))
            devices.Add(new JsonObject {
                ["deviceID"] = remoteId, ["encryptionPassword"] = key
            });
        if (folder["devices"] is null) folder["devices"] = devices;
        using var result = await api.PutAsJsonAsync("rest/config/folders/" + Uri.EscapeDataString(folderId),
            folder, ct);
        result.EnsureSuccessStatusCode();
        await StartAutomationWatcherAsync(path, ct);
    }

    private async Task StartAutomationWatcherAsync(string workspace, CancellationToken ct)
    {
        _watcher?.Dispose();
        _workspaceRoot = workspace;
        var todo = Path.Combine(workspace, "ToDo");
        Directory.CreateDirectory(todo);
        foreach (var path in Directory.EnumerateFiles(todo, "*.md", SearchOption.AllDirectories))
            await ProcessAutomationAsync(path, ct);
        _watcher = new FileSystemWatcher(todo, "*.md")
        {
            IncludeSubdirectories = true,
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite |
                           NotifyFilters.CreationTime,
            EnableRaisingEvents = true
        };
        _watcher.Created += (_, e) => _ = ProcessAutomationAsync(e.FullPath, CancellationToken.None);
        _watcher.Changed += (_, e) => _ = ProcessAutomationAsync(e.FullPath, CancellationToken.None);
        _watcher.Renamed += (_, e) => _ = ProcessAutomationAsync(e.FullPath, CancellationToken.None);
    }

    private async Task ProcessAutomationAsync(string path, CancellationToken ct)
    {
        if (_workspaceRoot is null) return;
        await _automationGate.WaitAsync(ct);
        try { await ArchiveCompletedTasks.ApplyAsync(_workspaceRoot, path, ct); }
        catch (IOException) { /* File may be in transit; next watcher event retries. */ }
        finally { _automationGate.Release(); }
    }

    public async Task<string> StatusAsync(string folderId, CancellationToken ct = default)
    {
        var api = _api ?? throw new InvalidOperationException("Syncthing not started.");
        using var r = await api.GetAsync(
            "rest/db/status?folder=" + Uri.EscapeDataString(folderId), ct);
        r.EnsureSuccessStatusCode();
        using var json = JsonDocument.Parse(await r.Content.ReadAsStringAsync(ct));
        return json.RootElement.TryGetProperty("state", out var state)
            ? state.GetString() ?? "unknown" : "unknown";
    }

    public async Task<string?> ExportRecoverySecretAsync(string folderId,
        CancellationToken ct = default)
    {
        var file = Path.Combine(_keys, folderId + ".txt");
        return File.Exists(file) ? await File.ReadAllTextAsync(file, ct) : null;
    }

    public async Task ImportRecoverySecretAsync(string folderId, string key,
        CancellationToken ct = default)
    {
        if (folderId != "notes") throw new ArgumentException("Unsupported folder ID.");
        string normalized = key.Trim();
        byte[] raw;
        try { raw = Convert.FromBase64String(normalized); }
        catch (FormatException) { throw new ArgumentException("Invalid recovery secret."); }
        if (raw.Length != 32) throw new ArgumentException("Recovery secret must decode to exactly 32 bytes.");
        var file = Path.Combine(_keys, folderId + ".txt");
        if (File.Exists(file))
        {
            string existing = (await File.ReadAllTextAsync(file, ct)).Trim();
            if (!CryptographicOperations.FixedTimeEquals(
                    SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(existing)),
                    SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(normalized))))
                throw new InvalidOperationException(
                    "This device already has a different folder key; changing it would make existing encrypted files inaccessible.");
            return;
        }
        await using (var stream = new FileStream(file, FileMode.CreateNew, FileAccess.Write,
            FileShare.None, 4096, FileOptions.Asynchronous))
        {
            await using var writer = new StreamWriter(stream);
            await writer.WriteAsync(normalized.AsMemory(), ct);
        }
        RestrictPermissions(file, false);
    }

    private async Task<string> GetOrCreateKeyAsync(string folderId, CancellationToken ct)
    {
        if (folderId != "notes") throw new ArgumentException("Unsupported folder ID.");
        var file = Path.Combine(_keys, folderId + ".txt");
        if (File.Exists(file)) return await File.ReadAllTextAsync(file, ct);
        var key = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32));
        await using (var stream = new FileStream(file, FileMode.CreateNew, FileAccess.Write,
            FileShare.None, 4096, FileOptions.Asynchronous))
        {
            await using var writer = new StreamWriter(stream);
            await writer.WriteAsync(key.AsMemory(), ct);
        }
        RestrictPermissions(file, false);
        return key;
    }

    private static void RestrictPermissions(string path, bool directory)
    {
        if (OperatingSystem.IsLinux() || OperatingSystem.IsMacOS())
            File.SetUnixFileMode(path, directory
                ? UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
                : UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }

    /// <summary>Restore a bounded encrypted Git snapshot entirely on a trusted
    /// desktop. The API returns ciphertext and no decryption secret.</summary>
    public async Task<string> RestoreSnapshotAsync(AccountApi api, string revisionSha,
        string destinationParent, CancellationToken ct = default)
    {
        if (!(revisionSha.Length is 40 or 64) ||
            revisionSha.Any(ch => !Uri.IsHexDigit(ch) || char.IsUpper(ch)))
            throw new ArgumentException("Invalid Git revision.");
        if (!Directory.Exists(destinationParent))
            throw new DirectoryNotFoundException(destinationParent);
        var secret = await ExportRecoverySecretAsync("notes", ct);
        if (string.IsNullOrWhiteSpace(secret))
            throw new InvalidOperationException("Import the local E2EE recovery secret before restoring.");

        var root = Path.Combine(Path.GetTempPath(), "gtm-encrypted-" + Guid.NewGuid().ToString("N"));
        var outDir = Path.Combine(Path.GetFullPath(destinationParent),
            "GTMD-restored-" + revisionSha[..8] + "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(root);
        RestrictPermissions(root, true);
        bool complete = false;
        try
        {
            using var tree = JsonDocument.Parse(await api.EncryptedHistoryJsonAsync(
                "tree/" + revisionSha, ct));
            var json = tree.RootElement;
            if (json.TryGetProperty("truncated", out var truncated) &&
                truncated.ValueKind == JsonValueKind.True)
                throw new InvalidOperationException("Historical tree is truncated.");
            var entries = json.GetProperty("tree").EnumerateArray()
                .Where(x => x.GetProperty("type").GetString() == "blob").ToArray();
            if (entries.Length == 0 || entries.Length > 4096)
                throw new InvalidOperationException("Unsupported history size (file count).");
            long transferred = 0;
            foreach (var entry in entries)
            {
                ct.ThrowIfCancellationRequested();
                string relative = entry.GetProperty("path").GetString() ??
                    throw new InvalidOperationException("Missing encrypted filename.");
                if (!ValidEncryptedRelativePath(relative))
                    throw new InvalidOperationException("Unsafe filename in encrypted Git history.");
                string blobSha = entry.GetProperty("sha").GetString() ??
                    throw new InvalidOperationException("Missing Git object ID.");
                if (!(blobSha.Length is 40 or 64) || blobSha.Any(ch => !Uri.IsHexDigit(ch)))
                    throw new InvalidOperationException("Invalid Git blob SHA.");
                using var data = JsonDocument.Parse(await api.EncryptedHistoryJsonAsync(
                    "blob/" + blobSha, ct));
                string content = data.RootElement.GetProperty("content").GetString() ??
                    throw new InvalidOperationException("Missing encrypted blob content.");
                var bytes = Convert.FromBase64String(content);
                transferred += bytes.Length;
                if (transferred > 128L * 1024 * 1024)
                    throw new InvalidOperationException("History restore is limited to 128 MiB of ciphertext.");
                string path = Path.GetFullPath(Path.Combine(root, relative));
                if (!path.StartsWith(root + Path.DirectorySeparatorChar,
                    StringComparison.Ordinal))
                    throw new InvalidOperationException("Encrypted history path traversal.");
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                await File.WriteAllBytesAsync(path, bytes, ct);
            }

            Directory.CreateDirectory(outDir);
            RestrictPermissions(outDir, true);
            var psi = new ProcessStartInfo(GetBinary())
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
                RedirectStandardOutput = true
            };
            // Avoid exposing the secret in process arguments, logs, Git or URLs.
            psi.Environment["FOLDER_PASSWORD"] = secret.Trim();
            psi.ArgumentList.Add("decrypt");
            psi.ArgumentList.Add("--folder-id=notes");
            psi.ArgumentList.Add("--to=" + outDir);
            psi.ArgumentList.Add(root);
            using var process = Process.Start(psi) ??
                throw new InvalidOperationException("Cannot start bundled Syncthing decrypt.");
            var stderr = process.StandardError.ReadToEndAsync(ct);
            var stdout = process.StandardOutput.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);
            await Task.WhenAll(stderr, stdout);
            if (process.ExitCode != 0)
                throw new InvalidOperationException("Encrypted history decryption failed: " +
                    (await stderr)[..Math.Min((await stderr).Length, 500)]);
            complete = true;
            return outDir;
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, recursive: true);
            if (!complete && Directory.Exists(outDir))
                Directory.Delete(outDir, recursive: true);
        }
    }

    private static bool ValidEncryptedRelativePath(string path) =>
        !string.IsNullOrWhiteSpace(path) &&
        !Path.IsPathRooted(path) &&
        !path.Contains('\\') &&
        !path.Any(char.IsControl) &&
        path.Split('/').All(part => part.Length > 0 && part is not "." and not ".." and not ".git");

    // The mailbox RSA secret is separate from the Syncthing symmetric folder
    // secret. No server can derive one from the other.
    public Task<string> EnsureMailPublicKeyAsync(Guid accountId, CancellationToken ct = default) =>
        TrustedMailImporter.EnsurePublicKeyAsync(_keys, accountId, ct);

    public Task<int> ImportMailTasksAsync(AccountApi api, Guid accountId,
        string workspaceRoot, CancellationToken ct = default) =>
        TrustedMailImporter.ImportAsync(_keys, accountId, api, workspaceRoot, ct);

    public Task<int> RestoreArchivedMailTasksAsync(AccountApi api, Guid accountId,
        string workspaceRoot, CancellationToken ct = default) =>
        TrustedMailImporter.RestoreArchiveAsync(_keys, accountId, api, workspaceRoot, ct);

    public Task<string?> ExportMailPrivateKeyAsync(Guid accountId,
        CancellationToken ct = default) =>
        TrustedMailImporter.ExportPrivateKeyAsync(_keys, accountId, ct);

    public Task ImportMailPrivateKeyAsync(Guid accountId, string privateKeyPem,
        CancellationToken ct = default) =>
        TrustedMailImporter.ImportPrivateKeyAsync(_keys, accountId, privateKeyPem, ct);

    public async ValueTask DisposeAsync()
    {
        _watcher?.Dispose();
        _api?.Dispose();
        if (_process is not null)
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
                await _process.WaitForExitAsync();
            }
            _process.Dispose();
        }
    }
}
