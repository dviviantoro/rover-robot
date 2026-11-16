# Robot documentation

**English** · [Bahasa Indonesia](id/README.md)

| | |
|---|---|
| [English handbook](en/README.md) | The master copy. Start here. |
| [Handbook Bahasa Indonesia](id/README.md) | Ringkasan dan peta dokumen. Diterjemahkan bertahap dari versi Inggris. |

## How this folder relates to the rest of the repository

| Folder | What it is | Who it is for |
|---|---|---|
| `docs/` (this folder) | The **handbook**: pictures first, then explanation. Describes the robot as it is, and says how sure we are of each claim | Team members, students, anyone new |
| `dev-docs/` and `planning/` | The **engineering record**: decisions, measurements, bug history, the roadmap | The people (and AI agents) changing the code |
| `AGENTS.md` | The **contract**: hard constraints and working rules | Anyone, human or AI, about to change something |

The handbook links into the engineering record instead of copying it. If the two ever
disagree, the engineering record (and the code) wins, and the handbook page is wrong:
please fix it.

## Pictures

Every diagram lives in `assets/diagrams/`: [system context](assets/diagrams/system-context.svg),
[process map](assets/diagrams/process-map.svg), [the GPU-voice swap cliff](assets/diagrams/swap-cliff.svg)
and [mecanum wheels](assets/diagrams/mecanum.svg). The `.svg` files are plain text and can be
edited by hand. Flow charts and sequence diagrams are written inline as
[Mermaid](https://mermaid.js.org/), which GitHub and VS Code draw by themselves.

## Building the site

The pages are plain Markdown that reads fine on GitHub. They are also published as a searchable
site (navigation, dark mode, Mermaid drawn in the browser) built by [MkDocs](https://www.mkdocs.org/)
with the Material theme. That tooling is **build-time only**: it goes in a separate environment,
never into the robot's `.venv`, and nothing on the robot imports it.

```bash
python3 -m venv .cache/docs-venv                      # .cache/ is on the DATA disk and gitignored
.cache/docs-venv/bin/pip install -r requirements-docs.txt
.cache/docs-venv/bin/mkdocs build                     # strict: a broken link or anchor fails it
python3 -m http.server 8000 --bind 127.0.0.1 --directory .cache/handbook-site
```

`mkdocs.yml` is the configuration. `scripts/docs/mkdocs_hooks.py` rewrites links that leave
`docs/` (to `dev-docs/`, `firmware/`, `AGENTS.md` ...) so that they point at the same file on GitHub.
Pushing to `master` publishes the site to GitHub Pages (`.github/workflows/docs.yml`); `.readthedocs.yaml`
does the same for Read the Docs. Use one of the two.
