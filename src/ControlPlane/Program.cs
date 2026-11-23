using System.ComponentModel.DataAnnotations;
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.RateLimiting;
using GettingThingsMarkdown.ControlPlane;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

string? jwtSecret = builder.Configuration["GTM_JWT_SECRET"];
if (string.IsNullOrWhiteSpace(jwtSecret))
    throw new InvalidOperationException("Set GTM_JWT_SECRET to a random string of at least 32 bytes.");
var tokenService = new TokenService(jwtSecret);
builder.Services.AddSingleton(tokenService);
builder.Services.AddSingleton<IPasswordHasher<Account>, PasswordHasher<Account>>();
builder.Services.AddDbContext<AppDb>(o => o.UseSqlite(
    builder.Configuration["GTM_DB"] ?? "Data Source=gtm.sqlite"));
builder.Services.AddDbContext<ProvisioningDb>(o => o.UseSqlite(
    builder.Configuration["GTM_PROVISIONING_DB"] ?? "Data Source=gtm-provisioning.sqlite"));
builder.Services.AddHostedService<ProvisioningWorker>();
builder.Services.AddHttpClient<StripeGateway>(client => client.Timeout = TimeSpan.FromSeconds(20));
builder.Services.AddSingleton<EmailVerificationService>();
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(o => o.TokenValidationParameters = tokenService.Validation);
builder.Services.AddAuthorization();
builder.Services.AddRateLimiter(o =>
{
    o.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    o.AddPolicy("auth", context => RateLimitPartition.GetFixedWindowLimiter(
        context.Connection.RemoteIpAddress?.ToString() ?? "unknown", _ =>
            new FixedWindowRateLimiterOptions
            {
                PermitLimit = 10, Window = TimeSpan.FromMinutes(1), QueueLimit = 0
            }));
});
builder.Services.AddCors(o => o.AddPolicy("browser", policy =>
{
    var allowed = builder.Configuration["GTM_WEB_ORIGIN"];
    if (!string.IsNullOrWhiteSpace(allowed))
        policy.WithOrigins(allowed).AllowAnyHeader().AllowAnyMethod();
}));

if (!builder.Environment.IsDevelopment() &&
    !EmailVerificationService.IsConfigured(builder.Configuration))
    throw new InvalidOperationException(
        "Production registration requires GTM_SMTP_HOST, GTM_SMTP_FROM, " +
        "GTM_SMTP_USERNAME, GTM_SMTP_PASSWORD and an HTTPS GTM_PUBLIC_URL.");
var app = builder.Build();
if (!app.Environment.IsDevelopment()) app.UseHsts();
app.UseHttpsRedirection();
app.UseCors("browser");
app.UseRateLimiter();
app.UseAuthentication();
app.UseAuthorization();
app.UseDefaultFiles();
app.UseStaticFiles();

