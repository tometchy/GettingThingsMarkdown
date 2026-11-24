using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using GettingThingsMarkdown.Automations;
using GettingThingsMarkdown.Client.Shared;

namespace GettingThingsMarkdown.Client.Desktop;

/// <summary>
/// Asymmetric mailbox private keys never leave a trusted device. The SMTP
/// server only sees the corresponding RSA SPKI public key. This importer turns
/// encrypted envelopes into ordinary local .md files; Syncthing then performs
/// its normal untrusted-device E2EE upload.
/// </summary>
public static class TrustedMailImporter
{
    private static readonly byte[] DomainSeparator = "GTM-INBOX-V1"u8.ToArray();

    private static string KeyPath(string keyDir, Guid owner) =>
        Path.Combine(keyDir, "mail-" + owner.ToString("N") + ".pem");

    public static async Task<string> EnsurePublicKeyAsync(
        string keyDir, Guid accountId, CancellationToken ct = default)
    {
        var path = KeyPath(keyDir, accountId);
        if (!File.Exists(path))
        {
            Directory.CreateDirectory(keyDir);
            using var newKey = RSA.Create(3072);
            try
            {
                await using var file = new FileStream(path, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous);
                await using var writer = new StreamWriter(file, new UTF8Encoding(false));
                await writer.WriteAsync(newKey.ExportPkcs8PrivateKeyPem().AsMemory(), ct);
            }
            catch (IOException) when (File.Exists(path))
            {
                // Another trusted instance created the key first; never overwrite it.
            }
            RestrictPermissions(path);
        }
        using var existing = RSA.Create();
        existing.ImportFromPem(await File.ReadAllTextAsync(path, ct));
        if (existing.KeySize is < 3072 or > 4096)
            throw new CryptographicException("Unexpected email private key size.");
        return Convert.ToBase64String(existing.ExportSubjectPublicKeyInfo());
    }

    public static Task<string?> ExportPrivateKeyAsync(string keyDir, Guid accountId,
        CancellationToken ct = default)
    {
        var path = KeyPath(keyDir, accountId);
        return File.Exists(path) ? File.ReadAllTextAsync(path, ct).ContinueWith<string?>(
            t => t.GetAwaiter().GetResult(), ct) : Task.FromResult<string?>(null);
    }

    public static async Task ImportPrivateKeyAsync(string keyDir, Guid accountId,
        string privateKeyPem, CancellationToken ct = default)
    {
        using var rsa = RSA.Create();
        rsa.ImportFromPem(privateKeyPem);
        if (rsa.KeySize is < 3072 or > 4096)
            throw new CryptographicException("RSA private key must be >=3072 bits.");
        // Force public/private key pair validation before saving.
        _ = rsa.ExportPkcs8PrivateKey();
        Directory.CreateDirectory(keyDir);
        var path = KeyPath(keyDir, accountId);
        if (File.Exists(path))
        {
            using var oldKey = RSA.Create();
            oldKey.ImportFromPem(await File.ReadAllTextAsync(path, ct));
            if (!oldKey.ExportSubjectPublicKeyInfo().AsSpan()
                    .SequenceEqual(rsa.ExportSubjectPublicKeyInfo()))
                throw new InvalidOperationException(
                    "This device already has a different email key. Refusing replacement.");
            return;
        }
        await using var stream = new FileStream(path, FileMode.CreateNew,
            FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous);
        await using (var writer = new StreamWriter(stream, new UTF8Encoding(false), leaveOpen: true))
            await writer.WriteAsync(rsa.ExportPkcs8PrivateKeyPem().AsMemory(), ct);
        RestrictPermissions(path);
    }

    public static Task<int> ImportAsync(string keyDir, Guid accountId, AccountApi api,
        string workspaceRoot, CancellationToken ct = default) =>
        ImportCoreAsync(keyDir, accountId, api, workspaceRoot, false, ct);

    public static Task<int> RestoreArchiveAsync(string keyDir, Guid accountId,
        AccountApi api, string workspaceRoot, CancellationToken ct = default) =>
        ImportCoreAsync(keyDir, accountId, api, workspaceRoot, true, ct);

