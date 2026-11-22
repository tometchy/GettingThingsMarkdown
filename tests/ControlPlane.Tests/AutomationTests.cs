using GettingThingsMarkdown.Automations;

namespace GettingThingsMarkdown.Tests;

public sealed class AutomationTests
{
    [Theory]
    [InlineData("---\nstatus: done\n---\n# Example", true)]
    [InlineData("---\nstatus: doing\n---\n# Example", false)]
    [InlineData("---\nfoo: bar\nstatus: 'done'\n---\nTest", true)]
    [InlineData("# No frontmatter\nstatus: done", false)]
    [InlineData("---\nstatus: done later\n---\n", false)]
    public void OnlyTopLevelCompletedFrontmatterTriggers(string markdown, bool expected)
        => Assert.Equal(expected, ArchiveCompletedTasks.IsDone(markdown));

    [Fact]
    public async Task ArchiveMoveIsIdempotentAndRespectsConflicts()
    {
        var root = Path.Combine(Path.GetTempPath(), "gtm-automation-" + Guid.NewGuid());
        try
        {
            var todo = Path.Combine(root, "ToDo");
            Directory.CreateDirectory(todo);
            var file = Path.Combine(todo, "task.md");
            await File.WriteAllTextAsync(file, "---\nstatus: done\n---\n# First");
            var one = await ArchiveCompletedTasks.ApplyAsync(root, file);
            Assert.True(one.Moved);
            Assert.False(one.Conflict);
            Assert.False(File.Exists(file));
            Assert.True(File.Exists(Path.Combine(root, "Archive", "task.md")));
            Assert.False((await ArchiveCompletedTasks.ApplyAsync(root, file)).Moved);

            await File.WriteAllTextAsync(file, "---\nstatus: done\n---\n# Second");
            var two = await ArchiveCompletedTasks.ApplyAsync(root, file);
            Assert.True(two.Conflict);
            Assert.True(File.Exists(two.Destination));
            Assert.Equal("---\nstatus: done\n---\n# First",
                await File.ReadAllTextAsync(Path.Combine(root, "Archive", "task.md")));
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }
}
