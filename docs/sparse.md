# Git Sparse Checkout

If you are only interested in the data _for a specific country_, you can use Git's `sparse-checkout` feature to download only the files for that country. This can save a significant amount of disk space and time.

Here is how you can do a sparse checkout for just the data for Norway (`NO`).

## Step-by-step guide

1.  **Clone the repository without checking out any files:**

    This special clone command creates the `.git` directory with all the repository history but doesn't pull the actual files yet.

    ```bash
    git clone --filter=blob:none --no-checkout https://github.com/peppoller/per_country.git
    cd per_country
    ```

2.  **Enable sparse checkout and define the directory you want:**

    This command tells Git that you only want to have the files in the `extracts/NO/` directory.

    ```bash
    git sparse-checkout set extracts/NO/
    ```

3.  **Pull the files:**

    Now, Git will pull only the files that match the path you specified.

    ```bash
    git checkout
    ```

After these steps, your `extracts/` directory will contain only the `NO` subdirectory.

## Adding more directories

If you want to add more directories later, you can use the `add` command:

```bash
git sparse-checkout add extracts/SE/
git checkout
```

## Reverting to a full checkout

If you want to disable sparse checkout and download all the files in the repository, you can use the `disable` command:

```bash
git sparse-checkout disable
git checkout
```
