"""Parse a Python repo into a nested graph: packages, modules, classes, functions.

Nodes carry an explicit kind and a parent, so the hierarchy is the tree itself,
not a set of `contains` edges. Edges are `calls`, `references` (a function
passed as a value, such as Airflow's `python_callable=`) and `imports`.
"""
import ast
import os
import subprocess
from collections import defaultdict

SKIP_DIRS = {'.git', '.venv', 'venv', 'env', 'node_modules', 'build', 'dist',
             '__pycache__', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages'}
SYMBOL_KINDS = ('class', 'function', 'method')


def list_files(repo):
    """Python files tracked or untracked-but-not-ignored by git; a plain walk otherwise."""
    try:
        out = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '*.py'],
            cwd=repo, capture_output=True, text=True, check=True).stdout
        files = sorted(f for f in set(out.split('\0')) if f and os.path.isfile(os.path.join(repo, f)))
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        files += [os.path.relpath(os.path.join(root, n), repo) for n in names if n.endswith('.py')]
    return sorted(files)


def _defs(body):
    """Class and function statements in a body, looking through if/try/with blocks."""
    for stmt in body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield stmt
        else:
            for field in ('body', 'orelse', 'finalbody', 'handlers'):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    yield from _defs(inner)


class Index:
    def __init__(self, repo):
        self.repo = repo
        self.nodes = {}                 # id -> node dict
        self.child = {}                 # (parent id, name) -> id
        self.ast_id = {}                # id(ast node) -> node id
        self.mods = {}                  # module id -> {'tree', 'file', 'pkg', 'imports'}
        self.bases = {}                 # class id -> [ast expr]
        self.attr_types = defaultdict(dict)  # class id -> {attr: class id}
        self.edges = defaultdict(int)   # (src, dst, kind) -> count
        self.unresolved = defaultdict(int)
        self.calls_total = 0
        self.errors = []
        self.sources = {}               # file -> text, for the viewer's code panel
        self._exact, self._suffix = {}, defaultdict(set)

    # ---- pass 1: nodes, imports ------------------------------------------

    def _add(self, nid, kind, parent, label, file=None, line=None, end=None):
        self.nodes[nid] = {'id': nid, 'kind': kind, 'parent': parent, 'label': label,
                           'file': file, 'line': line, 'end': end,
                           'loc': (end - line + 1) if line and end else None}
        if parent is not None:
            self.child[(parent, label)] = nid

    def _package(self, dotted):
        if not dotted or dotted in self.nodes:
            return dotted or None
        head, _, name = dotted.rpartition('.')
        parent = self._package(head)
        self._add(dotted, 'package', parent, name)
        return dotted

    def load(self):
        for rel in list_files(self.repo):
            parts = rel[:-3].split(os.sep)
            mid = '.'.join(parts)
            pkg = self._package('.'.join(parts[:-1]))
            try:
                src = open(os.path.join(self.repo, rel), encoding='utf-8', errors='replace').read()
                tree = ast.parse(src, filename=rel)
            except (SyntaxError, ValueError) as e:
                self.errors.append(f'{rel}: {e.__class__.__name__}')
                continue
            self.sources[rel] = src
            self._add(mid, 'module', pkg, parts[-1] + '.py', rel, 1, max(1, src.count('\n') + 1))
            self.mods[mid] = {'tree': tree, 'file': rel, 'pkg': pkg, 'imports': {}}
            names = [mid] + ([mid[:-len('.__init__')]] if mid.endswith('.__init__') else [])
            for name in names:
                self._exact[name] = mid
                p = name.split('.')
                for k in range(1, len(p)):
                    self._suffix['.'.join(p[k:])].add(mid)
            self._symbols(tree.body, mid, mid, in_class=False)
            self._collect_imports(mid, tree)
        return self

    def _symbols(self, body, parent, mid, in_class):
        for d in _defs(body):
            nid = f'{parent}.{d.name}'
            if isinstance(d, ast.ClassDef):
                kind = 'class'
                self.bases[nid] = d.bases
            else:
                kind = 'method' if in_class else 'function'
            self._add(nid, kind, parent, d.name, self.mods.get(mid, {}).get('file') or self.nodes[mid]['file'],
                      d.lineno, getattr(d, 'end_lineno', d.lineno))
            self.ast_id[id(d)] = nid
            self._symbols(d.body, nid, mid, in_class=isinstance(d, ast.ClassDef))

    def _from_base(self, mid, node):
        """Absolute dotted module name of a `from X import` statement."""
        if not node.level:
            return node.module or ''
        pkg = self.mods[mid]['pkg'] or ''
        if mid.endswith('.__init__'):
            pkg = mid[:-len('.__init__')]
        parts = pkg.split('.') if pkg else []
        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
        return '.'.join(parts + ([node.module] if node.module else []))

    def _collect_imports(self, mid, tree):
        table = self.mods[mid]['imports']
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        table[a.asname] = a.name
                    else:
                        head = a.name.split('.')[0]
                        table[head] = head
                    self.mods[mid].setdefault('import_targets', []).append(a.name)
            elif isinstance(node, ast.ImportFrom):
                base = self._from_base(mid, node)
                for a in node.names:
                    if a.name == '*':
                        continue
                    table[a.asname or a.name] = f'{base}.{a.name}' if base else a.name
                    self.mods[mid].setdefault('import_targets', []).append(
                        f'{base}.{a.name}' if base else a.name)

    # ---- resolution -------------------------------------------------------

    def module_for(self, dotted):
        if dotted in self._exact:
            return self._exact[dotted]
        hits = self._suffix.get(dotted)
        return next(iter(hits)) if hits and len(hits) == 1 else None

    def resolve_qual(self, qual, depth=0):
        parts = qual.split('.')
        for i in range(len(parts), 0, -1):
            m = self.module_for('.'.join(parts[:i]))
            if m is None:
                continue
            return m if i == len(parts) else self._in_module(m, parts[i:], depth)
        return None

    def _in_module(self, mid, rest, depth):
        nid = self.child.get((mid, rest[0]))
        if nid is None and depth < 5 and rest[0] in self.mods[mid]['imports']:
            nid = self.resolve_qual(self.mods[mid]['imports'][rest[0]], depth + 1)  # re-export
        for part in rest[1:]:
            if nid is None:
                return None
            nid = self.member(nid, part)
        return nid

    def module_of(self, nid):
        while nid and self.nodes[nid]['kind'] not in ('module', 'package'):
            nid = self.nodes[nid]['parent']
        return nid

    def member(self, nid, name, seen=None):
        """A child of nid; for classes, also inherited from project base classes."""
        n = self.nodes.get(nid)
        if n is None:
            return None
        if n['kind'] == 'module':
            return self._in_module(nid, [name], 0)
        hit = self.child.get((nid, name))
        if hit or n['kind'] != 'class':
            return hit
        seen = seen or set()
        if nid in seen:
            return None
        seen.add(nid)
        if name in self.attr_types.get(nid, {}):
            return None  # an instance attribute, not a method
        for b in self.bases.get(nid, []):
            base = self.resolve_expr(b, [self.module_of(nid)], {})
            if base and self.nodes[base]['kind'] == 'class':
                hit = self.member(base, name, seen)
                if hit:
                    return hit
        return None

    def resolve_name(self, name, chain, local):
        if name in local:
            return local[name]
        for i in range(len(chain) - 1, -1, -1):
            sid = chain[i]
            if self.nodes[sid]['kind'] == 'class' and i != len(chain) - 1:
                continue  # class scope is invisible to its methods
            hit = self.child.get((sid, name))
            if hit:
                return hit
        mid = chain[0]
        qual = self.mods[mid]['imports'].get(name)
        return self.resolve_qual(qual) if qual else None

    def _enclosing_class(self, chain):
        for sid in reversed(chain[:-1] if self.nodes[chain[-1]]['kind'] == 'class' else chain):
            if self.nodes[sid]['kind'] == 'class':
                return sid
        return None

    def resolve_expr(self, expr, chain, local):
        """Project node an expression denotes; an instance resolves to its class."""
        if isinstance(expr, ast.Name):
            return self.resolve_name(expr.id, chain, local)
        if isinstance(expr, ast.Attribute):
            v = expr.value
            if isinstance(v, ast.Name) and v.id in ('self', 'cls') and v.id not in local:
                cls = self._enclosing_class(chain)
                if cls is None:
                    return None
                if expr.attr in self.attr_types.get(cls, {}):
                    return self.attr_types[cls][expr.attr]
                return self.member(cls, expr.attr)
            dotted = _dotted(expr)
            if dotted and dotted.split('.')[0] in self.mods[chain[0]]['imports'] \
                    and dotted.split('.')[0] not in local:
                head, _, tail = dotted.partition('.')
                hit = self.resolve_qual(self.mods[chain[0]]['imports'][head] + '.' + tail)
                if hit:
                    return hit
            base = self.resolve_expr(v, chain, local)
            if base is None:
                return None
            if self.nodes[base]['kind'] == 'class' and expr.attr in self.attr_types.get(base, {}):
                return self.attr_types[base][expr.attr]
            return self.member(base, expr.attr)
        if isinstance(expr, ast.Call):
            target = self.resolve_expr(expr.func, chain, local)
            return target if target and self.nodes[target]['kind'] == 'class' else None
        if isinstance(expr, ast.Subscript):  # Optional[Foo], list[Foo]
            return self.resolve_expr(expr.slice, chain, local)
        return None

    def _class_of(self, expr, chain, local):
        nid = self.resolve_expr(expr, chain, local) if expr is not None else None
        return nid if nid and self.nodes[nid]['kind'] == 'class' else None

    # ---- pass 2: types, calls, imports -------------------------------------

    def link(self):
        for mid, info in self.mods.items():
            for cls_ast in (n for n in ast.walk(info['tree']) if isinstance(n, ast.ClassDef)):
                self._attr_types(cls_ast, mid)
        for mid, info in self.mods.items():
            _Linker(self, mid).visit(info['tree'])
            for target in info.get('import_targets', []):
                hit = self.resolve_qual(target)
                dst = self.module_of(hit) if hit else None
                if dst and dst != mid and self.nodes[dst]['kind'] == 'module':
                    self.edges[(mid, dst, 'imports')] += 1
        return self

    def _attr_types(self, cls_ast, mid):
        cid = self.ast_id[id(cls_ast)]
        chain = self._chain(cid)
        for stmt in cls_ast.body:  # class-level annotations: `store: FeatureStore`
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                t = self._class_of(stmt.annotation, chain, {})
                if t:
                    self.attr_types[cid][stmt.target.id] = t
        for fn in (s for s in cls_ast.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))):
            fchain = chain + [self.ast_id[id(fn)]]
            local = _param_types(self, fn, fchain)
            for node in ast.walk(fn):
                target, value, ann = None, None, None
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target, value = node.targets[0], node.value
                elif isinstance(node, ast.AnnAssign):
                    target, value, ann = node.target, node.value, node.annotation
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                        and target.value.id == 'self':
                    t = self._class_of(ann, fchain, local) or \
                        (self._class_of(value, fchain, local) if isinstance(value, (ast.Call, ast.Name)) else None)
                    if isinstance(value, ast.Name) and not ann:
                        t = local.get(value.id)
                    if t:
                        self.attr_types[cid][target.attr] = t

    def _chain(self, nid):
        chain = []
        while nid and self.nodes[nid]['kind'] != 'package':
            chain.append(nid)
            nid = self.nodes[nid]['parent']
        return chain[::-1]

    def data(self):
        return {
            'nodes': list(self.nodes.values()),
            'edges': [[s, t, k, c] for (s, t, k), c in sorted(self.edges.items())],
            'unresolved': dict(self.unresolved),
            'calls_total': self.calls_total,
            'errors': self.errors,
        }


