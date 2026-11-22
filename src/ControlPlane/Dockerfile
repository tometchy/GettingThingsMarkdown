FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /source
COPY src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj src/ControlPlane/
RUN dotnet restore src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj
COPY src/ControlPlane/ src/ControlPlane/
RUN dotnet publish src/ControlPlane/GettingThingsMarkdown.ControlPlane.csproj -c Release -o /out --no-restore
FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /out .
RUN mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000:1000
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "GettingThingsMarkdown.ControlPlane.dll"]
