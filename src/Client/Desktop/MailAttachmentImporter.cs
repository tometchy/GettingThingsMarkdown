using GettingThingsMarkdown.Automations;
using MimeKit;
using System.Text;
namespace GettingThingsMarkdown.Client.Desktop;
public static class MailAttachmentImporter
{
    public static async Task<string> SaveAsync(MarkdownWorkspace workspace, Guid id,
        string? rawMimeBase64, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(rawMimeBase64)) return "";
        await using var stream = new MemoryStream(Convert.FromBase64String(rawMimeBase64), writable: false);
        var message = await MimeMessage.LoadAsync(stream, ct);
        var links = new StringBuilder();
        int number = 0;
        foreach (var part in message.BodyParts)
        {
            if (part is not MimePart && part is not MessagePart) continue;
            if (part is MimePart p && !p.IsAttachment && string.IsNullOrEmpty(p.FileName) &&
                string.IsNullOrEmpty(p.ContentId) && p.ContentType.MediaType.Equals("text", StringComparison.OrdinalIgnoreCase))
                continue;
            number++;
            string supplied = part is MimePart mime ? mime.FileName ?? "" : "message.eml";
            string safe = new string(supplied.Select(c =>
                char.IsAsciiLetterOrDigit(c) || c is '.' or '-' or '_' ? c : '_').Take(100).ToArray()).Trim('.');
            if (safe.Length == 0) safe = part.ContentType.MimeType.ToLowerInvariant() switch {
                "image/png" => "image.png", "image/jpeg" => "image.jpg",
                "image/gif" => "image.gif", "image/webp" => "image.webp",
                "application/pdf" => "document.pdf", _ => "attachment.bin" };
            string name = $"{number:D3}-{safe}";
            string relative = $"Attachments/mail-{id:N}/{name}";
            await using var bytes = new MemoryStream();
            if (part is MimePart item && item.Content is not null) await item.Content.DecodeToAsync(bytes, ct);
            else if (part is MessagePart nested) await nested.Message.WriteToAsync(bytes, ct);
            else continue;
            await workspace.SaveBinaryIfAbsentAsync(relative, bytes.ToArray(), ct);
            bool image = part.ContentType.MimeType.ToLowerInvariant() is
                "image/png" or "image/jpeg" or "image/gif" or "image/webp";
            links.Append(image ? "!" : "").Append('[').Append(name)
                .Append("](../").Append(relative).Append(")\n");
        }
        return links.Length == 0 ? "" : "\n## Attachments\n\n" + links;
    }
}
