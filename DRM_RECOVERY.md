# DRM Recovery Runbook

This repository does not contain DRM content keys, private keys, KMS material, or
license server configuration. The desktop client detects DRM media and delegates
playback to the site player through WebView2.

## What To Check

- DRM provider console or tenant admin portal.
- KMS/HSM key inventory and key version history.
- CI/CD secret stores and deployment variables.
- Production, staging, and backup environment configuration.
- Infrastructure-as-code repositories that define license server settings.
- Incident backups, secret snapshots, or escrow procedures.

Do not attempt to extract content keys from end-user clients, browser caches,
network captures, or encrypted media files. Use provider-supported recovery or
rotation paths.

## If Keys Are Recoverable

1. Verify ownership and administrative access.
2. Restore the secret into the approved secret manager.
3. Rotate credentials if exposure is possible.
4. Restart or redeploy only the services that read the restored secret.
5. Test playback with an authorized account on staging before production.
6. Record the key id, rotation time, and affected content ids in the incident log.

## If Keys Are Not Recoverable

1. Generate new DRM keys through the approved KMS/HSM or DRM provider.
2. Repackage content from original source audio using the new key material.
3. Update the license server mapping from content id or key id to the new key.
4. Invalidate stale manifests and CDN objects.
5. Publish updated manifests.
6. Test playback on clean devices and accounts.
7. Keep the old key ids blocked or retired according to provider guidance.

## Client Impact

The current client should not need DRM key material. If playback fails after
rotation, verify that:

- The logged-in account can play the content in the official web player.
- WebView2 Runtime is installed.
- The app has a valid login cookie.
- CDN and manifest caches have expired or been purged.
- The license server accepts the new key id mapping.
