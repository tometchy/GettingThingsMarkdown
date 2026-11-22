using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Server-side device pairing. This class never accepts nor stores E2EE secrets.</summary>
public sealed class SyncthingTenantApi : IDisposable
{
    private readonly HttpClient _http;
    public SyncthingTenantApi(string ns, string key)
    {
        _http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:8384/"),
            Timeout = TimeSpan.FromSeconds(12) };
        _http.DefaultRequestHeaders.Add("X-API-Key", key);
    }

    public async Task<string> DeviceIdAsync(CancellationToken ct)
    {
        using var result = await _http.GetAsync("rest/system/status", ct);
        result.EnsureSuccessStatusCode();
        using var json = JsonDocument.Parse(await result.Content.ReadAsStringAsync(ct));
        return json.RootElement.GetProperty("myID").GetString()
            ?? throw new InvalidOperationException("Syncthing has no device ID.");
    }

    private static bool ValidId(string id) =>
        Regex.IsMatch(id, @"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$",
            RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));

    public async Task AddDeviceAsync(string deviceId, string? name, CancellationToken ct)
    {
        if (!ValidId(deviceId)) throw new ArgumentException("Invalid Syncthing Device ID.");
        var ownId = await DeviceIdAsync(ct);
        if (deviceId == ownId) throw new ArgumentException("Cannot pair to self.");
        // Defaults are provided by Syncthing itself, not guessed/reimplemented.
        var defaultDevice = await _http.GetFromJsonAsync<JsonObject>(
            "rest/config/defaults/device", ct) ?? throw new InvalidOperationException("No device defaults.");
        defaultDevice["deviceID"] = deviceId;
        defaultDevice["name"] = (name ?? "GTMD device")[..Math.Min((name ?? "GTMD device").Length, 80)];
        defaultDevice["untrusted"] = true;
        using (var result = await _http.PostAsJsonAsync("rest/config/devices", defaultDevice, ct))
        {
            if (!result.IsSuccessStatusCode && result.StatusCode != System.Net.HttpStatusCode.Conflict)
                result.EnsureSuccessStatusCode();
        }

        using var folderResponse = await _http.GetAsync("rest/config/folders/" + TenantNames.FolderId, ct);
        JsonObject folder;
        if (folderResponse.IsSuccessStatusCode)
            folder = JsonNode.Parse(await folderResponse.Content.ReadAsStringAsync(ct))!.AsObject();
        else if (folderResponse.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            folder = await _http.GetFromJsonAsync<JsonObject>("rest/config/defaults/folder", ct)
                ?? throw new InvalidOperationException("Missing folder defaults.");
            folder["id"] = TenantNames.FolderId;
            folder["label"] = "GettingThingsMarkdown";
            folder["path"] = "/data/files";
            folder["type"] = "receiveencrypted";
        }
        else { folderResponse.EnsureSuccessStatusCode(); return; }
        if (folder["type"]?.GetValue<string>() != "receiveencrypted")
            throw new InvalidOperationException("Server folder must remain receiveencrypted.");

        var members = folder["devices"] as JsonArray ?? new JsonArray();
        if (!members.Any(x => x?["deviceID"]?.ToString() == deviceId))
            members.Add(new JsonObject { ["deviceID"] = deviceId });
        folder["devices"] = members;

        using var save = await _http.PutAsJsonAsync(
            "rest/config/folders/" + TenantNames.FolderId, folder, ct);
        save.EnsureSuccessStatusCode();
    }

    public void Dispose() => _http.Dispose();
}

public record PairResponse(string FolderId, string ServerDeviceId, string Address,
    string EncryptionMode);
