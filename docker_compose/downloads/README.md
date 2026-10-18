# Desktop app downloads

What the cloud gateway serves at `/downloads/`: the installers of the Sati Fleet desktop app and `latest.json`, the release
manifest the app and the web client's Settings read. Nothing here is committed except this file (see `.gitignore`).

Publish a release with `sati-client/electron/publish-release.js` (it copies the installers here and updates `latest.json`):

    node sati-client/electron/publish-release.js --to cloud_server/docker_compose/downloads \
        --notes "What changed" sati-client/electron/dist/Sati-Fleet-0.1.0-linux-x86_64.AppImage

Design: `sati-client/docs/RELEASE_AND_UPDATES.md`.