using (var scope = app.Services.CreateScope())
{
    var accountsDb = scope.ServiceProvider.GetRequiredService<AppDb>();
    await accountsDb.Database.EnsureCreatedAsync();
    // EnsureCreated does not migrate pre-existing SQLite schemas. On an old
    // database, retain every existing subscriber's original quota before
    // activating the new 10/75/500 GiB catalog. This operation is idempotent.
    var connection = accountsDb.Database.GetDbConnection();
    await accountsDb.Database.OpenConnectionAsync();
    bool quotaColumnExists = false;
    using (var inspect = connection.CreateCommand())
    {
        inspect.CommandText = "PRAGMA table_info('Accounts')";
        using var reader = await inspect.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            if (string.Equals(reader.GetString(1), "QuotaOverrideBytes",
                StringComparison.OrdinalIgnoreCase)) quotaColumnExists = true;
        }
    }
    if (!quotaColumnExists)
    {
        await accountsDb.Database.ExecuteSqlRawAsync(
            "ALTER TABLE Accounts ADD COLUMN QuotaOverrideBytes INTEGER NULL");
        await accountsDb.Database.ExecuteSqlRawAsync(
            "UPDATE Accounts SET QuotaOverrideBytes = CASE Plan " +
            "WHEN 'basic' THEN 16106127360 WHEN 'plus' THEN 214748364800 " +
            "WHEN 'pro' THEN 1099511627776 ELSE NULL END " +
            "WHERE QuotaOverrideBytes IS NULL");
    }
    // Grandfather previously created accounts as email-verified. Only accounts
    // registered *after* this migration require a new confirmation token.
    var columns = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    using (var inspect = connection.CreateCommand())
    {
        inspect.CommandText = "PRAGMA table_info('Accounts')";
        using var reader = await inspect.ExecuteReaderAsync();
        while (await reader.ReadAsync()) columns.Add(reader.GetString(1));
    }
    if (!columns.Contains("EmailVerifiedUtc"))
    {
        await accountsDb.Database.ExecuteSqlRawAsync(
            "ALTER TABLE Accounts ADD COLUMN EmailVerifiedUtc TEXT NULL");
        await accountsDb.Database.ExecuteSqlRawAsync(
            "UPDATE Accounts SET EmailVerifiedUtc = CreatedUtc WHERE EmailVerifiedUtc IS NULL");
    }
    // Idempotent migration of optional, expiring bonus quota; old users start
    // with zero gift. Never alter their existing base allowance.
    foreach (var field in new[] { ("GiftQuotaGiB", "INTEGER NOT NULL DEFAULT 0"),
                                  ("GiftQuotaExpiresUtc", "TEXT") })
        if (!columns.Contains(field.Item1))
            await accountsDb.Database.ExecuteSqlRawAsync(
                $"ALTER TABLE Accounts ADD COLUMN {field.Item1} {field.Item2}");
    foreach (var field in new[] {
        ("VerificationTokenHash", "TEXT"), ("VerificationExpiresUtc", "TEXT"),
        ("VerificationSentUtc", "TEXT") })
        if (!columns.Contains(field.Item1))
            await accountsDb.Database.ExecuteSqlRawAsync(
                $"ALTER TABLE Accounts ADD COLUMN {field.Item1} {field.Item2} NULL");
    if (!columns.Contains("HistoryHoldUntilUtc"))
        await accountsDb.Database.ExecuteSqlRawAsync(
            "ALTER TABLE Accounts ADD COLUMN HistoryHoldUntilUtc TEXT NULL");
    // EnsureCreated does not add new tables to existing installations.
    // This durable intent table stores only opaque encrypted paths and no passwords.
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE TABLE IF NOT EXISTS "HistoryDeletionIntents" (
            "Id" TEXT NOT NULL CONSTRAINT "PK_HistoryDeletionIntents" PRIMARY KEY,
            "AccountId" TEXT NOT NULL,
            "OpaquePath" TEXT NOT NULL,
            "State" TEXT NOT NULL,
            "RequestedUtc" TEXT NOT NULL,
            "PurgeNotBeforeUtc" TEXT NOT NULL,
            "CanceledUtc" TEXT NULL
        )
        """);
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE UNIQUE INDEX IF NOT EXISTS "IX_HistoryDeletionIntents_AccountId_OpaquePath"
        ON "HistoryDeletionIntents" ("AccountId", "OpaquePath")
        """);
    // Old customer SQLite databases predate encrypted SMTP mailboxes.
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE TABLE IF NOT EXISTS "MailInboxes" (
            "AccountId" TEXT NOT NULL CONSTRAINT "PK_MailInboxes" PRIMARY KEY,
            "LocalPart" TEXT NOT NULL,
            "PublicKeyBase64" TEXT NOT NULL,
            "Enabled" INTEGER NOT NULL,
            "CreatedUtc" TEXT NOT NULL
        )
        """);
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE UNIQUE INDEX IF NOT EXISTS "IX_MailInboxes_LocalPart"
        ON "MailInboxes" ("LocalPart")
        """);
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE TABLE IF NOT EXISTS "EncryptedInboundMails" (
            "Id" TEXT NOT NULL CONSTRAINT "PK_EncryptedInboundMails" PRIMARY KEY,
            "AccountId" TEXT NOT NULL,
            "DedupSha256" TEXT NOT NULL,
            "WrappedKey" BLOB NOT NULL,
            "Nonce" BLOB NOT NULL,
            "Tag" BLOB NOT NULL,
            "Ciphertext" BLOB NOT NULL,
            "ReceivedUtc" TEXT NOT NULL,
            "AcknowledgedUtc" TEXT NULL
        )
        """);
    await accountsDb.Database.ExecuteSqlRawAsync("""
        CREATE UNIQUE INDEX IF NOT EXISTS "IX_EncryptedInboundMails_AccountId_DedupSha256"
        ON "EncryptedInboundMails" ("AccountId", "DedupSha256")
        """);
    // Existing SQLite databases must retain queued encrypted mail when
    // introducing the non-destructive ACK and recovery archive.
    bool hasMailAckColumn = false;
    using (var inspect = connection.CreateCommand())
    {
        inspect.CommandText = "PRAGMA table_info('EncryptedInboundMails')";
        using var reader = await inspect.ExecuteReaderAsync();
        while (await reader.ReadAsync())
            hasMailAckColumn |= string.Equals(reader.GetString(1), "AcknowledgedUtc",
                StringComparison.OrdinalIgnoreCase);
    }
    if (!hasMailAckColumn)
        await accountsDb.Database.ExecuteSqlRawAsync(
            "ALTER TABLE EncryptedInboundMails ADD COLUMN AcknowledgedUtc TEXT NULL");
    await accountsDb.Database.CloseConnectionAsync();
    await scope.ServiceProvider.GetRequiredService<ProvisioningDb>().Database.EnsureCreatedAsync();
}

var clock = TimeProvider.System;
app.MapGet("/healthz", () => Results.Ok(new { status = "alive" }));
// Liveness must not be mistaken for a database-backed readiness signal.
app.MapGet("/readyz", async (AppDb accounts, ProvisioningDb tenants, CancellationToken ct) =>
{
    try
    {
        var ready = await accounts.Database.CanConnectAsync(ct) &&
                    await tenants.Database.CanConnectAsync(ct);
        return ready ? Results.Ok(new { status = "ready" }) : Results.StatusCode(503);
    }
    catch (Exception) { return Results.StatusCode(503); }
});
app.MapGet("/api/plans", () => Results.Ok(PlanCatalog.Tiers.Values.Select(p =>
    new { id = p.Id, name = p.Name, quotaBytes = p.QuotaBytes,
        currency = "usd", monthlyCents = p.MonthlyUsdCents,
        annualCents = p.AnnualUsdCents, trialMonths = 1 })));

app.MapPost("/api/auth/register", async (RegisterRequest input, AppDb db,
    IPasswordHasher<Account> hasher, TokenService tokens,
    EmailVerificationService emailer, CancellationToken ct) =>
{
    var email = (input.Email ?? "").Trim().ToLowerInvariant();
    var plan = (input.Plan ?? "").Trim().ToLowerInvariant();
    if (!new EmailAddressAttribute().IsValid(email) || email.Length > 254 ||
        input.Password is null || input.Password.Length < 12 || input.Password.Length > 1024 ||
        !PlanCatalog.IsValid(plan))
        return Results.BadRequest(new { error = "Invalid email, plan or password (minimum 12 characters)." });

    var now = clock.GetUtcNow();
    bool longTrial = app.Environment.IsDevelopment() &&
        builder.Configuration.GetValue<bool>("GTM_DEV_LONG_TRIAL");
    var account = new Account
    {
        Email = email, Plan = plan, CreatedUtc = now,
        EmailVerifiedUtc = app.Environment.IsDevelopment() ? now : null,
        TrialUntilUtc = app.Environment.IsDevelopment()
            ? TrialPolicy.NewTrialEnd(now, true, longTrial)
            : DateTimeOffset.MinValue
    };
    string? verificationToken = null;
    if (account.EmailVerifiedUtc is null)
    {
        verificationToken = EmailVerificationService.NewToken();
        account.VerificationTokenHash = EmailVerificationService.Hash(verificationToken);
        account.VerificationExpiresUtc = now.AddHours(24);
        account.VerificationSentUtc = now;
    }
    account.PasswordHash = hasher.HashPassword(account, input.Password);
    db.Accounts.Add(account);
    try { await db.SaveChangesAsync(ct); }
    catch (DbUpdateException) { return Results.Conflict(new { error = "Account already exists." }); }
    if (verificationToken is not null)
    {
        try { await emailer.SendAsync(account.Email, verificationToken, ct); }
        catch (Exception)
        {
            // Account is still unverified (and cannot provision a tenant);
            // the customer can use resend once SMTP is healthy.
            return Results.Problem("Verification email temporarily unavailable.", statusCode: 503);
        }
    }
    return Results.Created("/api/account", new AuthResponse(tokens.Issue(account),
        PlanCatalog.View(account, now)));
}).RequireRateLimiting("auth");

app.MapGet("/api/auth/verify-email", async (string email, string token,
    AppDb db, CancellationToken ct) =>
{
    var normalized = email.Trim().ToLowerInvariant();
    var account = await db.Accounts.SingleOrDefaultAsync(a => a.Email == normalized, ct);
    var now = clock.GetUtcNow();
    if (account is null || account.EmailVerifiedUtc is not null ||
        account.VerificationTokenHash is null || account.VerificationExpiresUtc <= now ||
        !EmailVerificationService.Matches(token, account.VerificationTokenHash))
        return Results.BadRequest(new { error = "Link expired or invalid." });
    account.EmailVerifiedUtc = now;
    account.TrialUntilUtc = TrialPolicy.NewTrialEnd(now, false, false);
    account.VerificationTokenHash = null;
    account.VerificationExpiresUtc = null;
    await db.SaveChangesAsync(ct);
    return Results.Redirect("/app/?verified=1");
}).RequireRateLimiting("auth");

app.MapPost("/api/auth/resend-verification", async (ResendVerificationRequest request,
    AppDb db, EmailVerificationService emailer, CancellationToken ct) =>
{
    var email = (request.Email ?? "").Trim().ToLowerInvariant();
    var account = await db.Accounts.SingleOrDefaultAsync(a => a.Email == email, ct);
    if (account is null || account.EmailVerifiedUtc is not null)
        return Results.Ok(new { message = "Check your inbox if verification is needed." });
    var now = clock.GetUtcNow();
    if (account.VerificationSentUtc is { } last && now - last < TimeSpan.FromMinutes(10))
        return Results.Ok(new { message = "Check your inbox if verification is needed." });
    var token = EmailVerificationService.NewToken();
    account.VerificationTokenHash = EmailVerificationService.Hash(token);
    account.VerificationExpiresUtc = now.AddHours(24);
    account.VerificationSentUtc = now;
    await db.SaveChangesAsync(ct);
    try { await emailer.SendAsync(account.Email, token, ct); }
    catch (Exception) { return Results.Problem("Verification mail unavailable.", statusCode: 503); }
    return Results.Ok(new { message = "Check your inbox if verification is needed." });
}).RequireRateLimiting("auth");

app.MapPost("/api/auth/login", async (LoginRequest input, AppDb db,
    IPasswordHasher<Account> hasher, TokenService tokens, CancellationToken ct) =>
{
    var email = (input.Email ?? "").Trim().ToLowerInvariant();
    var account = await db.Accounts.SingleOrDefaultAsync(a => a.Email == email, ct);
    if (account is null || input.Password is null ||
        hasher.VerifyHashedPassword(account, account.PasswordHash, input.Password)
            == PasswordVerificationResult.Failed)
        return Results.Unauthorized();
    return Results.Ok(new AuthResponse(tokens.Issue(account),
        PlanCatalog.View(account, clock.GetUtcNow())));
}).RequireRateLimiting("auth");

var authenticated = app.MapGroup("/api").RequireAuthorization();
authenticated.MapGet("/account", async (ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    return account is null ? Results.NotFound() :
        Results.Ok(PlanCatalog.View(account, clock.GetUtcNow()));
});

authenticated.MapGet("/tenant", async (ClaimsPrincipal user, ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var tenant = await db.Tenants.FindAsync([id.Value], ct);
    return Results.Ok(tenant is null
        ? new TenantStatusView("Pending", null, "Provisioning not started.", TenantNames.FolderId, null)
        : new TenantStatusView(tenant.Status, tenant.ServerDeviceId,
            tenant.Status == "Failed" ? tenant.LastError : null, TenantNames.FolderId,
            builder.Configuration["GTM_SYNC_PUBLIC_ADDRESS"]));
});

authenticated.MapPost("/devices", async (AddDeviceRequest input, ClaimsPrincipal user,
    ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready") return Results.Problem(
        "Tenant not yet ready.", statusCode: 409);
    if (!builder.Configuration.GetValue<bool>("GTM_KUBE_ENABLED"))
        return Results.Problem("Kubernetes not enabled.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Missing tenant credentials.", statusCode: 503);
    using var syncthing = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    try
    {
        await syncthing.AddDeviceAsync(input.DeviceId, input.Name, ct);
        return Results.Ok(new PairResponse(TenantNames.FolderId,
            tenant.ServerDeviceId ?? await syncthing.DeviceIdAsync(ct),
            builder.Configuration["GTM_SYNC_PUBLIC_ADDRESS"] ?? "",
            "Syncthing Untrusted Device Encryption; key remains on trusted clients"));
    }
    catch (ArgumentException) { return Results.BadRequest(new { error = "Invalid device ID." }); }
    catch (HttpRequestException) { return Results.Problem("Syncthing pairing failed.", statusCode: 503); }
});

authenticated.MapGet("/devices", async (ClaimsPrincipal user,
    ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready")
        return Results.Problem("Tenant not ready.", statusCode: 409);
    if (!KubeApi.Available) return Results.Problem("Kubernetes unavailable.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Tenant unavailable.", statusCode: 503);
    using var sync = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    return Results.Ok(await sync.ListPairedDeviceIdsAsync(ct));
});

authenticated.MapDelete("/devices/{deviceId}", async (string deviceId,
    ClaimsPrincipal user, ProvisioningDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var tenant = id is null ? null : await db.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status != "Ready")
        return Results.Problem("Tenant not ready.", statusCode: 409);
    if (!KubeApi.Available) return Results.Problem("Kubernetes unavailable.", statusCode: 503);
    using var kube = new KubeApi();
    var credentials = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (credentials is null) return Results.Problem("Tenant unavailable.", statusCode: 503);
    using var sync = new SyncthingTenantApi(tenant.Namespace, credentials["sync-api-key"]);
    try { await sync.RemoveDeviceAsync(deviceId, ct); }
    catch (ArgumentException) { return Results.BadRequest(new { error = "Invalid device ID." }); }
    return Results.NoContent();
});

// Gitea is never reachable directly from untrusted clients. The API exposes
// only bounded read-only encrypted Git objects belonging to the current account.
async Task<IResult> GetHistoryAsync(ClaimsPrincipal user, string? sha, string kind,
    ProvisioningDb states, CancellationToken ct)
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var tenant = await states.Tenants.FindAsync([id.Value], ct);
    if (tenant is null || tenant.Status is not ("Ready" or "Suspended"))
        return Results.Problem("History tenant not ready.", statusCode: 409);
    if (sha is not null && !GitHistoryProxy.ValidGitOid(sha))
        return Results.BadRequest(new { error = "Invalid Git SHA." });
    if (!KubeApi.Available)
        return Results.Problem("History access requires Kubernetes.", statusCode: 503);
    using var kube = new KubeApi();
    var cert = await kube.SecretAsync(tenant.Namespace, "history-ca", ct);
    var secrets = await kube.SecretAsync(tenant.Namespace, "gtm-credentials", ct);
    if (cert is null || secrets is null)
        return Results.Problem("Missing tenant history credentials.", statusCode: 503);
    using var history = new GitHistoryProxy(tenant.Namespace,
        cert["tls.crt"], secrets["reader-pass"]);
    try
    {
        string body = kind switch
        {
            "commits" => await history.RecentCommitsAsync(ct),
            "tree" => await history.TreeAsync(sha!, ct),
            "blob" => await history.EncryptedBlobAsync(sha!, ct),
            _ => throw new InvalidOperationException("Unknown history action")
        };
        return Results.Content(body, "application/json");
    }
    catch (HttpRequestException)
    {
        return Results.Problem("Encrypted history unavailable.", statusCode: 503);
    }
    catch (InvalidOperationException)
    {
        return Results.Problem("Encrypted history response rejected.", statusCode: 413);
    }
}

authenticated.MapGet("/history/commits", (ClaimsPrincipal user,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, null, "commits", states, ct));
authenticated.MapGet("/history/tree/{sha}", (ClaimsPrincipal user, string sha,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, sha, "tree", states, ct));
authenticated.MapGet("/history/blob/{sha}", (ClaimsPrincipal user, string sha,
    ProvisioningDb states, CancellationToken ct) =>
    GetHistoryAsync(user, sha, "blob", states, ct));


//// HISTORY PROTECTION (fail-closed: intent/undo only; no privileged Git rewrite yet).
// The frontend supplies the encrypted Git-relative path, never a plaintext name.
// Access to historical ciphertext remains independent of paid entitlement.
authenticated.MapGet("/history/deletion-requests", async (
    ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    var intents = await db.HistoryDeletionIntents.AsNoTracking()
        .Where(x => x.AccountId == account.Id).ToListAsync(ct);
    return Results.Ok(new {
        tenantHoldUntilUtc = account.HistoryHoldUntilUtc,
        physicalPurgeAvailable = false,
        notice = "Removal from Git history is NOT yet implemented. No automatic purge occurs.",
        requests = intents.OrderByDescending(x => x.RequestedUtc).Take(100).Select(x =>
            new HistoryDeletionView(x.Id, x.OpaquePath, x.State, x.RequestedUtc,
                x.PurgeNotBeforeUtc,
                HistoryProtectionPolicy.EffectiveNotBefore(x, account), false))
    });
});

authenticated.MapPost("/history/deletion-requests", async (
    RequestHistoryDeletion request, ClaimsPrincipal user, AppDb db,
    IPasswordHasher<Account> hasher, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var account = await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    // Every request requires fresh verification; a 12-hour bearer alone is
    // insufficient. Neither password nor decrypted name is ever persisted.
    if (request.Password is null || request.Password.Length > 1024 ||
        hasher.VerifyHashedPassword(account, account.PasswordHash, request.Password)
            == PasswordVerificationResult.Failed)
        return Results.Unauthorized();
    if (!HistoryProtectionPolicy.ValidOpaquePath(request.OpaquePath))
        return Results.BadRequest(new { error = "Provide a valid encrypted relative path." });
    var existing = await db.HistoryDeletionIntents.SingleOrDefaultAsync(x =>
        x.AccountId == id.Value && x.OpaquePath == request.OpaquePath, ct);
    var now = clock.GetUtcNow();
    if (existing is null)
    {
        if (await db.HistoryDeletionIntents.CountAsync(x =>
            x.AccountId == id.Value && x.State == "Pending", ct) >= 100)
            return Results.Problem("Maximum 100 pending requests per account.", statusCode: 429);
        existing = HistoryProtectionPolicy.New(account.Id, request.OpaquePath, now);
        db.HistoryDeletionIntents.Add(existing);
    }
    else if (existing.State == "Canceled")
        HistoryProtectionPolicy.RestartCanceled(existing, now);
    else if (existing.State != "Pending")
        return Results.Conflict(new { error = "Path already processed." });
    else
        return Results.Accepted($"/api/history/deletion-requests/{existing.Id}",
            new HistoryDeletionView(existing.Id, existing.OpaquePath, existing.State,
                existing.RequestedUtc, existing.PurgeNotBeforeUtc,
                HistoryProtectionPolicy.EffectiveNotBefore(existing, account), false));

    db.Audit.Add(new AuditEntry { AccountId = account.Id,
        Action = "history-deletion-intent", Actor = "account-password",
        OccurredUtc = now,
        Details = $"intent={existing.Id};encryptedPathSha256Prefix={HistoryProtectionPolicy.Fingerprint(existing.OpaquePath)};notBefore={existing.PurgeNotBeforeUtc:O}" });
    try { await db.SaveChangesAsync(ct); }
    catch (DbUpdateException)
    {
        return Results.Conflict(new { error = "Concurrent request; reload history protection status." });
    }
    return Results.Accepted($"/api/history/deletion-requests/{existing.Id}",
        new HistoryDeletionView(existing.Id, existing.OpaquePath, existing.State,
            existing.RequestedUtc, existing.PurgeNotBeforeUtc,
            HistoryProtectionPolicy.EffectiveNotBefore(existing, account), false));
}).RequireRateLimiting("auth");

authenticated.MapDelete("/history/deletion-requests/{requestId:guid}", async (
    Guid requestId, ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var intent = await db.HistoryDeletionIntents.SingleOrDefaultAsync(x =>
        x.Id == requestId && x.AccountId == id.Value, ct);
    if (intent is null) return Results.NotFound();
    if (intent.State != "Pending")
        return Results.Conflict(new { error = "Request is not pending." });
    var now = clock.GetUtcNow();
    HistoryProtectionPolicy.Cancel(intent, now);
    db.Audit.Add(new AuditEntry { AccountId = id.Value,
        Action = "history-deletion-undone", Actor = "account",
        OccurredUtc = now, Details = $"intent={intent.Id}" });
    await db.SaveChangesAsync(ct);
    return Results.NoContent();
});


// NEW: The SMTP gateway encrypts at ingest, without the Syncthing E2EE
// secret or any tenant private key. A local trusted client imports the
// decrypted task into its ordinary Syncthing Send/Receive workspace.
authenticated.MapGet("/mail/inbox", async (ClaimsPrincipal user, AppDb db,
    CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.StatusCode(403);
    var domain = builder.Configuration["GTM_INBOUND_MAIL_DOMAIN"];
    if (string.IsNullOrWhiteSpace(domain))
        return Results.Problem("Inbound mail is not configured.", statusCode: 503);
    var inbox = await db.MailInboxes.FindAsync([account.Id], ct);
    if (inbox is null)
        return Results.Ok(new MailInboxView(false, "", 0, false));
    var pending = await db.EncryptedInboundMails.CountAsync(x =>
        x.AccountId == account.Id && x.AcknowledgedUtc == null, ct);
    return Results.Ok(new MailInboxView(inbox.Enabled,
        inbox.LocalPart + "@" + domain, pending, true));
});

authenticated.MapPost("/mail/inbox", async (RegisterMailInbox request, ClaimsPrincipal user,
    AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.StatusCode(403);
    var domain = builder.Configuration["GTM_INBOUND_MAIL_DOMAIN"];
    if (string.IsNullOrWhiteSpace(domain))
        return Results.Problem("Inbound mail domain is not configured.", statusCode: 503);
    if (!MailEncryption.ValidatePublicKey(request.PublicKeyBase64 ?? ""))
        return Results.BadRequest(new { error = "Expected 3072/4096-bit RSA SPKI public key." });
    var inbox = await db.MailInboxes.FindAsync([account.Id], ct);
    if (inbox is not null && inbox.PublicKeyBase64 != request.PublicKeyBase64)
        return Results.Conflict(new { error = "Inbox has another public key; import the original private key on this device." });
    if (inbox is null)
    {
        inbox = new MailInbox {
            AccountId = account.Id, PublicKeyBase64 = request.PublicKeyBase64,
            LocalPart = MailInboxPolicy.NewLocalPart(), Enabled = true,
            CreatedUtc = clock.GetUtcNow()
        };
        db.MailInboxes.Add(inbox);
        await db.SaveChangesAsync(ct);
        db.Audit.Add(new AuditEntry { AccountId = account.Id,
            Action = "mail-inbox-created", Actor = "account",
            OccurredUtc = clock.GetUtcNow(), Details = "rsa3072-public-key" });
        await db.SaveChangesAsync(ct);
    }
    return Results.Ok(new MailInboxView(inbox.Enabled,
        inbox.LocalPart + "@" + domain,
        await db.EncryptedInboundMails.CountAsync(x =>
            x.AccountId == account.Id && x.AcknowledgedUtc == null, ct),
        true));
}).RequireRateLimiting("auth");

authenticated.MapPost("/mail/inbox/rotate-address", async (RotateMailAddress request,
    ClaimsPrincipal user, AppDb db, IPasswordHasher<Account> hasher, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.StatusCode(403);
    if (string.IsNullOrEmpty(request.Password) ||
        hasher.VerifyHashedPassword(account, account.PasswordHash, request.Password) ==
            PasswordVerificationResult.Failed)
        return Results.Unauthorized();
    var inbox = await db.MailInboxes.FindAsync([account.Id], ct);
    if (inbox is null) return Results.NotFound();
    inbox.LocalPart = MailInboxPolicy.NewLocalPart();
    inbox.Enabled = true;
    db.Audit.Add(new AuditEntry { AccountId = account.Id, Action = "mail-alias-rotated",
        Actor = "account-password", OccurredUtc = clock.GetUtcNow(),
        Details = "Old alias disabled; no private key change." });
    await db.SaveChangesAsync(ct);
    return Results.Ok(new MailInboxView(true,
        inbox.LocalPart + "@" + builder.Configuration["GTM_INBOUND_MAIL_DOMAIN"],
        await db.EncryptedInboundMails.CountAsync(x =>
            x.AccountId == account.Id && x.AcknowledgedUtc == null, ct), true));
}).RequireRateLimiting("auth");

authenticated.MapGet("/mail/messages", async (ClaimsPrincipal user, AppDb db,
    CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.StatusCode(403);
    var messages = await db.EncryptedInboundMails.AsNoTracking()
        .Where(x => x.AccountId == account.Id && x.AcknowledgedUtc == null)
        .Take(50).ToListAsync(ct);
    return Results.Ok(messages.Select(MailEncryption.View).ToArray());
});

// A device can re-import mail after losing its local folder during the
// recovery window. Archived bodies remain ciphertext and are tenant-scoped.
authenticated.MapGet("/mail/messages/archived", async (
    int? skip, ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    if (skip is < 0 or > MailInboxPolicy.MaxRetainedMessages)
        return Results.BadRequest(new { error = "Invalid archive offset." });
    var mails = await db.EncryptedInboundMails.AsNoTracking()
        .Where(x => x.AccountId == id.Value && x.AcknowledgedUtc != null)
        .OrderBy(x => x.Id).Skip(skip ?? 0).Take(50).ToListAsync(ct);
    return Results.Ok(mails.Select(MailEncryption.View).ToArray());
});

authenticated.MapDelete("/mail/messages/{messageId:guid}", async (Guid messageId,
    ClaimsPrincipal user, AppDb db, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    if (id is null) return Results.Unauthorized();
    var message = await db.EncryptedInboundMails.SingleOrDefaultAsync(x =>
        x.Id == messageId && x.AccountId == id.Value, ct);
    if (message is null) return Results.NoContent(); // Retry after ACK is safe.
    // Do NOT erase a message merely because it was written to a local disk.
    // The Syncthing/Git replicas may still be offline. Acknowledgement moves
    // it into the recoverable encrypted archive, without changing ciphertext.
    if (message.AcknowledgedUtc is null)
    {
        message.AcknowledgedUtc = clock.GetUtcNow();
        await db.SaveChangesAsync(ct);
    }
    return Results.NoContent();
});

// Internal endpoints must be reachable only from the SMTP gateway network
// identity. The dedicated ingress token is NOT an administrator or account key.
bool MailGatewayAuthorized(HttpContext context)
{
    var expected = builder.Configuration["GTM_MAIL_INGRESS_TOKEN"];
    var supplied = context.Request.Headers["X-GTM-Mail-Token"].ToString();
    if (string.IsNullOrWhiteSpace(expected) || expected.Length < 32 ||
        string.IsNullOrWhiteSpace(supplied)) return false;
    return CryptographicOperations.FixedTimeEquals(
        System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(expected)),
        System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(supplied)));
}

app.MapGet("/internal/mail/recipient", async (
    string recipient, HttpContext context, AppDb db, CancellationToken ct) =>
{
    if (!MailGatewayAuthorized(context)) return Results.Unauthorized();
    if (!MailInboxPolicy.ValidRecipient(recipient,
            builder.Configuration["GTM_INBOUND_MAIL_DOMAIN"], out var localPart))
        return Results.NotFound();
    var inbox = await db.MailInboxes.AsNoTracking()
        .SingleOrDefaultAsync(x => x.LocalPart == localPart && x.Enabled, ct);
    var account = inbox is null ? null : await db.Accounts.FindAsync([inbox.AccountId], ct);
    if (account is null || !MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.NotFound();
    return Results.Ok(new { accepted = true });
});

app.MapPost("/internal/mail/receive", async (
    string recipient, HttpContext context, AppDb db, CancellationToken ct) =>
{
    if (!MailGatewayAuthorized(context)) return Results.Unauthorized();
    if (!MailInboxPolicy.ValidRecipient(recipient,
            builder.Configuration["GTM_INBOUND_MAIL_DOMAIN"], out var localPart))
        return Results.NotFound();
    var inbox = await db.MailInboxes.AsNoTracking()
        .SingleOrDefaultAsync(x => x.LocalPart == localPart && x.Enabled, ct);
    var account = inbox is null ? null : await db.Accounts.FindAsync([inbox.AccountId], ct);
    if (account is null || !MailInboxPolicy.IsEligible(account, clock.GetUtcNow()))
        return Results.NotFound();
    if (context.Request.ContentLength is > MailInboxPolicy.MaxRawMailBytes)
        return Results.StatusCode(413);
    await using var raw = new MemoryStream();
    var buffer = new byte[16384];
    int received;
    while ((received = await context.Request.Body.ReadAsync(buffer.AsMemory(), ct)) > 0)
    {
        if (raw.Length + received > MailInboxPolicy.MaxRawMailBytes)
            return Results.StatusCode(413);
        raw.Write(buffer, 0, received);
    }
    if (raw.Length == 0) return Results.BadRequest();
    byte[] bytes = raw.ToArray();
    var digest = Convert.ToHexString(
        System.Security.Cryptography.SHA256.HashData(bytes));
    if (await db.EncryptedInboundMails.AnyAsync(x =>
            x.AccountId == account.Id && x.DedupSha256 == digest, ct))
        return Results.Ok(new { duplicate = true });
    var now = clock.GetUtcNow();
    var messages = await db.EncryptedInboundMails.AsNoTracking()
        .Where(x => x.AccountId == account.Id)
        .Select(x => x.ReceivedUtc).ToListAsync(ct);
    if (messages.Count >= MailInboxPolicy.MaxRetainedMessages ||
        await db.EncryptedInboundMails.CountAsync(x =>
            x.AccountId == account.Id && x.AcknowledgedUtc == null, ct)
            >= MailInboxPolicy.MaxPendingMessages ||
        messages.Count(x => x >= now.AddDays(-1)) >= MailInboxPolicy.MaxDailyMessages)
        return Results.StatusCode(429);
    EncryptedInboundMail encrypted;
    try { encrypted = await MailEncryption.EncryptAsync(account.Id,
        inbox!.PublicKeyBase64, bytes, now, ct); }
    catch (Exception ex) when (ex is ArgumentException or MimeKit.ParseException)
    {
        return Results.BadRequest(new { error = "Invalid MIME message." });
    }
    finally { System.Security.Cryptography.CryptographicOperations.ZeroMemory(bytes); }
    db.EncryptedInboundMails.Add(encrypted);
    try { await db.SaveChangesAsync(ct); }
    catch (DbUpdateException) { return Results.Conflict(new { error = "Retry mail delivery." }); }
    return Results.Ok(new { queued = true });
});

authenticated.MapPost("/billing/checkout", async (CheckoutRequest request,
    ClaimsPrincipal user, AppDb db, StripeGateway stripe, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (account.EmailVerifiedUtc is null)
        return Results.Conflict(new { error = "Confirm your email before checkout." });
    var plan = (request.Plan ?? "").ToLowerInvariant();
    if (!PlanCatalog.IsValid(plan) || !PlanCatalog.ValidInterval(request.BillingInterval))
        return Results.BadRequest(new { error = "Unknown plan or billing interval." });
    if (!stripe.Configured) return Results.Problem("Stripe is not configured.", statusCode: 503);
    if (account.StripeStatus == "active" && account.PaidUntilUtc > clock.GetUtcNow())
        return Results.Conflict(new { error = "Subscription is active. Use the billing portal to change plans." });
    try { return Results.Ok(await stripe.CheckoutAsync(account, plan, request.BillingInterval, ct)); }
    catch (InvalidOperationException) { return Results.Problem("Checkout unavailable.", statusCode: 503); }
});

authenticated.MapPost("/billing/portal", async (ClaimsPrincipal user, AppDb db,
    StripeGateway stripe, CancellationToken ct) =>
{
    var id = AdminSecurity.UserId(user);
    var account = id is null ? null : await db.Accounts.FindAsync([id.Value], ct);
    if (account is null) return Results.NotFound();
    if (!stripe.Configured || account.StripeCustomerId is null)
        return Results.Problem("Customer portal unavailable.", statusCode: 503);
    try { return Results.Ok(await stripe.PortalAsync(account, ct)); }
    catch (InvalidOperationException) { return Results.Problem("Customer portal unavailable.", statusCode: 503); }
});

// Emergency/admin operations require a separate server-only secret, never a user account flag.
app.MapGet("/api/admin/accounts", async (string? email, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var query = db.Accounts.AsNoTracking();
    if (!string.IsNullOrWhiteSpace(email))
    {
        var normalized = email.Trim().ToLowerInvariant();
        query = query.Where(a => a.Email == normalized);
    }
    var accounts = await query.OrderBy(x => x.Email).Take(50).ToListAsync(ct);
    return Results.Ok(accounts.Select(a => PlanCatalog.View(a, clock.GetUtcNow())));
});

app.MapGet("/api/admin/accounts/{id:guid}/audit", async (Guid id, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var entries = await db.Audit.AsNoTracking()
        // SQLite EF cannot translate ORDER BY DateTimeOffset. Audit IDs are
        // monotonically increasing insert order and are indexed by the PK.
        .Where(x => x.AccountId == id).OrderByDescending(x => x.Id)
        .Take(50).ToListAsync(ct);
    return Results.Ok(entries);
});

app.MapPost("/api/admin/accounts/{id:guid}/extend-trial", async (
    Guid id, ExtendTrialRequest request, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    if (request.Days is < 1 or > 36500) return Results.BadRequest(new { error = "Invalid days." });
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var now = clock.GetUtcNow();
    var before = account.TrialUntilUtc;
    account.TrialUntilUtc = TrialPolicy.Extend(before, now, request.Days);
    db.Audit.Add(new AuditEntry
    {
        AccountId = id, Action = "extend-trial", Actor = "admin-token",
        OccurredUtc = now,
        Details = $"days={request.Days};before={before:O};after={account.TrialUntilUtc:O};reason={request.Reason?.Replace(';', ',').Substring(0, Math.Min(request.Reason.Length, 120))}"
    });
    await db.SaveChangesAsync(ct);
    return Results.Ok(PlanCatalog.View(account, now));
});

// A temporary discretionary capacity gift is independent of the Stripe price,
// must be audited and never rewrites a paid plan or the original quota.
app.MapPost("/api/admin/accounts/{id:guid}/temporary-quota", async (
    Guid id, TemporaryQuotaRequest request, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    if (request.ExtraGiB is < 1 or > TemporaryQuotaPolicy.MaxGiftGiB ||
        request.Days is < 1 or > TemporaryQuotaPolicy.MaxDays ||
        string.IsNullOrWhiteSpace(request.Reason) || request.Reason.Length > 200)
        return Results.BadRequest(new { error = "Provide 1..1024 GiB, 1..30 days and a reason (max 200 chars)." });
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var now = clock.GetUtcNow();
    try { TemporaryQuotaPolicy.Grant(account, request.ExtraGiB, request.Days, now); }
    catch (ArgumentOutOfRangeException) { return Results.BadRequest(new { error = "Invalid quota allowance." }); }
    db.Audit.Add(new AuditEntry {
        AccountId = id, Action = "temporary-quota-granted", Actor = "admin-token",
        OccurredUtc = now, Details = $"extraGiB={request.ExtraGiB};days={request.Days};until={account.GiftQuotaExpiresUtc:O};reason={request.Reason.Replace(';', ',')}"
    });
    await db.SaveChangesAsync(ct);
    return Results.Ok(PlanCatalog.View(account, now));
});

app.MapDelete("/api/admin/accounts/{id:guid}/temporary-quota", async (
    Guid id, HttpContext context, AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var now = clock.GetUtcNow();
    db.Audit.Add(new AuditEntry { AccountId = id, Action = "temporary-quota-revoked",
        Actor = "admin-token", OccurredUtc = now,
        Details = $"previousGiB={account.GiftQuotaGiB};previousUntil={account.GiftQuotaExpiresUtc:O}" });
    TemporaryQuotaPolicy.Revoke(account);
    await db.SaveChangesAsync(ct);
    return Results.Ok(PlanCatalog.View(account, now));
});


//// OPERATIONS: administrators see aggregate status, not ciphertext or tenant
// filesystem access. Incident holds can only be extended, never shortened.
app.MapGet("/api/admin/observability", async (
    HttpContext context, AppDb accounts, ProvisioningDb states, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var now = clock.GetUtcNow();
    var tenants = await states.Tenants.AsNoTracking()
        .Select(t => new { t.Status, t.UpdatedUtc }).ToListAsync(ct);
    var pendingTimes = await accounts.HistoryDeletionIntents.AsNoTracking()
        .Where(x => x.State == "Pending").Select(x => x.PurgeNotBeforeUtc).ToListAsync(ct);
    var holds = await accounts.Accounts.AsNoTracking()
        .Where(a => a.HistoryHoldUntilUtc != null)
        .Select(a => a.HistoryHoldUntilUtc).ToListAsync(ct);
    return Results.Ok(new {
        checkedUtc = now,
        accounts = await accounts.Accounts.CountAsync(ct),
        tenants = new {
            total = tenants.Count,
            ready = tenants.Count(t => t.Status == "Ready"),
            failed = tenants.Count(t => t.Status == "Failed"),
            provisioning = tenants.Count(t => t.Status is "Pending" or "Provisioning"),
            suspended = tenants.Count(t => t.Status == "Suspended"),
            staleReconciliation = tenants.Count(t => t.UpdatedUtc < now.AddMinutes(-5))
        },
        historyProtection = new {
            pendingDeletionIntents = pendingTimes.Count,
            deadlinesElapsedWithoutPurge = pendingTimes.Count(t => t < now),
            activeIncidentHolds = holds.Count(t => t > now),
            physicalPurgeEnabled = false,
            immutableRetentionVerified = false
        },
        resilience = new {
            kubernetesConfigured = builder.Configuration.GetValue<bool>("GTM_KUBE_ENABLED"),
            automaticCrossCloudFailoverEnabled = false,
            controlPlaneCrossCloudReplicationVerified = false
        }
    });
});

app.MapGet("/api/admin/accounts/{id:guid}/history-protection", async (
    Guid id, HttpContext context, AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var pending = await db.HistoryDeletionIntents.AsNoTracking()
        .Where(x => x.AccountId == id && x.State == "Pending")
        .Select(x => new { x.Id, x.RequestedUtc, x.PurgeNotBeforeUtc }).ToListAsync(ct);
    return Results.Ok(new {
        accountId = id, holdUntilUtc = account.HistoryHoldUntilUtc,
        pendingRequests = pending.Select(x => new { x.Id, x.RequestedUtc,
            effectiveNotBeforeUtc = account.HistoryHoldUntilUtc > x.PurgeNotBeforeUtc
                ? account.HistoryHoldUntilUtc : x.PurgeNotBeforeUtc }),
        physicalPurgeEnabled = false
    });
});

app.MapPost("/api/admin/accounts/{id:guid}/history-hold", async (
    Guid id, HistoryProtectionHoldRequest request, HttpContext context,
    AppDb db, CancellationToken ct) =>
{
    if (!AdminSecurity.IsAuthorized(context, builder.Configuration)) return Results.Unauthorized();
    if (request.Days is < 1 or > HistoryProtectionPolicy.MaxIncidentHoldDays ||
        string.IsNullOrWhiteSpace(request.Reason) || request.Reason.Length is < 5 or > 200)
        return Results.BadRequest(new { error = "Provide 1..365 days and a 5..200 character reason." });
    var account = await db.Accounts.FindAsync([id], ct);
    if (account is null) return Results.NotFound();
    var now = clock.GetUtcNow();
    var previous = account.HistoryHoldUntilUtc;
    var until = HistoryProtectionPolicy.ExtendTenantHold(account, now, request.Days);
    db.Audit.Add(new AuditEntry {
        AccountId = id, Action = "history-incident-hold-extended", Actor = "admin-token",
        OccurredUtc = now,
        Details = $"before={previous:O};after={until:O};days={request.Days};reason={request.Reason.Replace(';', ',')}"
    });
    await db.SaveChangesAsync(ct);
    return Results.Ok(new {
        accountId = id, holdUntilUtc = until,
        physicalPurgeEnabled = false,
        notice = "The hold is persisted; physical Git erasure is currently disabled."
    });
});

// Webhooks are authoritative only after signature verification. Do not trust redirect success URLs.
app.MapPost("/api/webhooks/stripe", async (HttpContext context,
    AppDb db, StripeGateway stripe, CancellationToken ct) =>
{
    if (context.Request.ContentLength is > 1048576)
        return Results.StatusCode(413);
    using var reader = new StreamReader(context.Request.Body, Encoding.UTF8);
    var payload = await reader.ReadToEndAsync(ct);
    var signature = context.Request.Headers["Stripe-Signature"].ToString();
    if (!stripe.VerifySignature(payload, signature, clock.GetUtcNow()))
        return Results.Unauthorized();

    using var document = JsonDocument.Parse(payload);
    var root = document.RootElement;
    string? eventId = StripeGateway.String(root, "id");
    string? type = StripeGateway.String(root, "type");
    var data = StripeGateway.Nested(root, "data", "object");
    if (string.IsNullOrEmpty(eventId) || eventId.Length > 255 || data.ValueKind != JsonValueKind.Object)
        return Results.BadRequest();
    if (await db.HandledStripeEvents.AnyAsync(x => x.Id == eventId, ct))
        return Results.Ok(new { duplicate = true });

    string? customerId = StripeGateway.String(data, "customer");
    string? subscriptionId = type == "checkout.session.completed"
        ? StripeGateway.String(data, "subscription")
        : StripeGateway.String(data, "id");
    var metadataId = StripeGateway.String(StripeGateway.Nested(data, "metadata"), "gtm_account_id");
    var checkoutId = type == "checkout.session.completed"
        ? StripeGateway.String(data, "client_reference_id") : null;
    Guid? accountId = Guid.TryParse(metadataId ?? checkoutId, out var parsed) ? parsed : null;
    Account? account = accountId is not null
        ? await db.Accounts.FindAsync([accountId.Value], ct) : null;
    if (account is null && subscriptionId is not null)
        account = await db.Accounts.FirstOrDefaultAsync(x => x.StripeSubscriptionId == subscriptionId, ct);
    if (account is null && customerId is not null)
        account = await db.Accounts.FirstOrDefaultAsync(x => x.StripeCustomerId == customerId, ct);

    if (account is not null)
    {
        if (type == "checkout.session.completed")
        {
            account.StripeCustomerId = customerId ?? account.StripeCustomerId;
            account.StripeSubscriptionId = subscriptionId ?? account.StripeSubscriptionId;
            // No entitlement granted by an unverified payment / mere checkout completion.
        }
        if (type is "customer.subscription.created" or "customer.subscription.updated")
        {
            account.StripeCustomerId = customerId ?? account.StripeCustomerId;
            account.StripeSubscriptionId = subscriptionId ?? account.StripeSubscriptionId;
            account.StripeStatus = StripeGateway.String(data, "status") ?? "unknown";
            if (account.StripeStatus == "active" && StripeGateway.PeriodEnd(data) is { } end)
                account.PaidUntilUtc = end;
            // A paid plan is selected only by a signed Stripe subscription event.
            var items = StripeGateway.Nested(data, "items", "data");
            if (account.StripeStatus == "active" && account.PaidUntilUtc > clock.GetUtcNow() &&
                items.ValueKind == JsonValueKind.Array)
                foreach (var item in items.EnumerateArray())
                {
                    var price = StripeGateway.String(StripeGateway.Nested(item, "price"), "id");
                    foreach (var plan in PlanCatalog.Quotas.Keys)
                        if (price is not null && (price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant()] ||
                            price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant() + "_MONTH"] ||
                            price == builder.Configuration["GTM_STRIPE_PRICE_" + plan.ToUpperInvariant() + "_YEAR"]))
                            account.Plan = plan;
                }
        }
        if (type == "customer.subscription.deleted")
        {
            account.StripeStatus = "canceled";
            account.PaidUntilUtc = clock.GetUtcNow();
        }
        if (type == "invoice.payment_failed")
            account.StripeStatus = "past_due";
        if (type == "invoice.paid")
        {
            // Also accept older and newer invoice subscription-reference shapes.
            var parentSubscription = StripeGateway.String(
                StripeGateway.Nested(data, "parent", "subscription_details"), "subscription");
            var invoiceSubscription = StripeGateway.String(data, "subscription") ?? parentSubscription;
            if (invoiceSubscription == account.StripeSubscriptionId)
            {
                var lines = StripeGateway.Nested(data, "lines", "data");
                if (lines.ValueKind == JsonValueKind.Array)
                    foreach (var line in lines.EnumerateArray())
                    {
                        var endJson = StripeGateway.Nested(line, "period", "end");
                        if (endJson.ValueKind == JsonValueKind.Number && endJson.TryGetInt64(out var unix))
                        {
                            var until = DateTimeOffset.FromUnixTimeSeconds(unix);
                            if (until > clock.GetUtcNow())
                            {
                                account.PaidUntilUtc = until;
                                account.StripeStatus = "active";
                            }
                        }
                    }
            }
        }
    }

    db.HandledStripeEvents.Add(new HandledStripeEvent { Id = eventId, HandledUtc = clock.GetUtcNow() });
    await db.SaveChangesAsync(ct);
    return Results.Ok();
});

app.Run();

public partial class Program { }
