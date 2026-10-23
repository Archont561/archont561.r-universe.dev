# Archont561 R-Universe

[![playtest status badge](https://archont561.r-universe.dev/badges/playtest)](https://archont561.r-universe.dev/playtest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Personal CRAN-like package repository for [@Archont561](https://github.com/Archont561), powered by [R-universe](https://r-universe.dev).

This universe provides automated, cross-platform builds and pre-compiled binaries (Linux, macOS, Windows) for R packages developed in this ecosystem.

---

## Published Packages

| Package | Version | Description | Source | Build Status |
| :--- | :--- | :--- | :--- | :--- |
| [**`playtest`**](https://archont561.r-universe.dev/playtest) | `0.1.0` | Playwright-style testing framework with dependency-injected fixtures, LIFO teardown, BDD scenarios, and property-based testing | [`Archont561/playtest` (`r/`)](https://github.com/Archont561/playtest) | [![status](https://archont561.r-universe.dev/badges/playtest)](https://archont561.r-universe.dev/playtest) |

---

## Installation

### In R Console

Enable the universe in your R options to install `playtest` and any future packages as standard CRAN packages:

```r
# Enable the Archont561 universe alongside CRAN
options(repos = c(
  archont561 = "https://archont561.r-universe.dev",
  CRAN = "https://cloud.r-project.org"
))

# Download and install pre-compiled binaries or source
install.packages("playtest")
```

### Using `pak`

```r
# Install directly from the universe
pak::repo_add(archont561 = "https://archont561.r-universe.dev")
pak::pkg_install("playtest")

# Or directly from the GitHub repository
pak::pkg_install("Archont561/playtest?subdir=r")
```

### Using `remotes`

```r
remotes::install_github("Archont561/playtest", subdir = "r")
```

---

## Registry Configuration

The package catalog is configured in [`packages.json`](./packages.json):

```json
[
  {
    "package": "playtest",
    "url": "https://github.com/Archont561/playtest",
    "branch": "*release",
    "subdir": "r"
  }
]
```

- **`subdir: "r"`**: Specifies the relative directory of the R package root within the multi-language repository.
- **`branch: "*release"`**: Instructs R-universe to automatically track and build tagged GitHub releases (`v*`, `r-v*`).

---

## API & Endpoints

- **Universe Web Portal**: [https://archont561.r-universe.dev](https://archont561.r-universe.dev)
- **Package Details**: [https://archont561.r-universe.dev/playtest](https://archont561.r-universe.dev/playtest)
- **JSON API (Catalog)**: [https://archont561.r-universe.dev/api/packages](https://archont561.r-universe.dev/api/packages)
- **JSON API (`playtest`)**: [https://archont561.r-universe.dev/api/packages/playtest](https://archont561.r-universe.dev/api/packages/playtest)
- **Build Badges**: `https://archont561.r-universe.dev/badges/:package`
