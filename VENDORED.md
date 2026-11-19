# Vendored code

## `cktools_data/`

The `cktools_data/` package is vendored verbatim from kteam-tools:

    kteam-tools/cktools/cktools-data/src/cktools_data

- Upstream repo: https://kernel.ubuntu.com/forgejo/kernel/kteam-tools
- Vendored at commit: `7aed5b4fa7dd8f74fb30ce5f566619d1e05dccf5`

It provides `KernelTargets` (the list of kernel source packages and projects to
scan) and `KernelSeries` (which fetches `kernel-series.json.gz` from
https://kernel.ubuntu.com/info at runtime to enumerate series and sources).

### Refreshing

Copy the whole package over the existing one and update the commit hash above:

    rm -rf cktools_data
    cp -r /path/to/kteam-tools/cktools/cktools-data/src/cktools_data ./cktools_data
    rm -rf cktools_data/__pycache__

The package is imported directly off the branch root (`from cktools_data import
KernelTargets`); no build/install step is required.
