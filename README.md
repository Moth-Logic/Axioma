# Modern Axioma language files

This bundle contains the current Axioma language engine, including the updates made after the original uploaded snapshots.

## Included files

- `axioma_lexer.py` — current lexer and token definitions
- `axioma_parser.py` — current parser and syntax rules
- `interpreter.py` — current runtime and built-in functions
- `run_axioma.py` — stdin runner used to execute Axioma programs
- `__init__.py` — package initializer
- `pyrightconfig.json` — Python 3.11 type-checking configuration

## Current language additions

- Set operators: `union`, `intersect`, `exclude`, `cart`
- Numerical calculus: `deriv(...)`, `integ(...)`
- Sigma and pi operations: `sum(...)`, `prod(...)`
- Table-style function output: `plot(...)`
- Nth-root notation: `{2}rt(25)`, `{3}rt(900)`
- Callable numeric type constructors such as `RR(...)` and `ZZ(...)`
- Control-flow block variables remain available after the block; function bodies remain scoped

## Run locally

From this folder, pipe Axioma source into the runner:

```bash
python3 run_axioma.py < program.ax
```
