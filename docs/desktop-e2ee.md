# Desktop E2EE and local key management

The Avalonia desktop host uses `LocalSyncthingEngine` to spawn a **bundled**
Syncthing binary. It is packaged with the application, not downloaded during
runtime, and no separate Syncthing install is required.

The local client:
1. Launches Syncthing on a random localhost GUI port, with a random API key.
2. Generates a native Syncthing identity under the user's application profile.
3. Asks the signed-in control plane to register only its public Device ID.
4. Marks the remote server as **untrusted**, using Syncthing-native folder
   encryption and a 32-byte random password generated on the device.
5. Keeps the folder password only in `~/.local/share/GettingThingsMarkdown/keys/`
   on Linux, with Unix permissions `0600`. The backend never receives it.
6. Sends changes to a server `Receive Encrypted` folder.

**Limitations and release blockers**:
- The local secret is only filesystem-permission protected. It is not yet
  integrated with the OS keyring, an audited KDF, or a securely implemented
  multi-device recovery process. The UI offers manual recovery-code export
  until such a process exists.
- On local kind, the server's Syncthing peer port must be exposed by
  `./dev.sh expose-sync ACCOUNT_UUID 22000`. This is a developer-only
  workaround, not production networking.
- macOS, Windows and mobile bundling, native engine bridges and iOS lifecycle
  remain untested.
- Confirm two-way interoperability and ciphertext-only server content before
  using sensitive files.
