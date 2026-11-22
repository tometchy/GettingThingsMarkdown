using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;

namespace GettingThingsMarkdown.Client.Shared;

// Exactly the same Avalonia controls and API client are used by the Linux/Windows
// desktop host and the browser/WASM host.
public sealed class AccountView : UserControl
{
    private readonly AccountApi _api = new();
    private readonly TextBlock _title = new() { FontSize = 27, FontWeight = Avalonia.Media.FontWeight.Bold };
    private readonly TextBlock _description = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly TextBlock _status = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly TextBlock _account = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly TextBox _email = new() { Watermark = "E-mail" };
    private readonly TextBox _password = new() { PasswordChar = '●' };
    private readonly ComboBox _plan = new() { SelectedIndex = 0 };
    private readonly Button _register = new();
    private readonly Button _login = new();
    private readonly Button _checkout = new();
    private readonly Button _portal = new();
    private readonly Button _language = new();
    private readonly Button _openLink = new();
    private Uri? _pendingUrl;
    private AccountInfo? _current;
    private bool _polish = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName != "en";

    public AccountView()
    {
        _plan.ItemsSource = new[] { "Basic · 15 GiB", "Plus · 200 GiB", "Pro · 1 TiB" };
        _plan.SelectedIndex = 0;

        _register.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _current = await _api.RegisterAsync(_email.Text ?? "", _password.Text ?? "", Plan);
            Render();
        });
        _login.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _current = await _api.LoginAsync(_email.Text ?? "", _password.Text ?? "");
            Render();
        });
        _checkout.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _pendingUrl = await _api.CheckoutAsync(Plan);
            Render();
        });
        _portal.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _pendingUrl = await _api.PortalAsync();
            Render();
        });
        _language.Click += (_, _) => { _polish = !_polish; Render(); };
        _openLink.Click += async (_, _) =>
        {
            if (_pendingUrl is not null) await OpenAsync(_pendingUrl);
        };

        var layout = new StackPanel { Spacing = 14 };
        layout.Children.Add(_language);
        layout.Children.Add(_title);
        layout.Children.Add(_description);
        layout.Children.Add(_email);
        layout.Children.Add(_password);
        layout.Children.Add(_plan);
        layout.Children.Add(_register);
        layout.Children.Add(_login);
        layout.Children.Add(_account);
        layout.Children.Add(_checkout);
        layout.Children.Add(_portal);
        layout.Children.Add(_openLink);
        layout.Children.Add(_status);

        Content = new Border
        {
            Padding = new Thickness(24),
            Child = new ScrollViewer { Content = layout }
        };
        Render();
    }

    private string Plan => _plan.SelectedIndex switch { 1 => "plus", 2 => "pro", _ => "basic" };
    private string T(string pl, string en) => _polish ? pl : en;

    private void Render()
    {
        _title.Text = "GettingThingsMarkdown";
        _language.Content = _polish ? "English" : "Polski";
        _description.Text = T("Konto i abonament · jeden interfejs w WWW i aplikacji. Miesiąc bezpłatnie, bez karty.",
            "Account and subscription · same interface on web and desktop. One month free, no card.");
        _password.Watermark = T("Hasło (min. 12 znaków)", "Password (min. 12 characters)");
        _register.Content = T("Załóż konto · trial", "Create account · trial");
        _login.Content = T("Zaloguj się", "Sign in");
        _checkout.Content = T("Przejdź do płatności Stripe", "Continue to Stripe Checkout");
        _portal.Content = T("Zarządzaj subskrypcją", "Manage subscription");
        _openLink.Content = T("Otwórz bezpieczną stronę Stripe", "Open secure Stripe page");
        _openLink.IsVisible = _pendingUrl is not null;
        _checkout.IsEnabled = _current is not null;
        _portal.IsEnabled = _current is not null;
        _account.Text = _current is null
            ? T("Zaloguj się lub utwórz konto.", "Sign in or create an account.")
            : $"{_current.Email}\n{T("Plan", "Plan")}: {_current.Plan}\n" +
              $"{T("Uprawnienia", "Entitlement")}: {_current.Entitlement}\n" +
              $"{T("Trial do", "Trial ends")}: {_current.TrialUntilUtc.ToLocalTime():yyyy-MM-dd}\n" +
              $"{T("Dostęp do zapisu", "Write access")}: {_current.CanWrite}";
    }

    private async Task ExecuteAsync(Func<Task> action)
    {
        _status.Text = T("Wykonywanie operacji…", "Working…");
        try
        {
            await action();
            _status.Text = T("Operacja zakończona.", "Completed.");
        }
        catch (Exception ex)
        {
            _status.Text = T("Błąd: ", "Error: ") + ex.Message;
        }
    }

    private async Task OpenAsync(Uri url)
    {
        var top = TopLevel.GetTopLevel(this);
        if (top is null || !await top.Launcher.LaunchUriAsync(url))
            _status.Text = T("Otwórz ręcznie: ", "Open manually: ") + url.AbsoluteUri;
    }
}
