"""
Axioma Runner — reads source code from stdin, executes it, prints to stdout.
Any error is printed to stderr with exit code 1.
"""
import sys
import io
import os

sys.path.insert(0, os.path.dirname(__file__))

from axioma_lexer import LexerError
from axioma_parser import ParseError
from interpreter import Interpreter, AxiomaError

def main():
    source = sys.stdin.read()
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = captured_out
    sys.stderr = captured_err
    exit_code = 0
    try:
        interp = Interpreter()
        interp.run(source)
    except (LexerError, ParseError, AxiomaError) as e:
        exit_code = 1
        sys.stderr = old_stderr
        sys.stdout = old_stdout
        output = captured_out.getvalue()
        if output:
            print(output, end="")
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except SystemExit as e:
        exit_code = e.code if e.code is not None else 0
        sys.stderr = old_stderr
        sys.stdout = old_stdout
        output = captured_out.getvalue()
        if output:
            print(output, end="")
        sys.exit(exit_code)
    except Exception as e:
        exit_code = 2
        sys.stderr = old_stderr
        sys.stdout = old_stdout
        output = captured_out.getvalue()
        if output:
            print(output, end="")
        print(f"Internal error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        if sys.stdout is not old_stdout:
            sys.stdout = old_stdout
        if sys.stderr is not old_stderr:
            sys.stderr = old_stderr

    output = captured_out.getvalue()
    if output:
        print(output, end="")

if __name__ == "__main__":
    main()
