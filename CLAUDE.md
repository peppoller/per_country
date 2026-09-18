# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PEPPOL Per Country is a Python-based tool that synchronizes PEPPOL business directory exports by country. It downloads the massive XML export from `directory.peppol.eu`, processes it using streaming XML parsing, and splits business cards into country-specific files stored in git.

**Core Technologies:**
- Python 3.x with `lxml` for XML processing
- GitHub Actions for daily automated sync
- MkDocs with Material theme for documentation

## Development Commands

### Running the sync tool

```bash
# Full sync: download + process XML (recommended for first run)
python3 peppol_sync.py sync

# Force re-download even if file exists
python3 peppol_sync.py sync -F

# Keep temporary files for debugging
python3 peppol_sync.py sync -K

# Don't delete existing extracts before processing
python3 peppol_sync.py sync -C

# Verbose output
python3 peppol_sync.py sync -V
```

### Utility commands

```bash
# Download XML only (no processing)
python3 peppol_sync.py download

# Check configuration
python3 peppol_sync.py check

# Show largest output files
python3 peppol_sync.py huge -n 20

# Custom max file size (default: 2MB)
python3 peppol_sync.py sync -M 1000000

# Single process, no worker pool (default: CPU count - 1 workers)
python3 peppol_sync.py sync -j 1
```

### Documentation

```bash
# Build and serve docs locally
mkdocs serve

# Build static site (output to site/)
mkdocs build
```

### Dependencies

```bash
# Install required Python package
pip install lxml
```

## Architecture

### Main Script: peppol_sync.py

The `PeppolSync` class handles the entire workflow:

1. **Download Phase** (`download_xml()`)
   - Streams XML from `https://directory.peppol.eu/export/businesscards`
   - Saves to `tmp/directory-export-business-cards.xml`
   - Shows progress every 100MB
   - Skips download if file exists (override with `-F`)

2. **Processing Phase** (`process_xml()`)
   - Uses text-based chunking (1MB chunks) for memory efficiency, scanning the buffer by index
   - Parses and pretty-prints cards in a worker pool (`-j`, default: CPU count - 1); the main process keeps input order and does all writing
   - Parses business cards with `lxml.etree` for fast XML handling
   - Extracts country code from `<entity countrycode="XX">`
   - Extracts registration date from `<regdate>` for statistics
   - Writes pretty-printed XML to country/month directories

3. **File Splitting Logic** (in `process_xml()`)
   - Groups cards per country and registration month (`<regdate>`): `extracts/BE/2026-08/`; cards without a registration date go to `extracts/BE/0000-00/`
   - Within a month directory a card always lands in the same file: `crc32(participant id) mod N`, named `business-cards.000001.xml` ... `business-cards.00000N.xml`
   - N per bucket is read back from the previous run's extracts (`plan_partitions()`, before cleanup) and only doubled or halved when files average more than 1.5x or less than 0.35x `max_bytes` (default: 2MB), so file sizes float around the target and a bucket is reshuffled only on a rescale
   - A bucket seen for the first time starts with the N of that country's latest month, so a new month begins with many small files instead of one huge one
   - Stable partition means a changed, added or deleted card touches exactly one file; daily commits stay small and the repo grows slowly
   - Output is buffered per file in memory and flushed in 128KB chunks with a short open/append/close, so no file handles stay open and there is no descriptor-limit concern
   - Automatically creates header and footer tags for valid XML

4. **Report Generation** (`generate_report()`)
   - Creates `docs/report.md` with country statistics
   - Shows month count, file count, card count, and size per country

### Output Structure

```
extracts/
├── AT/
│   ├── 0000-00/                     # cards without <regdate>
│   │   └── business-cards.000001.xml
│   ├── 2025-11/
│   │   └── business-cards.000001.xml
│   └── 2026-08/
│       ├── business-cards.000001.xml
│       └── business-cards.000002.xml
├── BE/
│   ├── 2026-08/
│   │   ├── business-cards.000001.xml
│   │   └── ...
│   └── ...
docs/report.md
log/peppol_sync.log
```

### GitHub Actions

**Daily Sync Workflow** (`.github/workflows/daily.yml`)
- Runs at 09:15 UTC daily
- Executes `python3 peppol_sync.py sync -V`
- Commits and pushes changes to `extracts/` automatically
- Creates `log/git_diff.txt` and `log/git_status.txt` with change summary

**Pages Deployment** (`.github/workflows/static.yml`)
- Deploys `site/` directory to GitHub Pages
- Triggered on push to main branch

## Key Implementation Details

### Memory-Efficient XML Processing

The script processes multi-GB XML files without loading everything into memory:
- Reads in 1MB text chunks
- Splits on `</businesscard>` delimiter
- Parses individual cards with lxml
- Uses streaming writes to output files

### Card Conversion

`convert_card()` is a module-level function (so worker processes can run it). It parses one
card with lxml, extracts the country code from `<entity countrycode="XX">`, the registration
date from `<regdate>`, the entity name, and returns the pretty-printed card as UTF-8 bytes.
It also returns the crc32 of the participant id, which the main process uses as the
partition key. Output files are written in binary mode; `tell()` on a text-mode file was
a measurable per-card cost, and so was keeping thousands of buffered handles open.

### Partition Stability

There is no size-based rotation any more. Changing `max_bytes` or the rescale thresholds
reshuffles buckets whose average file size falls outside the new band, which produces one
large commit. Deleting a bucket directory makes the next run start it from the country's
latest-month N. The hash is `zlib.crc32`, chosen because it is stable across Python versions
(the built-in `hash()` is salted per process).

### Cleanup Behavior

- **Temporary files** (`tmp/`): Deleted after processing by default (keep with `-K`)
- **Extract files** (`extracts/**/*.xml`): Deleted before each sync by default (preserve with `-C`)
- **Log file** (`log/peppol_sync.log`): Overwritten on each run

## Version Management

Version is stored in `VERSION.md` and updated via commit messages like `setver: set version to 0.1.12`. There is no automated version bumping script in the repository.

## Documentation Site

Uses MkDocs with Material theme. Configuration in `mkdocs.yml`:
- Main docs in `docs/` directory
- Documentation is deployed to GitHub Pages via the static workflow
- `README.md` is a symlink to `docs/index.md`
