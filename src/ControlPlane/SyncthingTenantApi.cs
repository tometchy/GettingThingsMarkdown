using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GettingThingsMarkdown.ControlPlane;

/// <summary>Server-side device pairing. This class never accepts nor stores E2EE secrets.</summary>
public sealed class SyncthingTenantApi : IDisposable
{
    private readonly HttpClient _http;
    private readonly bool _mailPeer;
    private string FolderId => _mailPeer ? "gtmd-mail" : TenantNames.FolderId;
    private string FolderType => _mailPeer ? "sendonly" : "receiveencrypted";
    public SyncthingTenantApi(string ns, string key, bool mailPeer = false)
    {
        _mailPeer = mailPeer;
        _http = new HttpClient { BaseAddress = new Uri($"http://sync.{ns}.svc.cluster.local:{(mailPeer ? 8386 : 8384)}/"),
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
        defaultDevice["untrusted"] = !_mailPeer;
        using (var result = await _http.PostAsJsonAsync("rest/config/devices", defaultDevice, ct))
        {
            if (!result.IsSuccessStatusCode && result.StatusCode != System.Net.HttpStatusCode.Conflict)
                result.EnsureSuccessStatusCode();
        }

        using var folderResponse = await _http.GetAsync("rest/config/folders/" + FolderId, ct);
        JsonObject folder;
        if (folderResponse.IsSuccessStatusCode)
            folder = JsonNode.Parse(await folderResponse.Content.ReadAsStringAsync(ct))!.AsObject();
        else if (folderResponse.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            folder = await _http.GetFromJsonAsync<JsonObject>("rest/config/defaults/folder", ct)
                ?? throw new InvalidOperationException("Missing folder defaults.");
            folder["id"] = FolderId;
            folder["label"] = _mailPeer ? "GTMD encrypted mail" : "GettingThingsMarkdown";
            folder["path"] = _mailPeer ? "/data/mail/.gtmd/inbox" : "/data/files";
            folder["type"] = FolderType;
        }
        else { folderResponse.EnsureSuccessStatusCode(); return; }
        if (folder["type"]?.GetValue<string>() != FolderType)
            throw new InvalidOperationException("Server folder must remain receiveencrypted.");

        var members = folder["devices"] as JsonArray ?? new JsonArray();
        if (!members.Any(x => x?["deviceID"]?.ToString() == deviceId))
            members.Add(new JsonObject { ["deviceID"] = deviceId });
        if (folder["devices"] is null) folder["devices"] = members;

        using var save = await _http.PutAsJsonAsync(
            "rest/config/folders/" + FolderId, folder, ct);
        save.EnsureSuccessStatusCode();
    }

    public async Task SetFolderPausedAsync(bool paused, CancellationToken ct)
    {
        using var response = await _http.GetAsync(
            "rest/config/folders/" + FolderId, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return;
        response.EnsureSuccessStatusCode();
        var folder = JsonNode.Parse(await response.Content.ReadAsStringAsync(ct))!.AsObject();
        if (folder["type"]?.ToString() != "receiveencrypted")
            throw new InvalidOperationException("Refusing to alter a non-encrypted folder.");
        if (folder["paused"]?.GetValue<bool>() == paused) return;
        folder["paused"] = paused;
        using var save = await _http.PutAsJsonAsync(
            "rest/config/folders/" + FolderId, folder, ct);
        save.EnsureSuccessStatusCode();
    }

    public async Task<IReadOnlyList<string>> ListPairedDeviceIdsAsync(CancellationToken ct)
    {
        var ownId = await DeviceIdAsync(ct);
        var json = await _http.GetFromJsonAsync<JsonArray>("rest/config/devices", ct)
            ?? throw new InvalidOperationException("Cannot read paired devices.");
        return json.Select(x => x?["deviceID"]?.ToString() ?? "")
            .Where(x => ValidId(x) && x != ownId)
            .Distinct(StringComparer.Ordinal)
            .ToArray();
    }

    public async Task RemoveDeviceAsync(string deviceId, CancellationToken ct)
    {
        if (!ValidId(deviceId) || deviceId == await DeviceIdAsync(ct))
            throw new ArgumentException("Invalid device ID.");

        using var folderResponse = await _http.GetAsync(
            "rest/config/folders/" + FolderId, ct);
        if (folderResponse.IsSuccessStatusCode)
        {
            var folder = JsonNode.Parse(await folderResponse.Content.ReadAsStringAsync(ct))!.AsObject();
            var devices = folder["devices"] as JsonArray ?? new JsonArray();
            for (int i = devices.Count - 1; i >= 0; i--)
            {
                if (devices[i]?["deviceID"]?.ToString() == deviceId)
                    devices.RemoveAt(i);
            }
            if (folder["devices"] is null) folder["devices"] = devices;
            using var saved = await _http.PutAsJsonAsync(
                "rest/config/folders/" + FolderId, folder, ct);
            saved.EnsureSuccessStatusCode();
        }
        using var removed = await _http.DeleteAsync("rest/config/devices/" + deviceId, ct);
        if (!removed.IsSuccessStatusCode && removed.StatusCode != System.Net.HttpStatusCode.NotFound)
            removed.EnsureSuccessStatusCode();
    }

    public void Dispose() => _http.Dispose();
}

public record PairResponse(string FolderId, string ServerDeviceId, string Address,
    string EncryptionMode, string? MailDeviceId = null, string? MailAddress = null);
