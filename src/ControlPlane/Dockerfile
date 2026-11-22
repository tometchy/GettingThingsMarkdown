FROM mcr.microsoft.com/dotnet/sdk:10.0 AS backend-build
WORKDIR /source
COPY src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj src/ControlPlane/
RUN dotnet restore src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj
COPY src/ControlPlane/ src/ControlPlane/
RUN dotnet publish src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj -c Release -o /out --no-restore

# The account/subscription portal is the same Avalonia WebAssembly code used by
# the desktop application. Bundle the browser build with the Control Plane API
# so that public /app/ registration actually works on a fresh kind deployment.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS browser-build
WORKDIR /source
RUN dotnet workload install wasm-tools
COPY src/Automations/GettingThingsMarkdown.Automations.csproj src/Automations/
COPY src/Client/Shared/GettingThingsMarkdown.Client.Shared.csproj src/Client/Shared/
COPY src/Client/Browser/GettingThingsMarkdown.Client.Browser.csproj src/Client/Browser/
RUN dotnet restore src/Client/Browser/GettingThingsMarkdown.Client.Browser.csproj
COPY src/Automations/ src/Automations/
COPY src/Client/Shared/ src/Client/Shared/
COPY src/Client/Browser/ src/Client/Browser/
RUN dotnet publish src/Client/Browser/GettingThingsMarkdown.Client.Browser.csproj \
    -c Release -o /browserout --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=backend-build /out .
COPY --from=browser-build /browserout/wwwroot ./wwwroot/app/
RUN mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000:1000
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "GettingThingsMarkdown.ControlPlane.dll"]
