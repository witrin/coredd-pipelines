# Mirror

Read-only web mirror of the collected GitHub data of every repository that carries annotator labels, so annotators can follow code, pull requests and issues when the original repository is no longer reachable.

The mirror holds exactly what the collection captured and fetches nothing from GitHub.
The repositories are the ones named in `audit-change-annotation` or `filter-change-annotation`.
Pull requests and issues come from `scope-change-raw` and `scope-issue-raw`, so only the ones closed within the collection window are present.
Code comes from the bare clone in `scope-source-raw`, including `refs/pull/*`.
Comments, reviews, review threads and commits of a pull request are complete; the timeline was collected from its first page only, so later label, assignment and force-push events of long threads are missing.

## Layout

- `archive.py` builds the mirror root: `mirror.sqlite` with the compressed raw payloads plus one `<owner>/<repo>.git` bare repository per repository.
- `store.py` queries `mirror.sqlite`.
- `code.py` reads trees, blobs, history and diffs from the bare repositories and highlights them with Pygments.
- `markup.py` renders issue and pull request Markdown, sanitizes embedded HTML and rewrites github.com links and `#123`, `owner/repo#123` and SHA references of mirrored repositories onto mirror paths.
- `conversation.py` merges comments, reviews, review threads, commits and timeline events into one chronological list.
- `server.py` is the Starlette application.
- `icon.py` provides the Primer Octicons (MIT) and generated identicon avatars used by the templates.

## URLs

URLs follow GitHub's layout below `/<owner>/<repo>`, so replacing `https://github.com` with the mirror host turns a GitHub link into a mirror link.
Supported paths are `pull/<n>` with `/commits` and `/files`, `issues/<n>`, `commit/<sha>`, `commits/<ref>[/<path>]`, `tree/<ref>/<path>`, `blob/<ref>/<path>` with `#L<n>` and `#L<n>-L<m>` anchors, `raw/<ref>/<path>` and `compare/<base>...<head>`.
File anchors in diffs use GitHub's `#diff-<sha256 of path>` form.
Anything outside the collection answers with a 404 page that says so.
The start page also accepts a pasted github.com link and opens the matching mirror page.

## Build

The build reads the assets from the bucket and needs Google Cloud credentials and `CLOUD_BUCKET`.
It writes about 850 MB for the six labeled repositories.

```sh
python -m benchmark.mirror.archive /srv/mirror
```

Asset versions default to the ones the mirror was built from and can be overridden with `--change`, `--issue`, `--edge`, `--source`, `--stationary`, `--audit` and `--filter`.

## Serve

```sh
python -m benchmark.mirror.server /srv/mirror --host 0.0.0.0 --port 8765
```

The server has no sign-in, because annotators hand mirror links to chat assistants such as Claude and ChatGPT, whose fetchers cannot sign in.
`robots.txt` admits only those user-initiated fetchers (`Claude-User`, `ChatGPT-User`) and every page carries `noindex`, so search and training crawlers stay out; the mirror holds only data that was public on GitHub.
Responses are gzip-compressed and the port defaults to the `PORT` environment variable, as set by Cloud Run.
