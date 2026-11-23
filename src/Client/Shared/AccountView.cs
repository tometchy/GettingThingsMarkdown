using System.Globalization;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Platform.Storage;
using GettingThingsMarkdown.Automations;
using Avalonia.Input.Platform;

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
    private readonly TextBox _email = new() { PlaceholderText = "E-mail" };
    private readonly TextBox _password = new() { PasswordChar = '●' };
    private readonly ComboBox _plan = new() { SelectedIndex = 0 };
    private readonly ComboBox _billingInterval = new() { SelectedIndex = 0 };
    private readonly Button _register = new();
    private readonly Button _login = new();
    private readonly Button _checkout = new();
    private readonly Button _portal = new();
    private readonly Button _refresh = new();
    private readonly Button _language = new();
    private readonly Button _openLink = new();
    private readonly IEmbeddedSyncEngine? _sync = ClientRuntime.EmbeddedSyncEngineFactory?.Invoke();
    private readonly TextBlock _tenantState = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly TextBlock _syncState = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly TextBox _folderPath = new() { PlaceholderText = "Path to notes folder" };
    private readonly TextBox _syncAddress = new() { PlaceholderText = "tcp://127.0.0.1:22000",
        Text = "tcp://127.0.0.1:22000" };
    private readonly Button _browse = new();
    private readonly Button _pair = new();
    private readonly Button _recovery = new();
    private readonly TextBox _recoveryImport = new() { PasswordChar = '●',
        PlaceholderText = "Paste recovery secret from your trusted device" };
    private readonly Button _importRecovery = new();
    private readonly Button _tenantRefresh = new();
    private readonly ListBox _devices = new() { MaxHeight = 160 };
    private readonly Button _refreshDevices = new();
    private readonly Button _revokeDevice = new();
    private readonly TextBox _newDocument = new() { PlaceholderText = "Notes/new.md" };
    private readonly ListBox _documents = new() { MaxHeight = 240 };
    private readonly ListBox _historyItems = new() { MaxHeight = 160 };
    private readonly TextBlock _historyStatus = new() { TextWrapping = Avalonia.Media.TextWrapping.Wrap };
    private readonly Button _loadHistory = new();
    private readonly Button _restoreHistory = new();
    private readonly TextBox _editor = new() { AcceptsReturn = true, TextWrapping = Avalonia.Media.TextWrapping.Wrap,
        MinHeight = 180, MaxHeight = 500 };
    private readonly Button _listDocuments = new();
    private readonly Button _openDocument = new();
    private readonly Button _saveDocument = new();
    private readonly Button _previewDocument = new();
    private readonly ContentControl _previewContent = new();
    private MarkdownWorkspace? _workspace;
    private string? _openedDocument;
    private Uri? _pendingUrl;
    private AccountInfo? _current;
    private bool _polish = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName != "en";

    public AccountView()
    {
        _plan.ItemsSource = new[] {
            "Mini · 10 GiB · $1.99/mo",
            "Plus · 75 GiB · $5.99/mo",
            "Pro · 500 GiB · $24.99/mo"
        };
        _billingInterval.ItemsSource = new[] { "Miesięcznie · Monthly", "Rocznie · Annual (-17–25%)" };
        _billingInterval.SelectedIndex = 0;
        _plan.SelectedIndex = 0;

        _register.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _current = await _api.RegisterAsync(_email.Text ?? "", _password.Text ?? "", Plan);
            Render();
        });
        _login.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _current = await _api.LoginAsync(_email.Text ?? "", _password.Text ?? "");
            _plan.SelectedIndex = _current.Plan switch { "plus" => 1, "pro" => 2, _ => 0 };
            Render();
        });
        _checkout.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _pendingUrl = await _api.CheckoutAsync(Plan, BillingInterval);
            Render();
        });
        _refresh.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _current = await _api.CurrentAsync();
            Render();
        });
        _portal.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _pendingUrl = await _api.PortalAsync();
            Render();
        });
        _language.Click += (_, _) => { _polish = !_polish; Render(); };
        _tenantRefresh.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            var tenant = await _api.TenantAsync();
            _tenantState.Text = T("Serwer: ", "Server: ") + tenant.Status +
                (tenant.ServerDeviceId is null ? "" : "\nSyncthing: " + tenant.ServerDeviceId) +
                (tenant.Detail is null ? "" : "\n" + tenant.Detail);
            if (!string.IsNullOrWhiteSpace(tenant.LocalSyncAddress))
                _syncAddress.Text = tenant.LocalSyncAddress;
        });
        _browse.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            var top = TopLevel.GetTopLevel(this) ??
                throw new InvalidOperationException("No window available.");
            var choices = await top.StorageProvider.OpenFolderPickerAsync(
                new FolderPickerOpenOptions { Title = "GettingThingsMarkdown",
                    AllowMultiple = false });
            var localPath = choices.FirstOrDefault()?.TryGetLocalPath();
            if (!string.IsNullOrEmpty(localPath)) _folderPath.Text = localPath;
        });
        _pair.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_current is null || _sync is null)
                throw new InvalidOperationException("Sign in first.");
            await _sync.StartAsync();
            var id = await _sync.DeviceIdAsync();
            var remote = await _api.PairDeviceAsync(id, Environment.MachineName);
            var address = !string.IsNullOrWhiteSpace(remote.Address)
                ? remote.Address : _syncAddress.Text ?? "";
            await _sync.ConfigureFolderAsync(_folderPath.Text ?? "", remote.FolderId,
                remote.ServerDeviceId, address);
            _syncState.Text = T("Urządzenie połączone: ", "Device paired: ") + id +
                "\n" + await _sync.StatusAsync(remote.FolderId);
        });
        _importRecovery.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_sync is null) return;
            await _sync.ImportRecoverySecretAsync("notes", _recoveryImport.Text ?? "");
            _recoveryImport.Text = "";
            _syncState.Text = T("Klucz odzyskiwania zapisano tylko na tym urządzeniu.",
                "Recovery key saved only on this device.");
        });
        _recovery.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_sync is null) return;
            var key = await _sync.ExportRecoverySecretAsync("notes");
            if (string.IsNullOrEmpty(key))
                throw new InvalidOperationException("No recovery key exists yet.");
            var clipboard = TopLevel.GetTopLevel(this)?.Clipboard ??
                throw new InvalidOperationException("Clipboard unavailable.");
            await clipboard.SetTextAsync(key);
            _syncState.Text = T("Klucz odzyskiwania skopiowano. Chroń go.",
                "Recovery key copied. Protect it.");
        });
        _refreshDevices.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            _devices.ItemsSource = await _api.DevicesAsync();
        });
        _revokeDevice.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            var selected = _devices.SelectedItem as string;
            if (selected is null) throw new InvalidOperationException("Select a device.");
            await _api.RevokeDeviceAsync(selected);
            _devices.ItemsSource = await _api.DevicesAsync();
        });
        _loadHistory.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            using var history = JsonDocument.Parse(await _api.EncryptedHistoryJsonAsync("commits"));
            var items = new List<string>();
            foreach (var item in history.RootElement.EnumerateArray().Take(20))
            {
                string sha = item.GetProperty("sha").GetString() ?? "";
                if (sha.Length is not (40 or 64)) continue;
                var message = item.GetProperty("commit").GetProperty("message").GetString() ?? "";
                items.Add(sha + " | " + message.Replace("\n", " ", StringComparison.Ordinal)[..Math.Min(message.Length, 80)]);
            }
            _historyItems.ItemsSource = items;
        });
        _restoreHistory.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_sync is null) return;
            string value = _historyItems.SelectedItem as string ??
                throw new InvalidOperationException("Select a historical Git revision.");
            string sha = value.Split(" | ", 2, StringSplitOptions.None)[0];
            var top = TopLevel.GetTopLevel(this) ??
                throw new InvalidOperationException("No desktop window.");
            var selection = await top.StorageProvider.OpenFolderPickerAsync(
                new FolderPickerOpenOptions {
                    Title = T("Folder do odzyskania historii", "Choose restoration directory"),
                    AllowMultiple = false
                });
            string dir = selection.FirstOrDefault()?.TryGetLocalPath() ?? "";
            if (string.IsNullOrWhiteSpace(dir)) return;
            string restored = await _sync.RestoreSnapshotAsync(_api, sha, dir);
            _historyStatus.Text = T("Odzyskano zaszyfrowaną historię do: ",
                "History decrypted locally into: ") + restored;
        });
        _listDocuments.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            var path = _folderPath.Text ?? "";
            _workspace = new MarkdownWorkspace(path);
            _documents.ItemsSource = _workspace.List();
            await Task.CompletedTask;
        });
        _openDocument.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_workspace is null) throw new InvalidOperationException("Select and list a workspace folder.");
            _openedDocument = _documents.SelectedItem as string
                ?? throw new InvalidOperationException("Choose a Markdown file.");
            _editor.Text = await _workspace.ReadAsync(_openedDocument);
            _newDocument.Text = _openedDocument;
        });
        _saveDocument.Click += async (_, _) => await ExecuteAsync(async () =>
        {
            if (_workspace is null) throw new InvalidOperationException("Select and list a workspace folder.");
            var path = (_newDocument.Text ?? _openedDocument ?? "").Trim();
            if (string.IsNullOrWhiteSpace(path)) throw new InvalidOperationException("Enter a Markdown file path.");
            await _workspace.SaveAsync(path, _editor.Text ?? "");
            _openedDocument = path;
            _documents.ItemsSource = _workspace.List();
        });
        _previewDocument.Click += (_, _) =>
            _previewContent.Content = MarkdownPreview.Create(_editor.Text ?? "");
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
        layout.Children.Add(_billingInterval);
        layout.Children.Add(_register);
        layout.Children.Add(_login);
        layout.Children.Add(_account);
        layout.Children.Add(_refresh);
        layout.Children.Add(_checkout);
        layout.Children.Add(_portal);
        layout.Children.Add(_openLink);
        layout.Children.Add(_tenantRefresh);
        layout.Children.Add(_tenantState);
        layout.Children.Add(_refreshDevices);
        layout.Children.Add(_devices);
        layout.Children.Add(_revokeDevice);
        if (_sync is not null)
        {
            layout.Children.Add(new TextBlock { Text = "Syncthing E2EE",
                FontSize = 21, FontWeight = Avalonia.Media.FontWeight.SemiBold });
            layout.Children.Add(_folderPath);
            layout.Children.Add(_browse);
            layout.Children.Add(_syncAddress);
            layout.Children.Add(_pair);
            layout.Children.Add(_recovery);
            layout.Children.Add(_recoveryImport);
            layout.Children.Add(_importRecovery);
            layout.Children.Add(_syncState);
            layout.Children.Add(new TextBlock { Text = "Git / E2EE history",
                FontSize = 20, FontWeight = Avalonia.Media.FontWeight.SemiBold });
            layout.Children.Add(_loadHistory);
            layout.Children.Add(_historyItems);
            layout.Children.Add(_restoreHistory);
            layout.Children.Add(_historyStatus);
            layout.Children.Add(new TextBlock { Text = "Markdown",
                FontSize = 21, FontWeight = Avalonia.Media.FontWeight.SemiBold });
            layout.Children.Add(_listDocuments);
            layout.Children.Add(_documents);
            layout.Children.Add(_openDocument);
            layout.Children.Add(_newDocument);
            layout.Children.Add(_editor);
            layout.Children.Add(_saveDocument);
            layout.Children.Add(_previewDocument);
            layout.Children.Add(_previewContent);
        }
        layout.Children.Add(_status);

        Content = new Border
        {
            Padding = new Thickness(24),
            Child = new ScrollViewer { Content = layout }
        };
        Render();
    }

    private string Plan => _plan.SelectedIndex switch { 1 => "plus", 2 => "pro", _ => "basic" };
    private string BillingInterval => _billingInterval.SelectedIndex == 1 ? "year" : "month";
    private string T(string pl, string en) => _polish ? pl : en;

    private void Render()
    {
        _title.Text = "GettingThingsMarkdown";
        _language.Content = _polish ? "English" : "Polski";
        _description.Text = T("Konto i abonament · jeden interfejs w WWW i aplikacji. Miesiąc bezpłatnie, bez karty.",
            "Account and subscription · same interface on web and desktop. One month free, no card.");
        _password.PlaceholderText = T("Hasło (min. 12 znaków)", "Password (min. 12 characters)");
        _register.Content = T("Załóż konto · trial", "Create account · trial");
        _login.Content = T("Zaloguj się", "Sign in");
        _checkout.Content = T("Przejdź do płatności Stripe", "Continue to Stripe Checkout");
        _billingInterval.IsEnabled = _current is not null;
        _portal.Content = T("Zarządzaj subskrypcją", "Manage subscription");
        _refresh.Content = T("Odśwież stan abonamentu", "Refresh subscription status");
        _tenantRefresh.Content = T("Sprawdź stan serwera", "Check tenant server");
        _refreshDevices.Content = T("Wyświetl sparowane urządzenia", "List paired devices");
        _revokeDevice.Content = T("Cofnij dostęp wybranemu urządzeniu", "Revoke selected device");
        _refreshDevices.IsEnabled = _current is not null;
        _revokeDevice.IsEnabled = _current is not null;
        _browse.Content = T("Wybierz folder", "Choose folder");
        _pair.Content = T("Połącz folder z szyfrowanym serwerem", "Pair encrypted folder");
        _recovery.Content = T("Skopiuj klucz odzyskiwania", "Copy recovery key");
        _importRecovery.Content = T("Importuj klucz z zaufanego urządzenia", "Import key from trusted device");
        _loadHistory.Content = T("Pokaż zapisane wersje Git", "List historical revisions");
        _restoreHistory.Content = T("Odszyfruj wybraną wersję lokalnie", "Restore selected version locally");
        _loadHistory.IsEnabled = _current is not null;
        _restoreHistory.IsEnabled = _current is not null;
        _listDocuments.Content = T("Wyświetl pliki Markdown", "List Markdown files");
        _openDocument.Content = T("Otwórz zaznaczony plik", "Open selected file");
        _saveDocument.Content = T("Zapisz plik", "Save Markdown file");
        _previewDocument.Content = T("Podgląd Markdown", "Markdown preview");
        _tenantRefresh.IsEnabled = _current is not null;
        _pair.IsEnabled = _current is not null;
        _refresh.IsEnabled = _current is not null;
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
