# Bundled Waggle Runtime

Release builds copy one signed `waggle-server` executable plus PyInstaller
`--onedir` support files into each target directory:

- `darwin-arm64/waggle-server`
- `darwin-x86_64/waggle-server`
- `linux-x86_64/waggle-server`
- `linux-aarch64/waggle-server`
- `win32-x86_64/waggle-server.exe`

The source tree intentionally does not commit generated runtime artifacts. Use
`python scripts/build_codex_plugin_runtime.py --build-current` on a native
runner to build the current platform artifact, then run
`python scripts/build_codex_plugin_runtime.py --require-artifacts` in the
release pipeline after all artifacts have been assembled.
