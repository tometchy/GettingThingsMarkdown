using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace GettingThingsMarkdown.Automations;

public sealed record AutomationResult(bool Moved, string? Destination, bool Conflict);

/// <summary>Idempotent local-only Markdown automation. Never runs on the
/// encrypted, untrusted server. A source is moved only when YAML frontmatter
/// has a scalar top-level status: done.</summary>
public static partial class ArchiveCompletedTasks
{
    [GeneratedRegex(@"^status\s*:\s*['""]?done['""]?\s*(?:#.*)?$", RegexOptions.IgnoreCase)]
    private static partial Regex DoneStatus();

    public static async Task<AutomationResult> ApplyAsync(string workspaceRoot, string source,
        CancellationToken ct = default)
    {
        var root = Path.GetFullPath(workspaceRoot);
        var todo = Path.Combine(root, "ToDo");
        var todoPrefix = Path.TrimEndingDirectorySeparator(todo) + Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(source);
        if (!full.StartsWith(todoPrefix, StringComparison.Ordinal) ||
            !full.EndsWith(".md", StringComparison.OrdinalIgnoreCase) ||
            !File.Exists(full))
            return new(false, null, false);
        // Avoid following symbolic links outside the trusted workspace.
        if ((File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
            return new(false, null, false);

        var text = await File.ReadAllTextAsync(full, ct);
        if (!IsDone(text)) return new(false, null, false);

        var relative = Path.GetRelativePath(todo, full);
        if (relative.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relative))
            return new(false, null, false);
        var target = Path.Combine(root, "Archive", relative);
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);

        if (File.Exists(target))
        {
            var old = await File.ReadAllBytesAsync(target, ct);
            var current = await File.ReadAllBytesAsync(full, ct);
            if (old.AsSpan().SequenceEqual(current))
            {
                File.Delete(full);
                return new(true, target, false);
            }

            // Deterministic conflict name prevents a second device from overwriting
            // a different note. There is no exactly-once guarantee.
            var digest = Convert.ToHexString(SHA256.HashData(current))[..12];
            target = Path.Combine(Path.GetDirectoryName(target)!,
                Path.GetFileNameWithoutExtension(target) + ".conflict-" + digest + ".md");
            if (File.Exists(target))
            {
                var existing = await File.ReadAllBytesAsync(target, ct);
                if (existing.AsSpan().SequenceEqual(current))
                {
                    File.Delete(full);
                    return new(true, target, true);
                }
                throw new IOException("Existing deterministic conflict file differs.");
            }
            File.Move(full, target);
            return new(true, target, true);
        }
        File.Move(full, target);
        return new(true, target, false);
    }

    public static bool IsDone(string markdown)
    {
        var normalized = markdown.Replace("\r\n", "\n", StringComparison.Ordinal);
        using var reader = new StringReader(normalized);
        if (reader.ReadLine() != "---") return false;
        for (int i = 0; i < 500; i++)
        {
            var line = reader.ReadLine();
            if (line is null || line == "---") return false;
            if (DoneStatus().IsMatch(line)) return true;
        }
        return false;
    }
}
