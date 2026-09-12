# Published Linux images

Repository: https://github.com/tengso/triflection-team-builder

Release `0.1.0` is public and supports anonymous pulls. The installer pins the
verified image digests in `src/team_builder/resources/images.json`. The release
includes an installer wheel and an `images.json` manifest with the build commit.

| Image | Contents |
| --- | --- |
| `ghcr.io/tengso/triflection-team-builder/buzz` | Buzz relay, administration CLI, and agent CLI |
| `ghcr.io/tengso/triflection-team-builder/hermes` | Hermes, Buzz CLI, Team Builder manager/MCP/worker, and checked adapter patch |

The initial platform is `linux/amd64`. Both images contain source revision labels,
upstream license files, and source provenance JSON under `/usr/share/team-builder`.
They contain no installation identities, workspaces, provider credentials, or old
builder code. All agents and the manager use the same Hermes runtime image.

## Publish a release

Run the **Publish images** workflow with a new semantic version:

```sh
gh workflow run publish-images.yml --repo tengso/triflection-team-builder -f version=0.1.1
```

The workflow downloads the exact upstream commits in `sources.json`, applies the
versioned Buzz patch, builds the binaries and runtime, and uploads candidate images.
It pulls them by digest and runs a fresh isolated Compose bootstrap plus an
identity-preserving setup retry. This check uses a dummy provider credential and
makes no model calls. Only passing images receive the release tags. Existing release
tags are rejected. The `images-VERSION` artifact records the immutable digests and
builder source commit. GitHub produces build provenance and SBOM attestations.

The workflow uses its repository-scoped `GITHUB_TOKEN` with `packages:write`;
maintainers do not need to store a Docker Hub password or personal publishing token.
GHCR initially creates packages as private: set each package's visibility to public
for anonymous installs. No secrets should be added to either Docker build context.

After validation, update `src/team_builder/resources/images.json` with the released
image digests, then build the installer with `uv build --wheel` and publish it with
the manifest in a GitHub release. This pins new installations; existing state
remains unchanged.

## Reproduce locally

On a Linux x86-64 Docker host, install this project and run:

```sh
python packaging/prepare_images.py
docker build -f packaging/Dockerfile.buzz -t team-builder-buzz:local .image-build/buzz
docker build -f packaging/Dockerfile.hermes \
  --build-arg BUZZ_IMAGE=team-builder-buzz:local \
  -t team-builder-hermes:local .image-build/hermes
BUZZ_IMAGE=team-builder-buzz:local HERMES_IMAGE=team-builder-hermes:local \
  python tests/integration/bootstrap_images.py
```

The check accepts local image tags or pulls published digest references if they
are not already present. The build context is deliberately an
allowlist assembled by `prepare_images.py`. Remove `.image-build/` before preparing
a new context. Upstream Rust and Hermes dependencies use their committed lockfiles;
Ubuntu packages and additional builder dependencies are resolved during image build.
The published image digests, rather than a claim of byte-identical future rebuilds,
are the installation reproducibility boundary.

The Buzz patch preserves the tested metadata and external-repository behavior.
The Hermes patch in `src/team_builder/resources/patch_hermes.py` supplies verified
message context to COA and enables owner messages in its office without mentions.
Patches fail on incompatible upstream source instead of silently dropping behavior.
