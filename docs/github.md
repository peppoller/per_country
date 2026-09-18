# GitHub action

## Daily Sync Workflow

- via `.github/workflows/daily.yml`
- Runs at 06:15 UTC daily (cron: "15 6 * * *")
- Executes `python3 peppol_sync.py sync`
- Requires `contents: write` permission for GITHUB_TOKEN
- Commits and pushes changes to `extracts/`, `docs/`, and `log/` directories
- Creates `log/git_status.txt` with git status summary
- Creates `log/git_diff.txt` with detailed change statistics
- Commit message includes timestamp and version from `VERSION.md`
- Logs total execution time at the end of the workflow
- Takes approximately 5 minutes

## Pages Deployment

- via `.github/workflows/static.yml`
- Deploys `site/` directory to GitHub Pages
- Triggered on push to main branch when `site/**` files change
- Uses sparse checkout to only fetch the `site/` directory (optimizes checkout performance)
- Requires `contents: read`, `pages: write`, and `id-token: write` permissions

```yaml
      - name: "Checkout (sparse: site/)"
        uses: actions/checkout@v4
        with:
          fetch-depth: 1
          filter: 'blob:none'
          sparse-checkout: |
            site
          sparse-checkout-cone-mode: true
```

## GitHub action testing

```bash
# uses https://github.com/nektos/act
# on MacOS: brew install act

test_gh_action.sh
```
