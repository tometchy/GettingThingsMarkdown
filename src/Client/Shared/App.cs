using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Themes.Fluent;

namespace GettingThingsMarkdown.Client.Shared;

public sealed class App : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
    }

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            desktop.MainWindow = new Window
            {
                Title = "GettingThingsMarkdown",
                Width = 720,
                Height = 760,
                MinWidth = 420,
                Content = new AccountView()
            };
        }
        else if (ApplicationLifetime is ISingleViewApplicationLifetime browser)
        {
            browser.MainView = new AccountView();
        }
        base.OnFrameworkInitializationCompleted();
    }
}

public static class ApiConfiguration
{
    // Browser sets this to window.location.origin before Avalonia startup.
    // Desktop reads GTM_API_URL; no server credentials are bundled into any client.
    public static Uri BaseAddress { get; set; } = new(
        Environment.GetEnvironmentVariable("GTM_API_URL") ?? "http://localhost:5080/");
}
