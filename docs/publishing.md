# Publish Ratchet on GitHub

The public repository holds the source and build instructions. Each GitHub Release points to a Git tag and offers the prebuilt Ubuntu package and its checksum. Ratchet’s package uses separately configured model weights and local receiver settings.

## 1. Prepare the source

Choose an [open-source license](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) for the Ratchet code you own and put it in a root `LICENSE` file. Keep the upstream llama.cpp license and the dependency notices included in the package. Install [GitHub CLI](https://cli.github.com/) for the publishing steps.

From the project directory, initialize Git if needed. The existing `llama.cpp` checkout can become a submodule at its current commit. Register it before staging the rest of Ratchet.

```bash
git init -b main
git submodule add https://github.com/ggml-org/llama.cpp.git llama.cpp
git add -A
git diff --cached --stat
git diff --cached
```

Run `git init` only for a new top-level repository. If `llama.cpp` is already a registered submodule, keep its entry. Check that the staged `llama.cpp` entry points to a commit available at its upstream URL. The submodule lets a contributor fetch the same dependency with `git clone --recurse-submodules`.

Review staged files and any earlier commits before publishing. Keep your GGUF, certificates, private keys, receiver tokens, SQLite telemetry, virtual environment and build outputs local. The repository’s `.gitignore` covers their usual paths. Git keeps files already recorded in history even after you add ignore rules. Check the public defaults in `smartbackend/config.yaml` for account details and credentials.

## 2. Publish the code

Replace `YOUR_GITHUB_USERNAME` with your account or organization name. This example creates a new public repository and pushes the reviewed source.

```bash
repo=YOUR_GITHUB_USERNAME/Ratchet
git commit -m "Publish Ratchet source"
gh auth login
gh repo create "$repo" --public --source=. --remote=origin --push
```

If you already created the GitHub repository, configure `origin` for it and push your branch. [GitHub CLI repository creation](https://cli.github.com/manual/gh_repo_create)

## 3. Attach a prebuilt release

Build from the source revision you intend to tag on Ubuntu 24.04 amd64. The [package build guide](manual-start.md#build-the-ubuntu-package) lists the dependencies. Test the package on the Ubuntu and CPU configurations you list in the release notes. Check the app-menu launch, Talk with Doc, the receiver and shutdown.

```bash
version=0.1.2
tag="v$version"
asset="ratchet_${version}_amd64.deb"

./packaging/build-deb "$version"
(cd dist && sha256sum "$asset" > "$asset.sha256")
git tag -a "$tag" -m "Ratchet $version"
git push origin "$tag"
gh release create "$tag" "dist/$asset" "dist/$asset.sha256" \
  --verify-tag --draft --title "Ratchet $version" --generate-notes
```

Review the draft and add the supported platform, the model file location and the receiver setup link. Publish the release from GitHub or run `gh release edit "$tag" --draft=false`. The public download URLs work after publication. [Create a release](https://cli.github.com/manual/gh_release_create) · [Edit a release](https://cli.github.com/manual/gh_release_edit)

## 4. Give users the download command

For a public release, users can download the package without GitHub CLI. Replace the owner and version with the published values. [GitHub release download links](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)

```bash
owner=YOUR_GITHUB_USERNAME
version=0.1.2
asset="ratchet_${version}_amd64.deb"
url="https://github.com/$owner/Ratchet/releases/download/v$version"

curl --fail --location --remote-name "$url/$asset"
curl --fail --location --remote-name "$url/$asset.sha256"
sha256sum -c "$asset.sha256" && sudo apt install "./$asset"
```

After installing, users can open Ratchet from the app menu. See the [model and receiver setup](../README.md#install-the-ubuntu-app) for first use.
