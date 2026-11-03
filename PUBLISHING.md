# Publishing `django-smstunnel` to PyPI

The package is published by GitHub Actions with **PyPI Trusted Publishing**: PyPI trusts the workflow
`.github/workflows/publish.yml` in the repository `narcisbodea/django-smstunnel`, in the GitHub environment `pypi`.
There is no API token or password anywhere: not in GitHub secrets, not on a computer. Each run gets a short-lived
token from GitHub (OIDC) that PyPI accepts only from that workflow, and the upload gets signed attestations (PEP 740)
automatically. It is the same setup as `smstunnel`, the Python client this package depends on.

The name `django-smstunnel` was free on PyPI on 3 November 2026 (`https://pypi.org/pypi/django-smstunnel/json`
answered 404; PyPI treats `django_smstunnel` as the same name). A pending publisher does **not** reserve it: if
someone else uploads a project with that name first, the pending publisher stops working. So do steps 1 to 3 and the
first release close together.

The `django-` prefix is allowed by the Django trademark license (<https://www.djangoproject.com/trademarks/>,
section 2): a library that extends Django may be called "django-foo" when it is under an OSI-approved license (MIT)
and does not claim to be official or endorsed by the Django Software Foundation.

PyPI has no review: a release is public a minute after the workflow finishes.

## 1. GitHub: the repository and the `pypi` environment (once)

1. Create the public repository `narcisbodea/django-smstunnel` and push this folder to its `main` branch:

   ```bash
   git remote add origin https://github.com/narcisbodea/django-smstunnel.git
   git push -u origin main
   ```

2. Check that the **CI** workflow passes on the **Actions** tab (every supported Python and Django pair, lint, types,
   build).
3. In the repository, open **Settings → Environments → New environment**, name it `pypi`, and save.
4. Recommended, in the same environment:
   - **Required reviewers**: add yourself. Every publish then waits for your click on **Review deployments → Approve**.
   - **Deployment branches and tags → Selected branches and tags → Add rule**: tag pattern `v*`.

## 2. PyPI: the pending publisher (once)

Sign in to PyPI with the account that owns `smstunnel` (the account "smstunnel"; it already has two-factor
authentication).

1. Open <https://pypi.org/manage/account/publishing/> (**Your account → Publishing**).
2. Under **Add a new pending publisher**, choose **GitHub** and fill in exactly:

   | Field | Value |
   | --- | --- |
   | PyPI Project Name | `django-smstunnel` |
   | Owner | `narcisbodea` |
   | Repository name | `django-smstunnel` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. Click **Add**. After the first successful upload it becomes the project's normal trusted publisher; nothing else to
   set up for later releases.

## 3. Release

1. Make sure the version in `src/django_smstunnel/_version.py` and the top of `CHANGELOG.md` are right (1.0.0 now).
2. On GitHub: **Releases → Draft a new release → Choose a tag**, type `v1.0.0` (create it on `main`), title `1.0.0`,
   paste the changelog entry, and **Publish release**. Or from a terminal with the GitHub CLI:

   ```bash
   gh release create v1.0.0 --repo narcisbodea/django-smstunnel --target main --title 1.0.0 --notes-file - <<'EOF'
   First release. See CHANGELOG.md.
   EOF
   ```

3. The **Publish** workflow runs: lint, types, tests, a check that the tag matches the version, the build and
   `twine check`. Then the **Publish to PyPI** job waits for your approval (if you set reviewers) and uploads.
4. Check <https://pypi.org/project/django-smstunnel/> and try it in a fresh virtual environment:

   ```bash
   python -m venv /tmp/try && . /tmp/try/bin/activate
   pip install "django-smstunnel[two-factor]"
   python -c "import django_smstunnel; print(django_smstunnel.__version__)"
   ```

For the next versions: change `_version.py` and `CHANGELOG.md`, commit, push, and publish a release tagged
`v<version>`. A version can never be uploaded twice, so a fix always needs a new version number.

## Optional: a dry run on TestPyPI first

TestPyPI is a separate site with a separate account.

1. Create an account at <https://test.pypi.org/account/register/> (with two-factor authentication) and add a pending
   publisher there with the same values, except **Environment name** `testpypi`.
2. In GitHub, add a second environment named `testpypi`.
3. In the repository: **Actions → Publish → Run workflow** (on `main`). It runs the same checks and uploads to
   <https://test.pypi.org/project/django-smstunnel/>.
4. Try it. Its dependencies (Django, smstunnel) come from the real PyPI:

   ```bash
   pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ django-smstunnel
   ```

## If the upload is refused

- `invalid-publisher`: the owner, repository, workflow file name or environment name on PyPI differs from the run.
  They must match exactly (`publish.yml`, not the workflow's display name).
- `File already exists`: that version is already on PyPI. Raise the version.
- The **Publish to PyPI** job never starts: approve the deployment, or check the environment's tag rule.

## What stays manual

Only the steps above: creating the GitHub repository and its environment, adding the pending publisher, and
publishing a release. Nothing in this repository needs a secret.

## Optional: being found from django-two-factor-auth

django-two-factor-auth's documentation lists only its own providers (Twilio and a fake one for development). A docs
change pointing to third-party providers would be a contribution to a Jazzband project. Its `CONTRIBUTING.rst` (read on
3 November 2026) asks for an issue or a pull request with tests, under the Jazzband code of conduct and guidelines
(<https://jazzband.co/about/guidelines>); it says nothing about AI-written code, and the repository has no
`AGENTS.md`. Write such a change yourself, or ask the maintainers first. This package does not need it to work.
