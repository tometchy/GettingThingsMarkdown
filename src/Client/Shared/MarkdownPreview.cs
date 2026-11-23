using Avalonia;
using Avalonia.Controls;
using Avalonia.Media;

namespace GettingThingsMarkdown.Client.Shared;

/// <summary>A minimal offline Markdown preview with no HTML execution and no
/// remote image loading. It intentionally renders a safe text subset.</summary>
public static class MarkdownPreview
{
    public static Control Create(string markdown)
    {
        var stack = new StackPanel { Spacing = 5, Margin = new Thickness(4) };
        bool code = false;
        foreach (var line in markdown.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n').Take(2000))
        {
            if (line.StartsWith("```", StringComparison.Ordinal))
            {
                code = !code;
                continue;
            }
            var text = line;
            int size = 14;
            bool heading = false;
            if (!code && line.StartsWith("# ", StringComparison.Ordinal))
            { text = line[2..]; size = 26; heading = true; }
            else if (!code && line.StartsWith("## ", StringComparison.Ordinal))
            { text = line[3..]; size = 21; heading = true; }
            else if (!code && line.StartsWith("### ", StringComparison.Ordinal))
            { text = line[4..]; size = 17; heading = true; }
            else if (!code && (line.StartsWith("- ", StringComparison.Ordinal) ||
                               line.StartsWith("* ", StringComparison.Ordinal)))
                text = "• " + line[2..];
            var block = new TextBlock
            {
                Text = text, FontSize = size, TextWrapping = TextWrapping.Wrap,
                FontWeight = heading ? FontWeight.Bold : FontWeight.Normal,
                FontFamily = code ? FontFamily.Default : FontFamily.Default
            };
            stack.Children.Add(block);
        }
        return new ScrollViewer { Content = stack, MaxHeight = 400 };
    }
}
