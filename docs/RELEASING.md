# Releases and Docker Hub

NASitron follows the release/build pipeline in
[Blockinator Web](https://github.com/StealthCat/blockinator-web/blob/main/.github/workflows/dockerhub.yml).
Images are built by GitHub Actions and pushed to `stealthcat128/nasitron` on Docker Hub.

## One-time setup

1. Create the `stealthcat128/nasitron` Docker Hub repository if it does not exist.
2. In NASitron's GitHub repository, add the Actions secret `DOCKERHUB_TOKEN` containing
   a Docker Hub access token for `stealthcat128` with write access to that image repository.
   Use the same secret name as Blockinator Web. GitHub repository secrets are scoped
   to each repository; configuring the secret on Blockinator Web alone does not supply
   it to NASitron. Do not put the token in source files or release notes.
3. Merge the workflow into `main` before publishing a release.

## Publish a stable release

1. Merge and test the source intended for release. Update the application's version
   and user-facing release notes as appropriate.
2. Tag that exact commit as `X.Y.Z` or `vX.Y.Z` and publish a non-draft, non-prerelease GitHub
   release for the tag.
3. Watch **Actions → Publish Docker Hub**.

The publisher accepts stable tags in `X.Y.Z` or `vX.Y.Z` form and rejects other
formats, drafts, and prereleases. It resolves the
tag to a commit SHA, then calls the existing test workflow with that exact SHA.
Both the application/Docker smoke tests and browser UI checks must pass before
the publisher logs into Docker Hub.

Validation uses the tagged application and packaged helper. The installer test
fixture is taken from the immutable publishing-workflow commit so its simulation
of root-owned directories works on unprivileged runners, including when validating
older tags. Other release source files are unchanged. The Docker image is built
from a separate clean checkout of the release commit.

Both tag forms publish the same Docker version (for example, `1.0.4` or
`v1.0.4` produces `stealthcat128/nasitron:1.0.4`).

The build pushes one multi-platform image for `linux/amd64` and `linux/arm64` as
`stealthcat128/nasitron:X.Y.Z`, with source/version/revision OCI labels and GitHub
Actions build caching. Publishing is serialized and running publishes are not cancelled.

After publishing the versioned image, the workflow checks GitHub's current latest
stable release. It updates `stealthcat128/nasitron:latest` to the built image digest
only when that release's tag matches this build. Rebuilding an older release therefore
does not move `latest` backwards. The published digest appears in the run summary.

## Retry an existing release

From **Actions → Publish Docker Hub → Run workflow**, select `main` and enter the
already published stable release tag. Manual runs from other branches are skipped.
The run rebuilds and revalidates the tagged commit, rather than building the current
branch tip. A source push or an unpublished Git tag alone does not publish an image.

## Verify publication

On a Docker host:

```bash
docker buildx imagetools inspect stealthcat128/nasitron:X.Y.Z
docker buildx imagetools inspect stealthcat128/nasitron:latest
```

Replace `X.Y.Z` with the actual published version. Verify both architectures and
the expected digest. Document a version as available only after the publish succeeds.
