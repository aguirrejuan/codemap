"""Parse a Python repo into a nested graph: packages, modules, classes, functions.

Nodes carry an explicit kind and a parent, so the hierarchy is the tree itself,
not a set of `contains` edges. Edges are `calls`, `references` (a function
passed as a value, such as Airflow's `python_callable=`), `reads` (a function or
constant using a module-level constant) and `imports`.
"""
import ast
import os
import re
import subprocess
import sys
from collections import defaultdict

SKIP_DIRS = {'.git', '.venv', 'venv', 'env', 'node_modules', 'build', 'dist',
             '__pycache__', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages'}
SYMBOL_KINDS = ('class', 'function', 'method')
STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | set(sys.builtin_module_names)
TEST = re.compile(r'^(tests?|testing|conftest)$|^test_|_tests?$')  # the viewer's Hide tests uses the same rule
# Decorators that wrap a function without registering it anywhere; any other decorator
# may hand the function to a framework that calls it, such as a route or a CLI command.
# A property counts as registered: serializers read it by name, which no call shows.
PLAIN_DECORATORS = {'staticmethod', 'classmethod', 'cache', 'lru_cache', 'abstractmethod'}
CONSTANT = re.compile(r'^_*[A-Z][A-Z0-9_]*$')  # module-level names in upper case, by PEP 8


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


def _module_level(body):
    """Statements that run when the module loads, looking through if/try/with blocks."""
    for stmt in body:
        if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield stmt
        for field in ('body', 'orelse', 'finalbody', 'handlers'):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list):
                yield from _module_level(inner)


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
        self.eager = set()              # (module, module) imports that run at load time
        self.cycles = []
        self.unresolved = defaultdict(int)
        self.calls_total = 0
        self.loose = set()              # attribute names used on objects of unknown type
        self.hooked = set()             # functions under a decorator that may register them
        self.imported = set()           # symbols some module imports by name
        self.const_ast = {}             # id(assignment) -> constant id
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
            self._constants(tree.body, mid)
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
            start = min([d.lineno] + [x.lineno for x in d.decorator_list])  # decorators belong to it
            self._add(nid, kind, parent, d.name, self.mods.get(mid, {}).get('file') or self.nodes[mid]['file'],
                      start, getattr(d, 'end_lineno', d.lineno))
            self.ast_id[id(d)] = nid
            if kind != 'class' and any(
                    (_dotted(x.func if isinstance(x, ast.Call) else x) or '').split('.')[-1]
                    not in PLAIN_DECORATORS for x in d.decorator_list):
                self.hooked.add(nid)
            self._symbols(d.body, nid, mid, in_class=isinstance(d, ast.ClassDef))

    def _constants(self, body, mid):
        """Upper-case names a module assigns at load time, each a node of its own."""
        for stmt in _module_level(body):
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target = stmt.target
            else:
                continue
            if not (isinstance(target, ast.Name) and CONSTANT.match(target.id)) \
                    or (mid, target.id) in self.child:
                continue
            nid = f'{mid}.{target.id}'
            self._add(nid, 'constant', mid, target.id, self.nodes[mid]['file'],
                      stmt.lineno, getattr(stmt, 'end_lineno', stmt.lineno))
            self.const_ast[id(stmt)] = nid

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
        """Fill the module's name table and its import targets.

        Each target is (dotted name, relative?, eager?). An eager import runs when
        the module loads: not inside a function and not under `if TYPE_CHECKING`.
        Only eager imports can form a cycle that breaks at import time.
        """
        table = self.mods[mid]['imports']
        targets = self.mods[mid].setdefault('import_targets', [])
        for node, eager in _walk_imports(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        table[a.asname] = a.name
                    else:
                        head = a.name.split('.')[0]
                        table[head] = head
                    targets.append((a.name, False, eager))
            else:
                base = self._from_base(mid, node)
                for a in node.names:
                    if a.name == '*':
                        targets.append((base, bool(node.level), eager))
                        continue
                    qual = f'{base}.{a.name}' if base else a.name
                    table[a.asname or a.name] = qual
                    targets.append((qual, bool(node.level), eager))

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
        # A repo that puts its own folders on sys.path imports `ml` for `dagfolder/ml`.
        # Modules count only at the root, so `utils/json.py` does not hide stdlib `json`.
        local = {n['label'] for n in self.nodes.values() if n['kind'] == 'package'}
        local |= {n['id'] for n in self.nodes.values() if n['kind'] == 'module' and not n['parent']}
        for mid, info in self.mods.items():
            _Linker(self, mid).visit(info['tree'])
            for target, relative, eager in info.get('import_targets', []):
                hit = self.resolve_qual(target) if target else None
                dst = self.module_of(hit) if hit else None
                if hit and self.nodes[hit]['kind'] in SYMBOL_KINDS:
                    self.imported.add(hit)
                if dst:
                    if dst != mid and self.nodes[dst]['kind'] == 'module':
                        self.edges[(mid, dst, 'imports')] += 1
                        if eager:
                            self.eager.add((mid, dst))
                    continue
                top = target.split('.')[0]
                if relative or not top or top == '__future__' or top in local:
                    continue
                ext = 'ext:' + top
                if ext not in self.nodes:
                    self._add(ext, 'external', None, top)
                    self.nodes[ext]['stdlib'] = top in STDLIB
                self.edges[(mid, ext, 'imports')] += 1
        self.cycles = _cycles(self.eager)
        self._metrics()
        return self

    # ---- pass 3: fan-in, fan-out, functions nothing calls ------------------

    def _metrics(self):
        """Count each symbol's distinct callers and callees, and mark the functions with no caller.

        A function stays unmarked when another module imports it by name, or when three or
        more modules define a top-level function of that name, as migrations define
        `upgrade`. A call through an object of unknown type resolves to nothing, so a
        method stays unmarked when its name appears on such an object anywhere in the repo, when a
        class in its hierarchy defines the same name, or when a base class lives outside
        the repo and may call it as a hook.
        """
        callers, callees = defaultdict(set), defaultdict(set)
        for s, t, kind in self.edges:
            if kind in ('calls', 'references'):
                callers[t].add(s)
                callees[s].add(t)
        self._family()
        tops = defaultdict(int)  # `upgrade` in every migration: a loader calls these by convention
        for n in self.nodes.values():
            if n['kind'] == 'function' and self.nodes[n['parent']]['kind'] == 'module':
                tops[n['label']] += 1
        self.convention = {k for k, c in tops.items() if c >= 3}
        for nid, n in self.nodes.items():
            if n['kind'] not in SYMBOL_KINDS:
                continue
            n['fan_in'], n['fan_out'] = len(callers[nid]), len(callees[nid])
            if n['kind'] != 'class' and not n['fan_in'] and not self._excused(nid):
                n['uncalled'] = True

    def _family(self):
        """Each class's project bases and subclasses, and the classes with a base outside the repo."""
        self.up, self.down, self.foreign = defaultdict(list), defaultdict(list), set()
        for cid, exprs in self.bases.items():
            for b in exprs:
                hit = self.resolve_expr(b, [self.module_of(cid)], {})
                if hit and self.nodes[hit]['kind'] == 'class':
                    self.up[cid].append(hit)
                    self.down[hit].append(cid)
                elif not (isinstance(b, ast.Name) and b.id == 'object'):
                    self.foreign.add(cid)

    def _excused(self, nid):
        n = self.nodes[nid]
        name = n['label']
        if name == 'main' or name.isupper() or (name.startswith('__') and name.endswith('__')) \
                or nid in self.hooked or nid in self.imported:
            return True
        if n['kind'] == 'function' and name in self.convention \
                and self.nodes[n['parent']]['kind'] == 'module':
            return True
        if any(TEST.search(p) for p in nid.split('.')):
            return True
        if n['kind'] != 'method':
            return False
        return name in self.loose or self._dispatched(n['parent'], name)

    def _dispatched(self, cid, name):
        """Whether another class in cid's hierarchy defines `name`, or an outside base may call it."""
        seen, todo = {cid}, [(cid, 'up'), (cid, 'down')]
        while todo:
            c, way = todo.pop()
            if way == 'up' and c in self.foreign:
                return True
            for o in (self.up if way == 'up' else self.down)[c]:
                if o in seen:
                    continue
                if (o, name) in self.child:
                    return True
                seen.add(o)
                todo.append((o, way))
        return False

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
            'cycles': self.cycles,
        }


