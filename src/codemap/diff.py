"""Map a git diff to the symbols it touches, and walk callers to find the blast radius."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque
from contextlib import contextmanager

PR_URL = re.compile(r'^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)')
HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def changed_ranges(repo, base):
    """{file: [(start, end)]} of new-side lines changed since the merge base with `base`.

    Diffs against the working tree, so uncommitted edits count too.
    """
    git = lambda *args: _git(repo, *args)
    try:
        start = git('merge-base', base, 'HEAD').strip()
    except subprocess.CalledProcessError:
        start = base
    out = git('diff', '-U0', '--no-color', '--no-ext-diff', start, '--', '*.py')
    ranges, current = defaultdict(list), None
    for line in out.splitlines():
        if line.startswith('+++ '):
            current = line[6:] if line.startswith('+++ b/') else None
        elif current and (m := HUNK.match(line)):
            first, count = int(m.group(1)), int(m.group(2) or 1)
            ranges[current].append((first, first + max(count, 1) - 1))  # count 0 = pure deletion
    return dict(ranges)


def _git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@contextmanager
def checkout(repo, rev):
    """Yield a temporary worktree of `repo` at `rev`, and remove it afterwards."""
    tmp = tempfile.mkdtemp(prefix='codemap-')
    path = f'{tmp}/{rev[:12].replace("/", "-")}'
    _git(repo, 'worktree', 'add', '-q', '--detach', path, rev)
    try:
        yield path
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', path], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def pr_repo(owner, name, cache=True):
    """Yield a clone of github.com/owner/name, for mapping a PR given only by its link.

    With `cache`, the clone stays in ~/.cache/codemap and later runs fetch into it;
    without, it lives in a temporary directory that is deleted afterwards.
    """
    tmp = None if cache else tempfile.mkdtemp(prefix='codemap-')
    path = f'{tmp}/{name}' if tmp else os.path.expanduser(f'~/.cache/codemap/{owner}/{name}')
    try:
        if os.path.isdir(f'{path}/.git'):
            _git(path, 'fetch', '-q', 'origin')
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            subprocess.run(['gh', 'repo', 'clone', f'{owner}/{name}', path, '--', '-q', '--no-checkout'],
                           capture_output=True, text=True, check=True)
        yield path
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def pr_revs(repo, pr):
    """(base, head, info) for a GitHub pull request, fetched so both exist locally.

    The head is the PR's last commit, so the diff shows what GitHub's "Files changed"
    tab shows. An open PR diffs against its base branch; a merged one against the
    base branch as it stood just before the merge.
    """
    out = subprocess.run(
        ['gh', 'pr', 'view', str(pr), '--json', 'number,title,state,baseRefName,headRefOid,mergeCommit,url'],
        cwd=repo, capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    _git(repo, 'fetch', '-q', 'origin', f'pull/{info["number"]}/head', info['baseRefName'])
    head = info['headRefOid']
    merged = (info.get('mergeCommit') or {}).get('oid')
    if merged:
        _git(repo, 'fetch', '-q', 'origin', merged)
        base = f'{merged}^1'
    else:
        base = f'origin/{info["baseRefName"]}'
    return base, head, info


def _code_lines(text, lo, hi):
    """Line numbers in lo..hi that hold code, not a blank or a comment."""
    lines = text.split('\n')
    return [i for i in range(lo, hi + 1)
            if i <= len(lines) and lines[i - 1].strip() and not lines[i - 1].lstrip().startswith('#')]


def changed_symbols(index, ranges):
    """Innermost symbols whose lines overlap a changed range; the module if none does.

    A hunk of only comments and blank lines changes nothing that runs, so it marks nothing.
    """
    by_file = defaultdict(list)
    for n in index.nodes.values():
        if n['file'] and n['kind'] in ('class', 'function', 'method', 'constant'):
            by_file[n['file']].append(n)
    modules = {n['file']: n['id'] for n in index.nodes.values() if n['kind'] == 'module'}
    changed = set()
    for file, spans in ranges.items():
        if file not in modules:
            continue
        text = index.sources.get(file)
        for lo, hi in spans:
            if text is not None:
                code = _code_lines(text, lo, hi)
                if not code:
                    continue
                lo, hi = code[0], code[-1]
            hits = [n for n in by_file[file] if n['line'] <= hi and n['end'] >= lo]
            parents = {n['parent'] for n in hits}
            inner = [n['id'] for n in hits if n['id'] not in parents]
            changed.update(inner or [modules[file]])
    return changed


def blast_radius(index, changed, depth):
    """{node id: hops} for changed nodes (0) and everything that reaches them."""
    rev = defaultdict(set)
    for (src, dst, kind) in index.edges:
        if kind in ('calls', 'references', 'reads'):
            rev[dst].add(src)
    rev_imports = defaultdict(set)
    for (src, dst, kind) in index.edges:
        if kind == 'imports':
            rev_imports[dst].add(src)
    hops = {c: 0 for c in changed}
    queue = deque((c, 0) for c in changed)
    while queue:
        nid, d = queue.popleft()
        if d == depth:
            continue
        # A module-level change reaches its importers; a symbol change reaches its callers,
        # and a constant change reaches the code that reads it.
        sources = rev_imports[nid] if index.nodes[nid]['kind'] == 'module' else rev[nid]
        for src in sources:
            if src not in hops:
                hops[src] = d + 1
                queue.append((src, d + 1))
    return hops
