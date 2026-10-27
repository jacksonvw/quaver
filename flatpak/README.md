# The Flatpaks

`com.jacksonv.Quaver.yml` and `com.jacksonv.Minim.yml` build the two apps on
KDE's Flatpak runtime; the main README has the build commands. Quaver's
Flatpak carries Minim too, for "Open in Minim".

## Python packages

The packages from PyPI are pinned, dependencies included, in
`requirements-minim.txt` (Minim, and media_core's share of Quaver) and
`requirements-quaver.txt` (the rest of Quaver). `python3-minim-deps.yaml` and
`python3-quaver-deps.yaml` are generated from them by flatpak-builder-tools'
[flatpak-pip-generator](https://github.com/flatpak/flatpak-builder-tools/tree/master/pip),
with a checksum for every download, so the build itself needs no network.
After changing a pin, regenerate both from the repository root, in the SDK the
apps build with:

```bash
mkdir -p build/pipgen
curl -fsSLo build/pipgen/flatpak-pip-generator.py \
    https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator.py
flatpak run --share=network --filesystem="$PWD" --command=sh org.kde.Sdk//6.11 -c '
    cd flatpak
    python3 -m pip install -q --target ../build/pipgen/deps packaging requirements-parser pyyaml
    for app in minim quaver; do
        PYTHONPATH=../build/pipgen/deps python3 ../build/pipgen/flatpak-pip-generator.py \
            --yaml --requirements-file requirements-$app.txt --output python3-$app-deps
    done'
```

yt-dlp is the one to bump often, since YouTube changes break older versions.
