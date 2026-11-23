namespace GettingThingsMarkdown.Automations;

/// <summary>Produces inert UTF-8 Markdown from an already decrypted email.
/// Runs only on the customer's trusted machine.</summary>
public static class MailTaskFormatter
{
    public static string Render(string subject, string sender, string body, DateTimeOffset received)
    {
        // Never allow SMTP headers to inject frontmatter fields or Markdown
        // headings. All untrusted strings are rendered as plain content.
        static string Inline(string value) => value
            .Replace("\r", " ", StringComparison.Ordinal)
            .Replace("\n", " ", StringComparison.Ordinal)
            .Replace("\\", "\\\\", StringComparison.Ordinal)
            .Replace("*", "\\*", StringComparison.Ordinal)
            .Replace("_", "\\_", StringComparison.Ordinal)
            .Replace("[", "\\[", StringComparison.Ordinal)
            .Replace("]", "\\]", StringComparison.Ordinal)
            .Replace("<", "&lt;", StringComparison.Ordinal)
            .Replace(">", "&gt;", StringComparison.Ordinal)
            .Trim();
        return "---\nstatus: todo\nsource: email\nreceived: " +
               received.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'",
                   System.Globalization.CultureInfo.InvariantCulture) +
               "\n---\n\n# " + Inline(subject) + "\n\n**From:** " + Inline(sender) +
               "\n\n" + body.Trim() + "\n";
    }

    public static string FileName(Guid mailId) => "ToDo/mail-" + mailId.ToString("N") + ".md";
}
