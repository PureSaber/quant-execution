# Repository governance

This repository uses GitHub Rulesets as the platform-enforced boundary for its default branch and release tags.

- Changes to the default branch must arrive through a pull request and pass every required GitHub Actions check against the latest default branch.
- Force pushes and deletion of the default branch are prohibited.
- Existing `v*` release tags cannot be updated or deleted.
- The required approval count is currently zero because this is a single-maintainer account. It must be raised to one independent approval when a second trusted maintainer is available and can review without creating a merge deadlock.
- `CODEOWNERS`, when introduced, routes responsibility and does not represent independent approval by itself.

## Break glass

There is no standing bypass actor. In a genuine incident, the repository owner may temporarily disable only the affected Ruleset. Before doing so, open an audit issue recording the reason, scope, expected duration, and recovery plan. Record the Ruleset JSON before and after the change, restore `active` enforcement immediately after the emergency action, rerun the required checks, and close the issue only after the effective rules are verified.

