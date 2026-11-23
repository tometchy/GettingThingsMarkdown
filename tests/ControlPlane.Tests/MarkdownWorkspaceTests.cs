using GettingThingsMarkdown.Automations;

namespace GettingThingsMarkdown.Tests;

public sealed class MarkdownWorkspaceTests
{
    [Fact]
    public async Task SaveOpenListAndRejectTraversal()
    {
        var root = Path.Combine(Path.GetTempPath(), "gtm-md-" + Guid.NewGuid());
        Directory.CreateDirectory(root);
        try
        {
            var notes = new MarkdownWorkspace(root);
            await notes.SaveAsync("Notes/hello.md", "# Hello\nTest");
            Assert.Equal("# Hello\nTest", await notes.ReadAsync("Notes/hello.md"));
            Assert.Equal(new[] { "Notes/hello.md" }, notes.List());
            await notes.SaveAsync("Notes/hello.md", "# Changed");
            Assert.Equal("# Changed", await notes.ReadAsync("Notes/hello.md"));
            Assert.Throws<ArgumentException>(() => notes.Resolve("../secret.md"));
            Assert.Throws<ArgumentException>(() => notes.Resolve("/tmp/outside.md"));
            Assert.Throws<ArgumentException>(() => notes.Resolve("image.png"));
            Assert.Throws<ArgumentException>(() => notes.Resolve("Notes/../secret.md"));
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
