# Releasing Sheriff

Sheriff follows [Semantic Versioning](https://semver.org/). Pick the version from what users notice:

| Change | Version |
|---|---|
| A fix that changes no setting, file or permission | patch, `1.0.x` |
| A new feature, setting or template change that existing installs keep working without (say under **Upgrading** what `gh sheriff install` adds) | minor, `1.x.0` |
| A change existing installs must run `gh sheriff install` for to keep working, a changed permission on a job an old template already runs, or a removed setting | major, `vN.0.0` (a new moving tag `vN`) |

A new permission asked for only by jobs or inputs that a new template adds ships in `v1`, because old templates never
run those jobs.

## Steps

1. **Pull request.** On a branch, write the change with its tests, then:
   - move the `## [Unreleased]` notes in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD` (UTC date), grouped as
     Added, Changed, Fixed, Removed or Breaking, plus **Upgrading** when users must act;
   - add the `[X.Y.Z]` compare link at the bottom and point `[Unreleased]` at `vX.Y.Z...HEAD`;
   - set `VERSION` in `gh-sheriff` to `X.Y.Z`. `tests/changelog.test.py` fails until the changelog has a dated entry for
     it.
2. **Rehearse.** Point a throwaway repository's `sheriff.yml` at the branch (`@<branch>` instead of `@v1`) and run the
   changed paths for real: open issues, comment, push pull requests. Record what you ran in the pull request.
3. **Merge** once CI (tests, actionlint, zizmor) is green.
4. **Tag and publish** from the merge commit:

   ```sh
   git switch main && git pull --ff-only
   python3 scripts/changelog.py notes X.Y.Z | gh release create vX.Y.Z --target "$(git rev-parse HEAD)" \
     --title "vX.Y.Z" --notes-file -
   ```

5. **Move the major tag** after the release is published, so `@v1` users get it:

   ```sh
   git fetch --tags && git tag -f v1 vX.Y.Z && git push -f origin v1
   ```

   The `v*` tags are protected by the "Release tags" ruleset; only admins can create or move them.
6. **Check a consumer.** Upgrade the CLI (`gh extension upgrade sheriff`), run `gh sheriff doctor --repo <repo>` in a
   repository that uses Sheriff, and run `gh sheriff install` there when the template changed.
