# codemap

See a Python repo as nested boxes — packages hold modules, modules hold
classes, classes hold methods — with the calls between them. Point it at a
branch and it marks what the branch changed and every caller within N hops:
the blast radius.

Tools like graphify draw every function, file and docstring as a peer node.
codemap parses with `ast`, so it knows what each thing is and where it lives,
and it keeps that nesting in the picture.

## Install

```bash
uv tool install ~/dev-personal/codemap
```

No runtime dependencies. The page loads Cytoscape.js from a CDN.

## Use

```bash
codemap ~/code/myrepo --open
```

```bash
codemap ~/code/myrepo --diff origin/main --open
```

The page starts at a zoom that fits the repo's size. Use the level buttons to
show packages, modules, classes or functions. Double-click a box to open or
close it; click one to list what it calls and what calls it. Edges between
hidden children fold into one edge on the nearest visible box, labelled with
the number of call sites.

| Flag | Effect |
| --- | --- |
| `--diff BASE` | mark symbols changed since the merge base with `BASE`, uncommitted edits included, and their callers |
| `--depth N` | caller hops in the blast radius (default 2) |
| `--out PATH` | where to write the page (default `~/codemap-<repo>.html`) |
| `--mermaid` | print a Markdown PR comment instead of a page |
| `--json` | print the graph instead of a page |

Edge kinds: **calls**; **references**, for a function passed as a value, such
as Airflow's `python_callable=`; and **imports**, hidden until you ask.

## Post the blast radius on every PR

`--mermaid` prints the changed and affected modules nested in their
packages; GitHub renders it inline.

```yaml
# .github/workflows/codemap.yml
name: codemap
on: pull_request
permissions:
  pull-requests: write
jobs:
  map:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from git+https://github.com/<you>/codemap codemap . --diff origin/${{ github.base_ref }} --mermaid > codemap.md
      - uses: marocchino/sticky-pull-request-comment@v2
        with:
          header: codemap
          path: codemap.md
```

## Limits

- Python only.
- Calls resolve through imports, re-exports, `self`/`cls`, base classes, and
  locals typed by `x = Foo()`, annotations or `with`. Calls through untyped
  attributes or loop variables, `super()`, `getattr` and other dynamic dispatch
  stay unresolved. The summary line reports the share that resolved;
  stdlib and third-party calls make up most of the rest.
- A change at module level (an import, a constant) counts as a change to the
  module, and its blast radius follows importers rather than callers.

## Develop

```bash
uv run pytest
```