def _type_checking(test):
    return (isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING') or \
        (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')


def _walk_imports(node, eager=True):
    """Yield (import node, eager?) for every import under `node`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child, eager
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield from _walk_imports(child, False)
        elif isinstance(child, ast.If) and _type_checking(child.test):
            for sub in child.body:
                yield from _walk_imports(ast.Module(body=[sub], type_ignores=[]), False)
            for sub in child.orelse:
                yield from _walk_imports(ast.Module(body=[sub], type_ignores=[]), eager)
        else:
            yield from _walk_imports(child, eager)


def _cycles(pairs):
    """Import cycles: strongly connected groups of modules, with the edges inside each.

    Tarjan's algorithm, iterative so a deep import chain cannot hit the recursion limit.
    """
    graph = defaultdict(list)
    for a, b in sorted(pairs):
        graph[a].append(b)
    index, low, stack, on, groups = {}, {}, [], set(), []
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = len(index)
                stack.append(v)
                on.add(v)
            nbrs = graph.get(v, [])
            if i < len(nbrs):
                work.append((v, i + 1))
                w = nbrs[i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                group = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    group.append(w)
                    if w == v:
                        break
                if len(group) > 1:
                    groups.append(sorted(group))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    out = []
    for group in sorted(groups):
        members = set(group)
        out.append({'modules': group,
                    'edges': sorted([a, b] for a, b in pairs if a in members and b in members)})
    return out


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
            if isinstance(node.func, ast.Attribute):
                self.ix.loose.add(node.func.attr)
        if isinstance(node.func, ast.Name) and node.func.id == 'getattr' and len(node.args) > 1 \
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            self.ix.loose.add(node.args[1].value)
        self.generic_visit(node)

    def _assign(self, node):
        """Reads in a constant's value come from the constant, not from the module."""
        nid = self.ix.const_ast.get(id(node))
        if nid is None:
            return self.generic_visit(node)
        self.chain.append(nid)
        self.visit(node.value)
        self.chain.pop()

    visit_Assign = visit_AnnAssign = _assign

    def _reference(self, node):
        """Record a function used as a value or a constant read; return what the expression denotes."""
        target = self.ix.resolve_expr(node, self.chain, self.locals[-1])
        kind = target and self.ix.nodes[target]['kind']
        if kind in ('function', 'method'):
            self._edge(target, 'references')
        elif kind == 'constant':
            self._edge(target, 'reads')
        return target

    def visit_Name(self, node):
        if id(node) not in self.call_funcs and isinstance(node.ctx, ast.Load):
            self._reference(node)

    def visit_Attribute(self, node):
        if id(node) not in self.call_funcs and isinstance(node.ctx, ast.Load):
            target = self._reference(node)
            if target is None:
                self.ix.loose.add(node.attr)
            elif self.ix.nodes[target]['kind'] in ('function', 'method', 'constant'):
                return
        self.generic_visit(node)


def build(repo):
    return Index(os.path.abspath(repo)).load().link()