def _dotted(expr):
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return '.'.join(reversed(parts))


def _param_types(index, fn, chain):
    local = {}
    args = fn.args
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        t = index._class_of(a.annotation, chain[:-1], {}) if a.annotation is not None else None
        if t:
            local[a.arg] = t
    return local


class _Linker(ast.NodeVisitor):
    """Walks one module, recording calls and references from the innermost symbol."""

    def __init__(self, index, mid):
        self.ix, self.mid = index, mid
        self.chain, self.locals = [mid], [{}]
        self.call_funcs = set()

    def _scope(self, node):
        for d in node.decorator_list:
            self.visit(d)
        if isinstance(node, ast.ClassDef):
            for b in node.bases + [k.value for k in node.keywords]:
                self.visit(b)
        else:
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                self.visit(d)
        nid = self.ix.ast_id[id(node)]
        self.chain.append(nid)
        local = dict(_param_types(self.ix, node, self.chain)) if not isinstance(node, ast.ClassDef) else {}
        if not isinstance(node, ast.ClassDef):
            for sub in ast.walk(node):  # `x = Foo()` makes x a Foo for the whole body
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    t = self.ix._class_of(sub.value, self.chain, local) if isinstance(sub.value, ast.Call) else None
                    if t:
                        local[sub.targets[0].id] = t
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    t = self.ix._class_of(sub.annotation, self.chain, local)
                    if t:
                        local[sub.target.id] = t
                elif isinstance(sub, (ast.With, ast.AsyncWith)):
                    for item in sub.items:
                        if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                            t = self.ix._class_of(item.context_expr, self.chain, local)
                            if t:
                                local[item.optional_vars.id] = t
        self.locals.append(local)
        for stmt in node.body:
            self.visit(stmt)
        self.locals.pop()
        self.chain.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def _edge(self, dst, kind):
        src = self.chain[-1]
        if dst != src:
            self.ix.edges[(src, dst, kind)] += 1

    def visit_Call(self, node):
        self.ix.calls_total += 1
        self.call_funcs.add(id(node.func))
        target = self.ix.resolve_expr(node.func, self.chain, self.locals[-1])
        if target and self.ix.nodes[target]['kind'] in SYMBOL_KINDS:
            self._edge(target, 'calls')
        else:
            self.ix.unresolved[self.mid] += 1
        self.generic_visit(node)

    def _maybe_reference(self, node):
        if id(node) in self.call_funcs or not isinstance(getattr(node, 'ctx', None), ast.Load):
            return False
        target = self.ix.resolve_expr(node, self.chain, self.locals[-1])
        if target and self.ix.nodes[target]['kind'] in ('function', 'method'):
            self._edge(target, 'references')
            return True
        return False

    def visit_Name(self, node):
        self._maybe_reference(node)

    def visit_Attribute(self, node):
        if not self._maybe_reference(node):
            self.generic_visit(node)


def build(repo):
    return Index(os.path.abspath(repo)).load().link()
