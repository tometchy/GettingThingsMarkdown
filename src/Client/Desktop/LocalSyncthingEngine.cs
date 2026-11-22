using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using GettingThingsMarkdown.Client.Shared;

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

    public async ValueTask DisposeAsync()
    {
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