    private static async Task<int> ImportCoreAsync(string keyDir, Guid accountId,
        AccountApi api, string workspaceRoot, bool archive, CancellationToken ct)
    {
        var key = KeyPath(keyDir, accountId);
        if (!File.Exists(key))
            throw new InvalidOperationException(
                "Inbox private key missing. Import from an existing trusted device.");
        using var privateKey = RSA.Create();
        privateKey.ImportFromPem(await File.ReadAllTextAsync(key, ct));
        var workspace = new MarkdownWorkspace(workspaceRoot);
        int imported = 0;
        // Page through the archive without downloading all ciphertext at once.
        // A deterministic mail ID makes retries and multi-device imports safe.
        for (int skip = 0, scanned = 0; scanned < (archive ? 10000 : 200); scanned++)
        {
            var messages = archive ? await api.MailArchivedMessagesAsync(skip, ct)
                : await api.MailMessagesAsync(skip, ct);
            foreach (var mail in messages)
            {
                ct.ThrowIfCancellationRequested();
                string relative = MailTaskFormatter.FileName(mail.Id);
                var path = workspace.Resolve(relative);
                // A new device may lack keys for old envelopes; do not ACK
                // them, and continue to messages addressed to the new key.
                MailMessagePayload message;
                try { message = Decrypt(mail, privateKey); }
                catch (CryptographicException) { skip++; continue; }
                await workspace.SaveBinaryIfAbsentAsync($".gtmd/inbox/{mail.Id:N}.json",
                    JsonSerializer.SerializeToUtf8Bytes(mail, AccountJsonContext.Default.MailEnvelopeInfo), ct);
                string receipt = $".gtmd/imported/{mail.Id:N}.receipt";
                bool completed = File.Exists(workspace.ResolveFile(receipt)) ||
                    File.Exists(workspace.Resolve("Archive/" + Path.GetFileName(relative)));
                if (!File.Exists(path) && (!completed || archive))
                {
                    string attachments = await MailAttachmentImporter.SaveAsync(
                        workspace, mail.Id, message.RawMimeBase64, ct);
                    await workspace.SaveAsync(relative, MailTaskFormatter.Render(
                        message.Subject, message.From, message.Body, message.ReceivedUtc) + attachments, ct);
                    imported++;
                }
                await workspace.SaveBinaryIfAbsentAsync(receipt, "imported"u8.ToArray(), ct);
                // ACK is never issued on archived recoveries, and on pending
                // deliveries happens only after the atomic local write.
                if (!archive) await api.AckMailAsync(mail.Id, ct);
                else skip++;
            }
            if (messages.Length == 0) break;
        }
        return imported;
    }

    public static MailMessagePayload Decrypt(MailEnvelopeInfo mail, RSA privateKey)
    {
        var wrapped = Convert.FromBase64String(mail.WrappedKey);
        var iv = Convert.FromBase64String(mail.Nonce);
        var tag = Convert.FromBase64String(mail.Tag);
        var ciphertext = Convert.FromBase64String(mail.Ciphertext);
        if (iv.Length != 12 || tag.Length != 16 || ciphertext.Length > 70 * 1024 * 1024)
            throw new CryptographicException("Invalid encrypted mail envelope.");
        byte[]? aesKey = null;
        if (wrapped.Length is >= 384 and <= 512)
            aesKey = privateKey.Decrypt(wrapped, RSAEncryptionPadding.OaepSHA256);
        else
        {
            foreach (var candidate in JsonSerializer.Deserialize<byte[][]>(wrapped) ?? [])
            {
                try { aesKey = privateKey.Decrypt(candidate, RSAEncryptionPadding.OaepSHA256); break; }
                catch (CryptographicException) { }
            }
        }
        if (aesKey is null) throw new CryptographicException("Mail was not encrypted for this device.");
        var plain = new byte[ciphertext.Length];
        try
        {
            using var aes = new AesGcm(aesKey, 16);
            aes.Decrypt(iv, ciphertext, tag, plain, DomainSeparator);
            return JsonSerializer.Deserialize(plain,
                AccountJsonContext.Default.MailMessagePayload)
                ?? throw new CryptographicException("Invalid decrypted mail.");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(plain);
            CryptographicOperations.ZeroMemory(aesKey);
        }
    }

    private static void RestrictPermissions(string path)
    {
        if (OperatingSystem.IsLinux() || OperatingSystem.IsMacOS())
            File.SetUnixFileMode(path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }
}
