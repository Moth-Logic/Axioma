"""
Axioma Language Interpreter
============================
Tree-walk interpreter. Takes the AST from the parser and executes it.

Usage:
    python3 interpreter.py my_program.axm
    python3 interpreter.py          (runs built-in demo)
"""

from __future__ import annotations
import sys
import math
import re
from typing import Any, List, Optional

from axioma_lexer import Lexer
from axioma_parser import (
    Parser, Node,
    Program, Block, VarDecl, FuncDef, Param, ReturnStmt,
    IfStmt, EachStmt, RepeatStmt, MatchStmt,
    CheckStmt, AbortStmt, UsingStmt, FromStmt,
    StructDef, StructField, ExprStmt,
    SkipStmt, StopStmt, NextStmt, DropStmt, VerifyStmt,
    EmitStmt, ExpandStmt, UsingAsStmt, IfMain, Assign,
    Literal, InterpolatedString, Identifier, MathSet,
    BinaryOp, UnaryOp, LenOp, Call, Projection,
    Subscript, Slice, Range, Ternary, LambdaExpr,
    ListExpr, SetExpr, MappingExpr,
    ListComp, SetComp, MappingComp, GeneratorComp,
    CollectExpr, SpawnExpr, Unpack,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Control-flow signals  (raised as exceptions, caught by the interpreter)
# ══════════════════════════════════════════════════════════════════════════════

class ReturnSignal(Exception):
    def __init__(self, value: Any):
        self.value = value

class StopSignal(Exception):   # break
    pass

class NextSignal(Exception):   # continue
    pass

class AxiomaError(Exception):  # abort
    def __init__(self, value: Any, cause: Any = None):
        self.value = value
        self.cause = cause
        super().__init__(str(value))


# ══════════════════════════════════════════════════════════════════════════════
#  Environment  (scope / symbol table)
# ══════════════════════════════════════════════════════════════════════════════

class Environment:
    def __init__(self, parent: Optional[Environment] = None):
        self.vars:   dict[str, Any] = {}
        self.parent: Optional[Environment] = parent

    def get(self, name: str) -> Any:
        if name in self.vars:
            return self.vars[name]
        if self.parent:
            return self.parent.get(name)
        raise AxiomaError(f"Undefined variable '{name}'")

    def set(self, name: str, value: Any):
        """Set in the current (innermost) scope."""
        self.vars[name] = value

    def assign(self, name: str, value: Any):
        """Assign to existing variable — walk up scopes."""
        if name in self.vars:
            self.vars[name] = value
            return
        if self.parent:
            self.parent.assign(name, value)
            return
        # not found anywhere — create in current scope
        self.vars[name] = value

    def drop(self, name: str):
        if name in self.vars:
            del self.vars[name]
        elif self.parent:
            self.parent.drop(name)
        else:
            raise AxiomaError(f"Cannot drop undefined variable '{name}'")


# ══════════════════════════════════════════════════════════════════════════════
#  Axioma callable types
# ══════════════════════════════════════════════════════════════════════════════

class AxiomaFunction:
    def __init__(self, node: FuncDef, closure: Environment):
        self.node    = node
        self.closure = closure

    def __repr__(self):
        return f"<function {self.node.name}>"


class AxiomaLambda:
    def __init__(self, node: LambdaExpr, closure: Environment):
        self.node    = node
        self.closure = closure

    def __repr__(self):
        return "<fn>"


class AxiomaStructClass:
    """The class-level object for a structure definition."""
    def __init__(self, node: StructDef, closure: Environment):
        self.node    = node
        self.closure = closure

    def __repr__(self):
        return f"<structure {self.node.name}>"


class AxiomaInstance:
    """A live instance of an Axioma structure."""
    def __init__(self, struct_class: AxiomaStructClass):
        self.struct_class = struct_class
        self.fields: dict[str, Any] = {}

    def __repr__(self):
        fields = ", ".join(f"{k}: {axioma_repr(v)}" for k, v in self.fields.items())
        return f"{self.struct_class.node.name}({fields})"


class AxiomaGenerator:
    """Wraps a Python generator for emit/expand."""
    def __init__(self, gen):
        self.gen = gen

    def __iter__(self):
        return self.gen

    def __repr__(self):
        return "<generator>"


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def axioma_repr(value: Any) -> str:
    if value is None:          return "Null"
    if value is True:          return "T"
    if value is False:         return "F"
    if isinstance(value, str): return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(axioma_repr(v) for v in value) + "]"
    if isinstance(value, set):
        return "{" + ", ".join(axioma_repr(v) for v in sorted(value, key=str)) + "}"
    if isinstance(value, dict):
        pairs = ", ".join(f"{axioma_repr(k)}: {axioma_repr(v)}" for k, v in value.items())
        return "{" + pairs + "}"
    return str(value)


def axioma_str(value: Any) -> str:
    """Used by out() — no quotes around strings."""
    if value is None:  return "Null"
    if value is True:  return "T"
    if value is False: return "F"
    if isinstance(value, list):
        return "[" + ", ".join(axioma_repr(v) for v in value) + "]"
    if isinstance(value, set):
        return "{" + ", ".join(axioma_repr(v) for v in sorted(value, key=str)) + "}"
    if isinstance(value, dict):
        pairs = ", ".join(f"{axioma_repr(k)}: {axioma_repr(v)}" for k, v in value.items())
        return "{" + pairs + "}"
    return str(value)


def resolve_interpolation(raw: str, env: Environment) -> str:
    """Replace {varname} placeholders in an s"..." string."""
    def replacer(m):
        expr_src = m.group(1).strip()
        try:
            tokens = Lexer(expr_src).tokenize()
            node   = Parser(tokens).parse()
            # parse() returns a Program; evaluate its first statement
            interp = Interpreter()
            interp.env = env
            val = interp.eval_expr(node.body[0].expr if hasattr(node.body[0], 'expr') else node.body[0])
            return axioma_str(val)
        except Exception:
            return m.group(0)
    return re.sub(r"\{([^}]+)\}", replacer, raw)


# ══════════════════════════════════════════════════════════════════════════════
#  Built-in functions
# ══════════════════════════════════════════════════════════════════════════════

def make_builtins() -> dict[str, Any]:
    def builtin_out(*args):
        print(*[axioma_str(a) for a in args])
        return None

    def builtin_in(prompt=""):
        return input(axioma_str(prompt) if prompt != "" else "")

    def builtin_type(x):
        if x is None:               return "Null"
        if isinstance(x, bool):     return "Bool"
        if isinstance(x, int):      return "ZZ"
        if isinstance(x, float):    return "RR"
        if isinstance(x, complex):  return "CC"
        if isinstance(x, str):      return "CharSequence"
        if isinstance(x, list):     return "Sequence"
        if isinstance(x, tuple):    return "Tuple"
        if isinstance(x, dict):     return "Mapping"
        if isinstance(x, set):      return "Set"
        if isinstance(x, AxiomaInstance):
            return x.struct_class.node.name
        return type(x).__name__

    def builtin_sup(seq):
        if isinstance(seq, (list, set)):
            return max(seq)
        raise AxiomaError("sup() requires a Sequence or Set")

    def builtin_inf(seq):
        if isinstance(seq, (list, set)):
            return min(seq)
        raise AxiomaError("inf() requires a Sequence or Set")

    def builtin_sort(seq):
        if isinstance(seq, list):
            return sorted(seq)
        raise AxiomaError("sort() requires a Sequence")

    def builtin_reverse(seq):
        if isinstance(seq, list):
            return list(reversed(seq))
        raise AxiomaError("reverse() requires a Sequence")

    def builtin_round(x, n=0):
        return round(x, n)

    def builtin_apply(fn, seq):
        return [_call_fn(fn, [x]) for x in seq]

    def builtin_select(fn, seq):
        return [x for x in seq if _call_fn(fn, [x])]

    def builtin_fold(fn, acc, seq):
        for x in seq:
            acc = _call_fn(fn, [acc, x])
        return acc

    def builtin_indexed(seq):
        return list(enumerate(seq))

    def builtin_combine(*seqs):
        return list(zip(*seqs))

    def builtin_nrt(n, x):
        # Accept a single-element set/frozenset as degree: {2}rt(x)
        if isinstance(n, (set, frozenset)):
            n = next(iter(n))
        return x ** (1 / n)

    def builtin_stringify(x):
        return axioma_repr(x)

    def builtin_format(x, fmt=""):
        return format(x, fmt)

    def builtin_char(n):
        return chr(n)

    def builtin_code(c):
        return ord(c)

    def builtin_base2(n):
        return bin(n)

    def builtin_base8(n):
        return oct(n)

    def builtin_base16(n):
        return hex(n)

    def builtin_hash(x):
        return hash(x)

    def builtin_identity(x):
        return id(x)

    def builtin_is_function(x):
        return isinstance(x, (AxiomaFunction, AxiomaLambda))

    def builtin_exists(seq, fn):
        return any(_call_fn(fn, [x]) for x in seq)

    def builtin_forall(seq, fn):
        return all(_call_fn(fn, [x]) for x in seq)

    def builtin_plot(fn, start_or_domain, end=None, step=1):
        """Render a two-column table of x → f(x) values."""
        # Resolve domain
        if end is None:
            # plot(fn, sequence)
            domain = list(start_or_domain)
        else:
            # plot(fn, start, end) or plot(fn, start, end, step)
            s = int(start_or_domain)
            e = int(end)
            st = int(step)
            domain = list(range(s, e + 1, st))

        rows = []
        for x in domain:
            try:
                y = _call_fn(fn, [x])
            except Exception as exc:
                y = f"Error: {exc}"
            rows.append((x, y))

        def _fmt(v):
            if isinstance(v, float):
                # trim unnecessary trailing zeros
                s = f"{v:.6f}".rstrip("0").rstrip(".")
                return s
            return str(v)

        x_vals  = [_fmt(r[0]) for r in rows]
        y_vals  = [_fmt(r[1]) for r in rows]
        x_w = max(len("x"),  max(len(v) for v in x_vals))
        y_w = max(len("f(x)"), max(len(v) for v in y_vals))

        header = f" {'x':>{x_w}} \u2502 {'f(x)':<{y_w}}"
        sep    = "\u2500" * (x_w + 2) + "\u253c" + "\u2500" * (y_w + 2)
        lines  = [header, sep]
        for xv, yv in zip(x_vals, y_vals):
            lines.append(f" {xv:>{x_w}} \u2502 {yv:<{y_w}}")
        print("\n".join(lines))
        return None

    def builtin_sum(fn, start, end, step=1):
        """Sigma notation: Σ fn(n) for n from start to end inclusive"""
        start, end, step = int(start), int(end), int(step)
        return sum(_call_fn(fn, [n]) for n in range(start, end + 1, step))

    def builtin_prod(fn, start, end, step=1):
        """Pi notation: Π fn(n) for n from start to end inclusive"""
        start, end, step = int(start), int(end), int(step)
        result = 1
        for n in range(start, end + 1, step):
            result *= _call_fn(fn, [n])
        return result

    def builtin_deriv(fn, x, h=1e-7):
        """Numerical derivative via central difference: (f(x+h) - f(x-h)) / (2h)"""
        x = float(x)
        h = float(h)
        return (_call_fn(fn, [x + h]) - _call_fn(fn, [x - h])) / (2 * h)

    def builtin_integ(fn, a, b, n=1000):
        """Definite integral via composite Simpson's rule"""
        a, b = float(a), float(b)
        n = int(n)
        if n % 2 != 0:
            n += 1  # Simpson's rule requires even number of intervals
        h = (b - a) / n
        total = _call_fn(fn, [a]) + _call_fn(fn, [b])
        for i in range(1, n):
            x = a + i * h
            total += (4 if i % 2 != 0 else 2) * _call_fn(fn, [x])
        return (h / 3) * total

    # Type constructors
    def builtin_ZZ(x):     return int(x)
    def builtin_RR(x):     return float(x)
    def builtin_CC(x):     return complex(x)
    def builtin_Bool(x):   return bool(x)
    def builtin_String(x): return str(x)
    def builtin_Sequence(*args): return list(args)
    def builtin_Tuple(*args):    return tuple(args)
    def builtin_Set(*args):      return set(args)
    def builtin_Mapping(**kw):   return dict(kw)

    return {
        "out":        builtin_out,
        "in":         builtin_in,
        "type":       builtin_type,
        "sup":        builtin_sup,
        "inf":        builtin_inf,
        "sort":       builtin_sort,
        "reverse":    builtin_reverse,
        "round":      builtin_round,
        "apply":      builtin_apply,
        "select":     builtin_select,
        "fold":       builtin_fold,
        "indexed":    builtin_indexed,
        "combine":    builtin_combine,
        "nrt":        builtin_nrt,
        "stringify":  builtin_stringify,
        "format":     builtin_format,
        "char":       builtin_char,
        "code":       builtin_code,
        "base2":      builtin_base2,
        "base8":      builtin_base8,
        "base16":     builtin_base16,
        "hash":       builtin_hash,
        "identity":   builtin_identity,
        "is_function":builtin_is_function,
        "exists":     builtin_exists,
        "forall":     builtin_forall,
        "plot":       builtin_plot,
        "sum":        builtin_sum,
        "prod":       builtin_prod,
        "deriv":      builtin_deriv,
        "integ":      builtin_integ,
        # type constructors
        "ZZ":         builtin_ZZ,
        "RR":         builtin_RR,
        "CC":         builtin_CC,
        "Bool":       builtin_Bool,
        "String":     builtin_String,
        "Sequence":   builtin_Sequence,
        "Tuple":      builtin_Tuple,
        "Set":        builtin_Set,
        "Mapping":    builtin_Mapping,
        # math constants
        "PI":  math.pi,
        "E":   math.e,
        "Null": None,
        "T":    True,
        "F":    False,
    }


def _call_fn(fn: Any, args: list) -> Any:
    """Call an AxiomaFunction or AxiomaLambda with positional args."""
    interp = Interpreter()
    return interp._invoke(fn, args)


# ══════════════════════════════════════════════════════════════════════════════
#  Interpreter
# ══════════════════════════════════════════════════════════════════════════════

class Interpreter:
    def __init__(self):
        self.global_env = Environment()
        for name, val in make_builtins().items():
            self.global_env.set(name, val)
        self.env = self.global_env

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, source: str):
        tokens = Lexer(source).tokenize()
        ast    = Parser(tokens).parse()
        self.exec_program(ast)

    def run_file(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.run(f.read())

    # ── Program / Block ───────────────────────────────────────────────────────

    def exec_program(self, node: Program):
        for stmt in node.body:
            self.exec_stmt(stmt)

    def exec_block(self, node: Block, env: Optional[Environment] = None, scoped: bool = True):
        outer = self.env
        if env is not None:
            self.env = env
        elif scoped:
            self.env = Environment(parent=self.env)
        # scoped=False and no explicit env → run in current env (control-flow blocks)
        try:
            for stmt in node.stmts:
                self.exec_stmt(stmt)
        finally:
            self.env = outer

    # ── Statement dispatcher ──────────────────────────────────────────────────

    def exec_stmt(self, node: Node):
        t = type(node)

        if t == VarDecl:      self.exec_var_decl(node)
        elif t == FuncDef:    self.exec_func_def(node)
        elif t == ReturnStmt: raise ReturnSignal(self.eval_expr(node.value))
        elif t == IfStmt:     self.exec_if(node)
        elif t == EachStmt:   self.exec_each(node)
        elif t == RepeatStmt: self.exec_repeat(node)
        elif t == MatchStmt:  self.exec_match(node)
        elif t == CheckStmt:  self.exec_check(node)
        elif t == AbortStmt:  self.exec_abort(node)
        elif t == UsingStmt:  pass   # module system: skip for now
        elif t == FromStmt:   pass
        elif t == StructDef:  self.exec_struct_def(node)
        elif t == ExprStmt:   self.eval_expr(node.expr)
        elif t == SkipStmt:   pass
        elif t == StopStmt:   raise StopSignal()
        elif t == NextStmt:   raise NextSignal()
        elif t == DropStmt:   self.env.drop(node.name)
        elif t == VerifyStmt: self.exec_verify(node)
        elif t == EmitStmt:   raise ReturnSignal(self.eval_expr(node.value))
        elif t == ExpandStmt: self.exec_expand(node)
        elif t == UsingAsStmt:self.exec_using_as(node)
        elif t == IfMain:     self.exec_block(node.body)
        elif t == Assign:     self.exec_assign(node)
        else:
            raise AxiomaError(f"Unknown statement type: {t.__name__}")

    # ── Variable declaration ──────────────────────────────────────────────────

    def exec_var_decl(self, node: VarDecl):
        value = self.eval_expr(node.value) if node.value else None
        self.env.set(node.name, value)

    # ── Function definition ───────────────────────────────────────────────────

    def exec_func_def(self, node: FuncDef):
        fn = AxiomaFunction(node, self.env)
        self.env.set(node.name, fn)

    # ── Structure definition ──────────────────────────────────────────────────

    def exec_struct_def(self, node: StructDef):
        cls = AxiomaStructClass(node, self.env)
        self.env.set(node.name, cls)

    # ── If statement ──────────────────────────────────────────────────────────

    def exec_if(self, node: IfStmt):
        for condition, block in node.branches:
            if self._truthy(self.eval_expr(condition)):
                self.exec_block(block, scoped=False)
                return
        if node.otherwise:
            self.exec_block(node.otherwise, scoped=False)

    # ── each loop ─────────────────────────────────────────────────────────────

    def exec_each(self, node: EachStmt):
        iterable = self.eval_expr(node.iter)
        iterable = self._to_iterable(iterable)
        for item in iterable:
            self.env.set(node.var, item)
            try:
                self.exec_block(node.body, scoped=False)
            except StopSignal:
                break
            except NextSignal:
                continue

    # ── repeat-until ──────────────────────────────────────────────────────────

    def exec_repeat(self, node: RepeatStmt):
        while True:
            try:
                self.exec_block(node.body, scoped=False)
            except StopSignal:
                break
            except NextSignal:
                pass
            if self._truthy(self.eval_expr(node.condition)):
                break

    # ── match ─────────────────────────────────────────────────────────────────

    def exec_match(self, node: MatchStmt):
        subject = self.eval_expr(node.subject)
        for val_node, block in node.cases:
            if self.eval_expr(val_node) == subject:
                self.exec_block(block, scoped=False)
                return
        if node.default:
            self.exec_block(node.default, scoped=False)

    # ── check / catch / resolve / always ─────────────────────────────────────

    def exec_check(self, node: CheckStmt):
        error_occurred = False
        try:
            self.exec_block(node.body)
        except AxiomaError as e:
            error_occurred = True
            handled = False
            for clause in node.catches:
                # match by type name or catch-all
                if clause.error_type is None or clause.error_type == type(e.value).__name__ or True:
                    env = Environment(parent=self.env)
                    if clause.alias:
                        env.set(clause.alias, str(e.value))
                    self.exec_block(clause.body, env)
                    handled = True
                    break
            if not handled:
                raise
        else:
            if node.resolve and not error_occurred:
                self.exec_block(node.resolve)
        finally:
            if node.always:
                self.exec_block(node.always)

    # ── abort ─────────────────────────────────────────────────────────────────

    def exec_abort(self, node: AbortStmt):
        value = self.eval_expr(node.value)
        cause = self.eval_expr(node.cause) if node.cause else None
        raise AxiomaError(value, cause)

    # ── verify ────────────────────────────────────────────────────────────────

    def exec_verify(self, node: VerifyStmt):
        result = self.eval_expr(node.expr)
        if not self._truthy(result):
            raise AxiomaError(f"Verification failed: {axioma_repr(node.expr)}")

    # ── expand ────────────────────────────────────────────────────────────────

    def exec_expand(self, node: ExpandStmt):
        value = self.eval_expr(node.value)
        # In a generator context, expand yields each item
        # In a regular context, it's a no-op (generator delegation handled by caller)
        _ = value

    # ── using ... as (context manager) ───────────────────────────────────────

    def exec_using_as(self, node: UsingAsStmt):
        resource = self.eval_expr(node.expr)
        # call on_open if it's an AxiomaInstance
        if isinstance(resource, AxiomaInstance):
            self._call_method(resource, "on_open", [])
        env = Environment(parent=self.env)
        env.set(node.alias, resource)
        try:
            self.exec_block(node.body, env)
        finally:
            if isinstance(resource, AxiomaInstance):
                self._call_method(resource, "on_close", [])

    # ── assignment ────────────────────────────────────────────────────────────

    def exec_assign(self, node: Assign):
        value = self.eval_expr(node.value)

        # augmented assignment
        if node.op != ":=":
            current = self._resolve_target(node.target)
            if   node.op == "+=": value = current + value
            elif node.op == "-=": value = current - value
            elif node.op == "*=": value = current * value
            elif node.op == "/=": value = current / value

        self._assign_target(node.target, value)

    def _resolve_target(self, target: Node) -> Any:
        if isinstance(target, Identifier):
            return self.env.get(target.name)
        if isinstance(target, Projection):
            obj = self.eval_expr(target.obj)
            if isinstance(obj, AxiomaInstance):
                return obj.fields.get(target.field)
        raise AxiomaError(f"Cannot resolve assignment target")

    def _assign_target(self, target: Node, value: Any):
        if isinstance(target, Identifier):
            self.env.assign(target.name, value)
        elif isinstance(target, Projection):
            obj = self.eval_expr(target.obj)
            if isinstance(obj, AxiomaInstance):
                obj.fields[target.field] = value
            else:
                raise AxiomaError(f"Cannot set field on non-instance")
        elif isinstance(target, Subscript):
            obj = self.eval_expr(target.obj)
            idx = self.eval_expr(target.index)
            if isinstance(obj, list):
                obj[idx] = value
            elif isinstance(obj, dict):
                obj[idx] = value
            else:
                raise AxiomaError("Subscript assignment requires Sequence or Mapping")
        else:
            raise AxiomaError(f"Invalid assignment target")

    # ══════════════════════════════════════════════════════════════════════════
    #  Expression evaluator
    # ══════════════════════════════════════════════════════════════════════════

    def eval_expr(self, node: Node) -> Any:
        t = type(node)

        if t == Literal:             return node.value
        if t == InterpolatedString:  return resolve_interpolation(node.raw, self.env)
        if t == MathSet:             return node.name   # just a string tag for now
        if t == Identifier:          return self._eval_ident(node)
        if t == BinaryOp:            return self._eval_binary(node)
        if t == UnaryOp:             return self._eval_unary(node)
        if t == LenOp:               return len(self.eval_expr(node.operand))
        if t == Call:                return self._eval_call(node)
        if t == Projection:          return self._eval_projection(node)
        if t == Subscript:           return self._eval_subscript(node)
        if t == Slice:               return self._eval_slice(node)
        if t == Range:               return self._eval_range(node)
        if t == Ternary:             return self._eval_ternary(node)
        if t == LambdaExpr:          return AxiomaLambda(node, self.env)
        if t == ListExpr:            return [self.eval_expr(e) for e in node.elements]
        if t == SetExpr:             return set(self.eval_expr(e) for e in node.elements)
        if t == MappingExpr:         return {self.eval_expr(k): self.eval_expr(v) for k, v in node.pairs}
        if t == ListComp:            return self._eval_list_comp(node)
        if t == SetComp:             return self._eval_set_comp(node)
        if t == MappingComp:         return self._eval_mapping_comp(node)
        if t == GeneratorComp:       return self._eval_generator_comp(node)
        if t == Assign:              self.exec_assign(node); return None
        if t == CollectExpr:         return self.eval_expr(node.value)   # sync for now
        if t == SpawnExpr:           return self.eval_expr(node.value)   # sync for now
        if t == ExprStmt:            return self.eval_expr(node.expr)

        raise AxiomaError(f"Unknown expression type: {t.__name__}")

    # ── Identifier ────────────────────────────────────────────────────────────

    def _eval_ident(self, node: Identifier) -> Any:
        # built-in constants
        if node.name == "T":    return True
        if node.name == "F":    return False
        if node.name == "Null": return None
        return self.env.get(node.name)

    # ── Binary operations ─────────────────────────────────────────────────────

    def _eval_binary(self, node: BinaryOp) -> Any:
        # short-circuit logical operators
        if node.op == "&&":
            return self._truthy(self.eval_expr(node.left)) and self._truthy(self.eval_expr(node.right))
        if node.op == "||":
            return self._truthy(self.eval_expr(node.left)) or self._truthy(self.eval_expr(node.right))

        left  = self.eval_expr(node.left)
        right = self.eval_expr(node.right)

        op = node.op
        if op == "+":
            if isinstance(left, list) and isinstance(right, list):
                return left + right
            return left + right
        if op == "-":   return left - right
        if op == "*":   return left * right
        if op == "/":
            if right == 0: raise AxiomaError("Division by zero")
            return left / right
        if op == "//":
            if right == 0: raise AxiomaError("Division by zero")
            return left // right
        if op == "%":   return left % right
        if op == "**":  return left ** right
        if op == "===": return left == right
        if op == "=!=": return left != right
        if op == "<":   return left < right
        if op == ">":   return left > right
        if op == "<=":  return left <= right
        if op == ">=":  return left >= right
        if op == "&":   return left & right
        if op == "|":   return left | right
        if op == "^":   return left ^ right
        if op == "<<":  return left << right
        if op == ">>":  return left >> right
        # math set operators
        if op == "union":
            return self._to_set(left) | self._to_set(right)
        if op == "intersect":
            return self._to_set(left) & self._to_set(right)
        if op == "exclude":
            return self._to_set(left) - self._to_set(right)
        if op == "cart":
            return [(a, b) for a in left for b in right]

        raise AxiomaError(f"Unknown binary operator: {op}")

    # ── Unary operations ──────────────────────────────────────────────────────

    def _eval_unary(self, node: UnaryOp) -> Any:
        val = self.eval_expr(node.operand)
        if node.op == "-":  return -val
        if node.op == "!!": return not self._truthy(val)
        raise AxiomaError(f"Unknown unary operator: {node.op}")

    # ── Function call ─────────────────────────────────────────────────────────

    def _eval_call(self, node: Call) -> Any:
        callee = self.eval_expr(node.callee)
        args   = [self.eval_expr(a) for a in node.args]
        return self._invoke(callee, args)

    def _invoke(self, callee: Any, args: list) -> Any:
        # Python built-in
        if callable(callee) and not isinstance(callee, (AxiomaFunction, AxiomaLambda, AxiomaStructClass)):
            return callee(*args)

        # Structure instantiation
        if isinstance(callee, AxiomaStructClass):
            return self._instantiate(callee, args)

        # Axioma function
        if isinstance(callee, AxiomaFunction):
            return self._call_axioma_fn(callee.node, callee.closure, args)

        # Lambda
        if isinstance(callee, AxiomaLambda):
            return self._call_lambda(callee, args)

        raise AxiomaError(f"'{axioma_repr(callee)}' is not callable")

    def _call_axioma_fn(self, node: FuncDef, closure: Environment, args: list) -> Any:
        env = Environment(parent=closure)
        self._bind_params(node.params, args, env)
        outer = self.env
        self.env = env
        try:
            self.exec_block(node.body, env)
            return None
        except ReturnSignal as r:
            return r.value
        finally:
            self.env = outer

    def _call_lambda(self, lam: AxiomaLambda, args: list) -> Any:
        env = Environment(parent=lam.closure)
        self._bind_params(lam.node.params, args, env)
        outer = self.env
        self.env = env
        try:
            return self.eval_expr(lam.node.body)
        finally:
            self.env = outer

    def _bind_params(self, params: List[Param], args: list, env: Environment):
        for i, param in enumerate(params):
            if param.variadic:
                env.set(param.name, list(args[i:]))
                return
            if i < len(args):
                env.set(param.name, args[i])
            else:
                env.set(param.name, None)

    # ── Structure instantiation ───────────────────────────────────────────────

    def _instantiate(self, cls: AxiomaStructClass, args: list) -> AxiomaInstance:
        instance = AxiomaInstance(cls)

        # inherit fields from base if any
        if cls.node.base:
            base_cls = self.env.get(cls.node.base)
            if isinstance(base_cls, AxiomaStructClass):
                base_inst = self._instantiate(base_cls, [])
                instance.fields.update(base_inst.fields)

        # set up instance environment with 'this'
        env = Environment(parent=cls.closure)
        env.set("this", instance)

        outer = self.env
        self.env = env

        # find and run create()
        for member in cls.node.members:
            if isinstance(member, FuncDef) and member.name == "create":
                self._call_axioma_fn(member, env, args)
                break

        self.env = outer
        return instance

    def _call_method(self, instance: AxiomaInstance, method_name: str, args: list) -> Any:
        cls = instance.struct_class
        for member in cls.node.members:
            if isinstance(member, FuncDef) and member.name == method_name:
                env = Environment(parent=cls.closure)
                env.set("this", instance)
                return self._call_axioma_fn(member, env, args)
        return None   # method not found — silently skip

    # ── Projection (obj'field or obj'method()) ────────────────────────────────

    def _eval_projection(self, node: Projection) -> Any:
        obj = self.eval_expr(node.obj)

        if isinstance(obj, AxiomaInstance):
            # Check fields first
            if node.field in obj.fields:
                return obj.fields[node.field]
            # Then check methods
            cls = obj.struct_class
            for member in cls.node.members:
                if isinstance(member, FuncDef) and member.name == node.field:
                    # Return a bound method
                    def make_bound(m, inst):
                        def bound(*a):
                            env = Environment(parent=cls.closure)
                            env.set("this", inst)
                            return self._call_axioma_fn(m, env, list(a))
                        return bound
                    return make_bound(member, obj)
            raise AxiomaError(f"'{cls.node.name}' has no field or method '{node.field}'")

        # String methods
        if isinstance(obj, str):
            if node.field == "length": return len(obj)
            if node.field == "upper":  return obj.upper()
            if node.field == "lower":  return obj.lower()

        # List methods
        if isinstance(obj, list):
            if node.field == "length": return len(obj)

        raise AxiomaError(f"Cannot project '{node.field}' on {axioma_repr(obj)}")

    # ── Subscript ─────────────────────────────────────────────────────────────

    def _eval_subscript(self, node: Subscript) -> Any:
        obj = self.eval_expr(node.obj)
        idx = self.eval_expr(node.index)
        try:
            return obj[idx]
        except (IndexError, KeyError) as e:
            raise AxiomaError(f"Index error: {e}")

    # ── Slice ─────────────────────────────────────────────────────────────────

    def _eval_slice(self, node: Slice) -> Any:
        obj   = self.eval_expr(node.obj)
        start = self.eval_expr(node.start)
        stop  = self.eval_expr(node.stop)
        step  = self.eval_expr(node.step) if node.step else 1
        if isinstance(obj, list):
            return obj[start:stop:step]
        raise AxiomaError("Slice requires a Sequence")

    # ── Range ─────────────────────────────────────────────────────────────────

    def _eval_range(self, node: Range) -> Any:
        start = self.eval_expr(node.start)
        stop  = self.eval_expr(node.stop)
        return list(range(start, stop + 1))   # inclusive like math notation

    # ── Ternary ───────────────────────────────────────────────────────────────

    def _eval_ternary(self, node: Ternary) -> Any:
        if self._truthy(self.eval_expr(node.condition)):
            return self.eval_expr(node.then_expr)
        return self.eval_expr(node.else_expr)

    # ── Comprehensions ────────────────────────────────────────────────────────

    def _eval_list_comp(self, node: ListComp) -> list:
        result = []
        for item in self._to_iterable(self.eval_expr(node.iter)):
            env = Environment(parent=self.env)
            env.set(node.var, item)
            outer, self.env = self.env, env
            try:
                if all(self._truthy(self.eval_expr(c)) for c in node.conditions):
                    result.append(self.eval_expr(node.expr))
            finally:
                self.env = outer
        return result

    def _eval_set_comp(self, node: SetComp) -> set:
        result = set()
        for item in self._to_iterable(self.eval_expr(node.iter)):
            env = Environment(parent=self.env)
            env.set(node.var, item)
            outer, self.env = self.env, env
            try:
                if all(self._truthy(self.eval_expr(c)) for c in node.conditions):
                    result.add(self.eval_expr(node.expr))
            finally:
                self.env = outer
        return result

    def _eval_mapping_comp(self, node: MappingComp) -> dict:
        result = {}
        for item in self._to_iterable(self.eval_expr(node.iter)):
            env = Environment(parent=self.env)
            env.set(node.var, item)
            outer, self.env = self.env, env
            try:
                k = self.eval_expr(node.key_expr)
                v = self.eval_expr(node.val_expr)
                result[k] = v
            finally:
                self.env = outer
        return result

    def _eval_generator_comp(self, node: GeneratorComp):
        def gen():
            for item in self._to_iterable(self.eval_expr(node.iter)):
                env = Environment(parent=self.env)
                env.set(node.var, item)
                outer, self.env = self.env, env
                try:
                    yield self.eval_expr(node.expr)
                finally:
                    self.env = outer
        return AxiomaGenerator(gen())

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _truthy(self, val: Any) -> bool:
        if val is None or val is False: return False
        if isinstance(val, (int, float)) and val == 0: return False
        if isinstance(val, (str, list, dict, set)) and len(val) == 0: return False
        return True

    def _to_set(self, val: Any) -> set:
        if isinstance(val, set):  return val
        if isinstance(val, list): return set(val)
        raise AxiomaError(f"Expected Set, got {axioma_repr(val)}")

    def _to_iterable(self, val: Any):
        if isinstance(val, (list, set, tuple, dict)):
            return val
        if isinstance(val, AxiomaGenerator):
            return val
        if isinstance(val, range):
            return val
        raise AxiomaError(f"'{axioma_repr(val)}' is not iterable")


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

DEMO = """
-- ─────────────────────────────────────────
--  Axioma Demo Program
-- ─────────────────────────────────────────

-- 1. Hello World
out("── Hello World ──")
out("Hello, Axioma!")

-- 2. Variables & types
out("── Variables ──")
let x : ZZ := 10
let pi : RR := 3.14159
let name : CharSequence := "Axioma"
out(s"x = {x}, pi = {pi}, name = {name}")

-- 3. Arithmetic
out("── Arithmetic ──")
out(s"2 ** 10 = {2 ** 10}")
out(s"17 // 3 = {17 // 3}")
out(s"17 %  3 = {17 % 3}")

-- 4. Conditionals
out("── Conditionals ──")
let x > 5 begin
  out("x is greater than 5")
end otherwise begin
  out("x is 5 or less")
end

-- 5. Each loop with range
out("── Each loop ──")
each i in {1..5} begin
  out(s"  i = {i}")
end

-- 6. repeat-until
out("── Repeat-until ──")
let count := 0
repeat begin
  count += 1
end until count === 3
out(s"count after repeat = {count}")

-- 7. Function
out("── Functions ──")
where square(n : ZZ) : ZZ begin
  report n * n
end
out(s"square(7) = {square(7)}")

-- 8. Recursion (Fibonacci)
out("── Fibonacci ──")
where fib(n : ZZ) : ZZ begin
  let n <= 1 begin
    report n
  end
  report fib(n - 1) + fib(n - 2)
end
each i in {0..9} begin
  out(s"fib({i}) = {fib(i)}")
end

-- 9. Lambda & higher-order
out("── Lambdas ──")
let double := fn(x) => x * 2
let nums := [1, 2, 3, 4, 5]
let doubled := apply(double, nums)
out(s"doubled = {doubled}")
let evens := select(fn(x) => x % 2 === 0, nums)
out(s"evens = {evens}")
let total := fold(fn(acc, x) => acc + x, 0, nums)
out(s"sum = {total}")

-- 10. List comprehension
out("── Comprehensions ──")
let squares := [n * n | n in {1..6}]
out(s"squares = {squares}")
let even_sq := {n * n | n in {1..6}, n % 2 === 0}
out(s"even squares = {even_sq}")

-- 11. Ternary
out("── Ternary ──")
let score := 85
let grade := score >= 90 ? "A" : score >= 70 ? "B" : "C"
out(s"grade = {grade}")

-- 12. Structure
out("── Structures ──")
structure Point :=
  x : RR
  y : RR
  create(px : RR, py : RR) begin
    this'x := px
    this'y := py
  end
  where distance() : RR begin
    report (this'x ** 2 + this'y ** 2) ** 0.5
  end
  where to_string() : CharSequence begin
    report s"Point({this'x}, {this'y})"
  end
end

let p := Point(3.0, 4.0)
out(s"distance = {p'distance()}")
out(p'to_string())

-- 13. Error handling
out("── Error handling ──")
check begin
  abort "something went wrong"
end catch Error as e begin
  out(s"Caught: {e}")
end always begin
  out("Cleanup ran.")
end

-- 14. Verify
out("── Verify ──")
verify #nums === 5
out("Verification passed!")

out("── Done! ──")
"""

if __name__ == "__main__":
    interp = Interpreter()
    if len(sys.argv) > 1:
        interp.run_file(sys.argv[1])
    else:
        print("No file given — running built-in demo.\n")
        try:
            interp.run(DEMO)
        except AxiomaError as e:
            print(f"\nAxiomaError: {e}")
        except Exception as e:
            print(f"\nInternal error: {e}")
            raise
