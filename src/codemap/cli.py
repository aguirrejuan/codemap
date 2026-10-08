"""codemap: see a Python repo as nested packages, modules, classes and functions.

    codemap REPO [--diff BASE] [--depth N] [--out PATH] [--open]
    codemap REPO --diff BASE --head REV        # compare two commits
    codemap REPO --pr 397 [--open]             # a GitHub pull request against its base
    codemap https://github.com/OWNER/REPO/pull/397   # the same, from the link alone
    codemap REPO --diff origin/main --mermaid   # for a PR comment
    codemap REPO --json
"""
import argparse
import json
import os
import subprocess
import sys
import webbrowser
from contextlib import ExitStack
from datetime import datetime, timezone

from . import diff as diffmod
from . import parse, render


def summarise(index, hops):
    n = index.nodes.values()
    mods = sum(1 for x in n if x['kind'] == 'module')
    syms = sum(1 for x in n if x['kind'] in ('class', 'function', 'method'))
    resolved = index.calls_total - sum(index.unresolved.values())
    pct = round(100 * resolved / index.calls_total) if index.calls_total else 0
    ext = sum(1 for x in n if x['kind'] == 'external')
    s = lambda n, word: f'{n} {word}' + ('' if n == 1 else 's')
    base = (f'{mods} modules, {syms} symbols, {len(index.edges)} edges, '
            f'{s(ext, "external package")}. '
            f'{pct}% of {index.calls_total} calls resolve to project code; the rest are '
            f'stdlib, third-party or dynamic.')
    if index.cycles:
        biggest = max(len(c['modules']) for c in index.cycles)
        base += (f' {s(len(index.cycles), "import cycle")}, the largest '
                 f'through {s(biggest, "module")}.')
    idle = sum(1 for x in n if x.get('uncalled'))
    if idle:
        base += f' {s(idle, "function")} with no callers found.'
    if hops is None:
        return base, None
    changed = [k for k, h in hops.items() if h == 0]
    reached = len(hops) - len(changed)
    tops = {k.split('.')[0] for k in hops}
    pr = (f'{s(len(changed), "changed symbol")} reach{"es" if len(changed) == 1 else ""} '
          f'{s(reached, "other")} across {s(len(tops), "top-level package")}.')
    return base, pr


def main(argv=None):
    ap = argparse.ArgumentParser(prog='codemap', description=__doc__.splitlines()[0])
    ap.add_argument('repo', nargs='?', default='.', help='repo directory, or a GitHub pull request link')
    ap.add_argument('--diff', metavar='BASE', help='highlight changes since the merge base with BASE')
    ap.add_argument('--head', metavar='REV',
                    help='map commit REV instead of the working tree (needs --diff)')
    ap.add_argument('--pr', metavar='N', help='map GitHub pull request N against its base branch')
    ap.add_argument('--no-cache', action='store_true',
                    help='for a PR link, clone into a temporary directory and delete it afterwards')
    ap.add_argument('--depth', type=int, default=2, help='caller hops in the blast radius (default 2)')
    ap.add_argument('--out', help='HTML path (default ~/codemap-<repo>.html)')
    ap.add_argument('--open', action='store_true', help='open the page in a browser')
    ap.add_argument('--mermaid', action='store_true', help='print a Mermaid PR comment instead')
    ap.add_argument('--json', action='store_true', help='print the graph as JSON instead')
    ap.add_argument('--no-source', action='store_true', help='leave source code out of the page')
    a = ap.parse_args(argv)

    m = diffmod.PR_URL.match(a.repo)
    if not m:
        return mapped(a)
    if a.pr:
        sys.exit('codemap: give the PR as a link or with --pr, not both')
    with ExitStack() as stack:
        try:
            a.repo = stack.enter_context(diffmod.pr_repo(m.group(1), m.group(2), cache=not a.no_cache))
        except subprocess.CalledProcessError as e:
            sys.exit(f'codemap: cannot clone {m.group(1)}/{m.group(2)}: {e.stderr.strip()}')
        a.pr = m.group(3)
        mapped(a)


def mapped(a):
    """Map the repo at a.repo, or the commit or PR that a.head or a.pr names."""
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(repo):
        sys.exit(f'codemap: {a.repo} is not a directory')
    name, label = os.path.basename(repo), None
    if a.pr:
        if a.diff or a.head:
            sys.exit('codemap: --pr sets the base and head itself; drop --diff and --head')
        try:
            a.diff, a.head, info = diffmod.pr_revs(repo, a.pr)
        except Exception as e:  # gh missing or signed out, no such PR
            sys.exit(f'codemap: cannot read pull request {a.pr}: {getattr(e, "stderr", "") or e}')
        label = f'PR #{info["number"]}: {info["title"]}'
        a.out = a.out or f'~/codemap-{name}-pr{info["number"]}.html'
        print(f'{label} ({info["state"].lower()}), {info["url"]}',
              file=sys.stderr if a.json or a.mermaid else sys.stdout)
    if a.head:
        if not a.diff:
            sys.exit('codemap: --head needs --diff BASE')
        try:
            with diffmod.checkout(repo, a.head) as tree:
                run(a, tree, name, repo, label)
        except subprocess.CalledProcessError as e:
            sys.exit(f'codemap: cannot check out {a.head}: {e.stderr.strip()}')
    else:
        run(a, repo, name, repo, label)


def run(a, repo, name, root, label):
    """Map the tree at `repo`; `name` and `root` describe the repo the user named."""
    index = parse.build(repo)
    if not index.mods:
        sys.exit(f'codemap: no Python files found in {repo}')

    hops, ranges = None, {}
    if a.diff:
        try:
            ranges = diffmod.changed_ranges(repo, a.diff)
        except Exception as e:  # git missing, bad ref
            sys.exit(f'codemap: git diff against {a.diff} failed: {e}')
        hops = diffmod.blast_radius(index, diffmod.changed_symbols(index, ranges), a.depth)
    base, pr = summarise(index, hops)

    data = index.data()
    data.update(repo=name, root=root,
                generated=datetime.now(timezone.utc).isoformat(timespec='seconds'), summary=base,
                sources={} if a.no_source else index.sources,
                diff={'base': a.diff, 'pr': label, 'depth': a.depth, 'hops': hops, 'summary': pr, 'lines': ranges}
                if a.diff else None)

    if a.json:
        json.dump(data, sys.stdout, indent=1)
        print(base, file=sys.stderr)
        return
    if a.mermaid:
        print(render.mermaid(index, hops or {}, pr or base))
        return

    out = os.path.expanduser(a.out or f'~/codemap-{name}.html')
    render.html(data, out)
    print(base)
    if pr:
        print(pr)
    if index.errors:
        print(f'{len(index.errors)} files failed to parse: ' + ', '.join(index.errors[:5]))
    print(out)
    if a.open:
        webbrowser.open('file://' + out)


if __name__ == '__main__':
    main()
