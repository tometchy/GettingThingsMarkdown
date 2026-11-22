using System.Text;

namespace GettingThingsMarkdown.Automations;

/// <summary>Local-only, path-safe Markdown editing. The storage root must be a
/// directory chosen by the user, and is never accessed by the SaaS operator.</summary>
public sealed class MarkdownWorkspace
{
    public string Root { get; }

    public MarkdownWorkspace(string root)
    {
        Root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        if (!Directory.Exists(Root)) throw new DirectoryNotFoundException(Root);
        EnsureNotSymlink(Root);
    }

    public IReadOnlyList<string> List()
    {
        return Directory.EnumerateFiles(Root, "*.md", SearchOption.AllDirectories)
            .Select(Path.GetFullPath)
            .Where(path => !HasSymlinkComponent(path))
            .Select(path => Path.GetRelativePath(Root, path).Replace('\\', '/'))
            .OrderBy(name => name, StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }

    public async Task<string> ReadAsync(string relative, CancellationToken ct = default)
        => await File.ReadAllTextAsync(Resolve(relative), ct);

    public async Task SaveAsync(string relative, string content, CancellationToken ct = default)
    {
        string path = Resolve(relative);
        string folder = Path.GetDirectoryName(path)!;
        Directory.CreateDirectory(folder);
        EnsureAllParentsNotSymlinks(folder);

        // Write temp in the same folder for atomic rename. No Syncthing/watchers
        // will observe a partially written markdown file.
        string temp = Path.Combine(folder, "." + Guid.NewGuid().ToString("N") + ".gtm-tmp");
        try
        {
            await File.WriteAllTextAsync(temp, content, new UTF8Encoding(false), ct);
            File.Move(temp, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temp)) File.Delete(temp);
        }
    }

    public string Resolve(string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative) ||
            relative.Contains('\\') || relative.Split('/').Any(p => p is "." or ".." or ""))
            throw new ArgumentException("Unsafe relative path.");
        if (!relative.EndsWith(".md", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Only Markdown files are allowed.");
        string full = Path.GetFullPath(Path.Combine(Root, relative));
        string prefix = Root + Path.DirectorySeparatorChar;
        if (!full.StartsWith(prefix, StringComparison.Ordinal))
            throw new ArgumentException("Path escaped workspace.");
        EnsureAllParentsNotSymlinks(Path.GetDirectoryName(full)!);
        if (File.Exists(full)) EnsureNotSymlink(full);
        return full;
    }

    private bool HasSymlinkComponent(string path)
    {
        try
        {
            EnsureAllParentsNotSymlinks(path);
            EnsureNotSymlink(path);
            return false;
        }
        catch (IOException) { return true; }
    }

    private void EnsureAllParentsNotSymlinks(string path)
    {
        var current = Root;
        EnsureNotSymlink(current);
        var relative = Path.GetRelativePath(Root, path);
        if (relative == ".") return;
        if (relative.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relative))
            throw new IOException("Path traversal detected.");
        foreach (var part in relative.Split(Path.DirectorySeparatorChar))
        {
            current = Path.Combine(current, part);
            if (File.Exists(current) || Directory.Exists(current))
                EnsureNotSymlink(current);
        }
    }

    private static void EnsureNotSymlink(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Symbolic links are not followed.");
    }
}
