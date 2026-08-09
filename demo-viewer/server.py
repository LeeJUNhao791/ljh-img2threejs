#!/usr/bin/env python3
"""
img2threejs Demo Server
Serves the Vite frontend and provides API endpoints for the image-to-3D pipeline.
Handles ES modules from node_modules.
"""
import os
os.environ['PYTHONUNBUFFERED'] = '1'

import http.server
import io
import json
import mimetypes
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from http.server import HTTPServer

from qwen_pump_vision import analyze_pump_image

# Fix Windows encoding with line buffering
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

# Debug output
print("[DEBUG] Starting server.py...", flush=True)

# Configuration
PORT = 8765
VITE_PORT = 5173
FORGE_PATH = Path(__file__).parent.parent / "forge"
DEMO_PATH = Path(__file__).parent
PIC_PATH = DEMO_PATH / "pic"
OUTPUT_PATH = DEMO_PATH / "output"
SRC_PATH = DEMO_PATH / "src"
NODE_MODULES = DEMO_PATH / "node_modules"

# MIME types for ES modules
MIME_TYPES = {
    '.js': 'application/javascript',
    '.mjs': 'application/javascript',
    '.ts': 'application/typescript',
    '.json': 'application/json',
    '.css': 'text/css',
    '.html': 'text/html',
    '.glb': 'model/gltf-binary',
    '.gltf': 'model/gltf+json',
}

# Store generation status
generation_status = {
    "status": "idle",
    "progress": 0,
    "message": "",
    "modelPath": None,
    "error": None
}


def strip_typescript(content: str) -> str:
    """Remove TypeScript type annotations to make code valid JavaScript.

    Uses a token-based parser (regex + structure tracking) rather than
    line-by-line regex hackery, so we never modify operator characters
    like +=, =>, >=, ^=, ===, etc.
    """
    import re

    # 1. Drop import statements that still carry `import type` aliases up
    #    front (rare in generated code, but harmless).
    content = re.sub(r'^import\s+type\s+[^;]+;\s*\n', '', content, flags=re.MULTILINE)

    lines = content.split('\n')

    # Phase A: track type/interface blocks to skip entirely.
    skip_block = False
    skip_depth = 0
    out_lines = []

    for line in lines:
        stripped = line.lstrip()

        if skip_block:
            # Count braces on this line. Strings/comments are not a concern
            # here because the generated code uses no string-typed braces.
            skip_depth += line.count('{') - line.count('}')
            if skip_depth <= 0:
                skip_block = False
                skip_depth = 0
            continue

        # Start of a type/interface block.
        if re.match(r'^(export\s+)?type\s+\w', stripped) or re.match(r'^interface\s+\w', stripped):
            # If the line contains `{` we need to track depth; otherwise a
            # single-line type like `type X = 'something';` can be dropped.
            if '{' in line:
                skip_block = True
                skip_depth = line.count('{') - line.count('}')
                if skip_depth <= 0:
                    skip_block = False
            # Remove the whole line either way.
            continue

        out_lines.append(line)

    # Phase B: clean remaining lines.
    cleaned = []
    for line in out_lines:
        # Skip empty lines.
        if line.strip() == '':
            cleaned.append(line)
            continue

        stripped = line.lstrip()

        # 3. Remove return type before `{` or `=>`:
        #    `): T {` -> `) {`    and   `(a): T =>` -> `(a) =>`
        #    The type may contain `<>`, `[]`, `{}`, `|`, etc.
        line = strip_return_type(line)

        # 4. Remove `as Type` only outside of `import` lines. We protect
        #    `import * as THREE` explicitly. The type may be a generic
        #    like `Record<string, Foo>` or an inline object literal.
        if not stripped.startswith('import'):
            line = strip_as_type(line)

        # 5. Remove generic `<T>` from identifiers / function names.
        #    Only when angle brackets are not arrow comparison operators.
        #    A simple heuristic: only strip <...> on a word immediately
        #    followed by `(` or space + `function` (typical TS generic).
        line = re.sub(r'([A-Za-z_]\w*)<[A-Za-z_][\w,\s]*>(?=\s*[\(])', r'\1', line)

        # 6. Remove variable type annotations: `const x: T = v`, `let x: T`
        #    This is the trickiest because the type can contain `{}`, `[]`,
        #    `<>`, `|`, etc. We use a balanced-brace scanner.
        line = strip_var_type_in_line(line)

        # 6b. Remove parameter type annotations for multi-line parameter
        #     lists: lines like `canvas: HTMLCanvasElement,` that appear
        #     between `function foo(` and `): ReturnType {`.
        line = strip_multi_line_param_type(line)

        # 7. Remove type from arrow function parameter list, e.g.
        #    `(part: string) => ...` -> `(part) => ...`
        #    Note: this also covers `(value): value is string =>`
        line = re.sub(r'\s*=\s*>\s*', ' => ', line)
        line = strip_arrow_param_types(line)

        # Phase B.5: Strip ES2020 optional chaining (?.) and nullish coalescing (??)
        # Not all browsers support these (e.g. older Edge, some mobile browsers).
        # `?\.(?!\?)` matches `?.` but NOT `??` (negative lookahead).
        line = re.sub(r'\?\.(?!\?)', '.', line)  # `?.` -> `.`
        line = re.sub(r'\?\?', '||', line)       # `??` -> `||`
        cleaned.append(line)

    # Phase C: restore imports. If we now have `import * from 'three';`
    # (because `as THREE` was dropped), fix it.
    final = []
    for line in cleaned:
        m = re.match(r"^(\s*)import\s+\*\s+from\s+['\"]([^'\"]+)['\"]\s*;?\s*$", line)
        if m:
            indent = m.group(1)
            module = m.group(2)
            if 'three' in module.lower():
                name = 'THREE'
            else:
                last = module.rstrip('/').split('/')[-1]
                last = last.replace('.js', '').replace('.', '-')
                name = last[0].upper() + last[1:] if last else 'Mod'
            line = f"{indent}import * as {name} from '{module}';"
        final.append(line)

    return '\n'.join(final)


def strip_var_type_in_line(line: str) -> str:
    """Remove `const x: Type = ...` and `let x: Type;` annotations.

    Uses a balanced-brace scanner so the type may contain generics such
    as `Record<string, Foo[]>` or inline object literals like
    `{ margin?: number; ... }`.
    """
    import re

    # Match `const|let|var name` only (no leading '=' etc.)
    m = re.search(r'\b(const|let|var)\s+([A-Za-z_]\w*)', line)
    if not m:
        return line

    # Position after the identifier.
    scan = m.end()
    rest = line[scan:]

    # Skip whitespace.
    rest_strip = rest.lstrip()
    ws_len = len(rest) - len(rest_strip)
    scan += ws_len

    # Expect either `:` (type annotation) or `=`/`;` (no annotation).
    if scan >= len(line) or line[scan] != ':':
        return line

    # Found a `:` — but it might be inside a default value like
    # `const x = (a: number) => ...` (not a declaration type). Reject
    # if there's no identifier-like char token before this `:`.
    # We already know the prior token is an identifier, so it's safe.
    scan += 1  # skip ':'

    # Fast-forward through whitespace.
    while scan < len(line) and line[scan] in ' \t':
        scan += 1

    # Now scan the type expression respecting brackets, angle brackets,
    # and braces. For object-literal types like `{ margin?: number; ... }`
    # we also need to handle `?` markers which are not balanced.
    start = scan
    depth_angle = 0
    depth_paren = 0
    depth_brack = 0
    depth_brace = 0
    while scan < len(line):
        ch = line[scan]
        if ch == '<':
            depth_angle += 1
        elif ch == '>':
            if depth_angle > 0:
                depth_angle -= 1
            # bare `>` is a comparison operator; treat as type end
            else:
                break
        elif ch == '(':
            depth_paren += 1
        elif ch == ')':
            if depth_paren > 0:
                depth_paren -= 1
            else:
                break
        elif ch == '[':
            depth_brack += 1
        elif ch == ']':
            if depth_brack > 0:
                depth_brack -= 1
        elif ch == '{':
            depth_brace += 1
        elif ch == '}':
            if depth_brace > 0:
                depth_brace -= 1
        elif ch in ',;=' and depth_angle == 0 and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
            # End of type annotation: hit a comma, semicolon, or equals.
            # Wait — we want to MERGE through `=` so that remaining
            # code is `= value` not `= value` with extra spaces.
            break
        elif ch == '?':
            # `?` is allowed in object-type members (`margin?: number`).
            pass
        scan += 1

    type_end = scan

    # Reconstruct: keep what came before, then append what comes after
    # the type. Trim trailing whitespace from the prefix.
    prefix = line[:m.end()].rstrip()
    suffix = line[type_end:].lstrip()

    # Re-attach with a single space before = if the next char is `=`.
    if suffix.startswith('='):
        # Don't add a space if previous char is already not a space.
        prefix = prefix.rstrip()
        # Preserve operator spacing; if suffix is `= value` keep one space.
        suffix = ' ' + suffix
    return prefix + suffix


def strip_multi_line_param_type(line: str) -> str:
    """Strip type annotations from multi-line function parameter lists.

    A line like `  canvas: HTMLCanvasElement,` (a single continuation param
    inside a multi-line `function foo(\n  a: T,\n  b: U,\n)` block) should
    become `  canvas,`.
    """
    import re

    # Match `  identifier: Type` or `  identifier: Type = default,` (with optional
    # default value) at end of params. Allow `?` after identifier (`holes?: Type,`).
    #
    # The default-value clause uses `(?<![=<>!])` lookbehind to ensure the
    # `=` is NOT preceded by `<`/`>`/`=`/`!`, which would make it part of an
    # equality / comparison operator (`<=`, `>=`, `==`, `===`, `!=`, `!==`)
    # inside an expression rather than a default-value assignment. Without
    # this, regex backtracking would split `rest="spec.doubleSided"` and
    # `default=" === true ? ..."`, then drop `rest` entirely.
    m = re.match(r'^(\s*)([A-Za-z_]\w*)(\s*\??)\s*:\s*(.+?)((?<![=<>!])\s*=\s*[^,]+)?(,?\s*)$', line)
    if not m:
        return line
    # Be safe: skip if the line already looks stripped (no comma/paren/anything).
    indent, ident, optional, rest, default, tail = m.groups()
    default = default or ''
    # Heuristic: if the rest looks like a JS expression (contains operators,
    # parens, brackets, dot access, ternary), it's NOT a type annotation —
    # it's an expression on the right side of an object property assignment
    # like `color: textures ? 0xffffff : new THREE.Color(...)`. Leave it alone.
    #
    # NOTE: We deliberately exclude `.` and `()` from the bail-out set:
    # valid TS types include `THREE.ColorSpace`, `Record<K, V>`, `Foo<X>()`,
    # etc. The remaining markers (`=`, `=>`, `??`, `?.`, `&&`, `||`, `new`)
    # reliably identify JS expressions and not TS types.
    expr_markers = re.search(r'=>|\?\?|\?\.|&&|\|\||new ', rest)
    if expr_markers:
        return line
    # Also bail if `rest` contains comparison/equality operators (`===`,
    # `==`, `!==`, `!=`) or a ternary `?` — these never appear in a TS type
    # annotation in a parameter list (conditional types only appear inside
    # generic angle brackets, not at the top level here). Catches lines like
    # `side: spec.doubleSided === true ? THREE.X : THREE.Y,`.
    if re.search(r'===|!==|==|!=|\?', rest):
        return line
    # Bail out if rest contains 'as' or 'satisfies' — these are TypeScript
    # keywords that indicate a type assertion, not a type annotation.
    if ' as ' in rest or ' satisfies ' in rest:
        return line
    # Also bail out if `rest` starts with a lowercase identifier that looks
    # like an expression value rather than a type name (types usually start
    # with an uppercase letter or a primitive keyword).
    primitive_types = {
        'string', 'number', 'boolean', 'bigint', 'symbol', 'object',
        'any', 'unknown', 'never', 'void', 'undefined', 'null',
        'true', 'false', 'this', 'typeof', 'instanceof',
    }
    stripped_rest = rest.strip()
    first_token = re.match(r'\S+', stripped_rest)
    if first_token:
        # Examine the first *identifier character* (skip leading non-letters
        # like `_`, but consider `.` part of a dotted name). If the identifier
        # is lowercase and not a primitive type, it's an expression reference
        # (`spec.foo`, `myVar`, etc.), not a TS type annotation.
        first_char = first_token.group(0)[0]
        # Strip trailing `[]` / `[][]` etc. before checking the primitive set
        # so `string[]` is recognized as the primitive `string`.
        first_token_stripped = re.sub(r'(\[\])+$', '', first_token.group(0))
        if first_char.islower() and first_token_stripped not in primitive_types:
            # Lowercase first token suggests an expression (variable reference,
            # function call, etc.), not a type annotation. Bail out.
            # Note: types like `string[]`, `boolean`, etc. are in primitive_types;
            # user-defined type aliases usually start uppercase.
            return line
    # If the rest is short and looks like a TS primitive type, strip it.
    if stripped_rest in primitive_types:
        return f'{indent}{ident}{optional}{default}{tail}'
    # For generic types like `THREE.Something`, `Record<string, X>`, etc.,
    # also strip them - we trust this only matches valid param lines because
    # of the `^(\s*)([A-Za-z_]\w*)(\s*\??)\s*:\s*(.+?)(,?\s*)$` anchor.
    return f'{indent}{ident}{optional}{default}{tail}'

def strip_return_type(line: str) -> str:
    """Remove return type annotation before `{` or `=>`.

    Examples:
        `function foo(): T {`     -> `function foo() {`
        `(a: number): T =>`        -> `(a) =>`
        `(a): value is string =>`  -> `(a) =>`
    """
    import re

    # Find a `)` that is followed by `: type` and then `{` or `=>`.
    # Use a manual scan to balance brackets inside the type.
    result = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == ')':
            # Look ahead: skip whitespace, expect `:`.
            j = i + 1
            while j < len(line) and line[j] in ' \t':
                j += 1
            if j < len(line) and line[j] == ':':
                # Balanced scan of the type until we hit `{` or `=>`.
                scan = j + 1
                while scan < len(line) and line[scan] in ' \t':
                    scan += 1
                depth_angle = 0
                depth_paren = 0
                depth_brack = 0
                depth_brace = 0
                while scan < len(line):
                    c = line[scan]
                    if c == '<':
                        depth_angle += 1
                    elif c == '>':
                        if depth_angle > 0:
                            depth_angle -= 1
                        else:
                            break
                    elif c == '(':
                        depth_paren += 1
                    elif c == ')':
                        if depth_paren > 0:
                            depth_paren -= 1
                        else:
                            break
                    elif c == '[':
                        depth_brack += 1
                    elif c == ']':
                        if depth_brack > 0:
                            depth_brack -= 1
                    elif c == '{':
                        depth_brace += 1
                        # If we just opened a brace while all other bracket
                        # depths are 0, this `{` is the function body — the
                        # type annotation ends here. Don't advance scan so
                        # the outer loop will re-emit the `{` after we exit.
                        if depth_angle == 0 and depth_paren == 0 and depth_brack == 0 and depth_brace == 1:
                            break
                    elif c == '}':
                        if depth_brace > 0:
                            depth_brace -= 1
                    elif c == '=' and depth_angle == 0 and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
                        # `=` inside what we thought was a return type means
                        # it's actually a parameter default value. The return
                        # type scan stops here; the trailing default value
                        # will be preserved by strip_multi_line_param_type.
                        if scan + 1 < len(line) and line[scan + 1] == '>':
                            break
                        break
                    elif c == ';' and depth_angle == 0 and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
                        # A `;` inside the type expression is invalid TS.
                        # If we encounter one, this is NOT a return type
                        # — it's a ternary expression or similar. Bail out.
                        return line
                    else:
                        # Advance. (Anything else inside the type.)
                        pass
                    scan += 1
                # If we walked all the way to the end of the line without
                # hitting `{` (function body) or `=>` (arrow body), the `:`
                # we found was NOT a return-type annotation. Most commonly
                # this is a ternary expression like `cond ? a() : 1,` —
                # strip-typescript would otherwise eat the ` : 1,` and
                # produce invalid JS. Bail out and leave the line alone.
                if scan >= len(line):
                    return line
                tail = line[scan]
                if tail not in ('{', '='):
                    # `=>` has its own path above (the `=` advance with
                    # `line[scan+1] == '>'` check). Any other terminator
                    # means this isn't a return type.
                    return line
                # Drop the type content (`: T`) but keep the `)`.
                result.append(')')
                # The outer loop will re-emit line[scan] in the next
                # iteration (i = scan - 1, then i += 1), so we don't
                # need to inject any whitespace here.
                i = scan - 1
                continue
        result.append(ch)
        i += 1
    return ''.join(result)


def strip_as_type(line: str) -> str:
    """Remove `value as Type` where Type may include `<>`, `[]`, `{}`, `|`.

    Examples:
        `value as Record<string, X>` -> `value`
        `value as number`            -> `value`
        `value as { a: number }`     -> `value`

    Strings (single, double, backtick quoted) and regex literals are
    protected from scanning.
    Also handles `expr satisfies T` (TS 4.9+).
    """
    pieces = []
    i = 0
    n = len(line)

    def is_identifier_end(ch: str) -> bool:
        # Identifier boundary: previous char must not be alphanumeric/underscore.
        return not (ch.isalnum() or ch == '_')

    while i < n:
        ch = line[i]
        if ch in ('"', "'", '`'):
            quote = ch
            pieces.append(ch)
            i += 1
            while i < n and line[i] != quote:
                if line[i] == '\\' and i + 1 < n:
                    pieces.append(line[i])
                    pieces.append(line[i + 1])
                    i += 2
                    continue
                pieces.append(line[i])
                i += 1
            if i < n:
                pieces.append(line[i])
                i += 1
            continue

        # Skip regex literals: `/foo/` when the `/` is not preceded by an
        # identifier-like char.
        if ch == '/' and (i == 0 or is_identifier_end(line[i - 1]) if pieces else True):
            # Check what comes after to decide if this is a regex.
            # Look for matching close `/` on the same line.
            scan = i + 1
            while scan < n and line[scan] != '/' and line[scan] != '\n':
                scan += 1
            if scan < n and line[scan] == '/':
                pieces.append(line[i:scan + 1])
                i = scan + 1
                continue

        # Look for ` as ` or ` satisfies `.
        import re as _re
        as_match = _re.match(r'\s+as\s+', line[i:])
        sat_match = _re.match(r'\s+satisfies\s+', line[i:])
        matched = as_match or sat_match
        if not matched:
            pieces.append(ch)
            i += 1
            continue

        # Verify the character before ` as ` (i.e. the char at position i-1
        # relative to the search start) is a valid expression boundary:
        # an identifier char, `)`, `]`, or `}`. We accept identifier chars
        # since `value as Type` is the common pattern.
        if i == 0:
            pieces.append(ch)
            i += 1
            continue
        prev_char = line[i - 1]
        if prev_char == ' ':
            # Walk back over whitespace to find the real previous char.
            j = i - 1
            while j >= 0 and line[j] == ' ':
                j -= 1
            if j < 0:
                pieces.append(ch)
                i += 1
                continue
            prev_char = line[j]
        valid_boundary = (prev_char.isalnum() or prev_char == '_'
                          or prev_char in ')]}')
        if not valid_boundary:
            pieces.append(ch)
            i += 1
            continue

        start = i + matched.start()
        pieces.append(line[i:start])
        scan = i + matched.end()

        # Scan the type expression.
        depth_angle = 0
        depth_paren = 0
        depth_brack = 0
        depth_brace = 0
        while scan < n:
            c = line[scan]
            if c in ('"', "'", '`'):
                quote = c
                scan += 1
                while scan < n and line[scan] != quote:
                    if line[scan] == '\\' and scan + 1 < n:
                        scan += 2
                        continue
                    scan += 1
                if scan < n:
                    scan += 1
                continue
            if c == '<':
                depth_angle += 1
            elif c == '>':
                if depth_angle > 0:
                    depth_angle -= 1
                else:
                    break
            elif c == '(':
                depth_paren += 1
            elif c == ')':
                if depth_paren > 0:
                    depth_paren -= 1
                else:
                    break
            elif c == '[':
                depth_brack += 1
            elif c == ']':
                if depth_brack > 0:
                    depth_brack -= 1
            elif c == '{':
                depth_brace += 1
            elif c == '}':
                if depth_brace > 0:
                    depth_brace -= 1
            elif c in ',;{}()' and depth_angle == 0 and depth_paren == 0 and depth_brack == 0 and depth_brace == 0:
                break
            scan += 1
        i = scan
    return ''.join(pieces)


def strip_arrow_param_types(line: str) -> str:
    """Strip type annotations from arrow function parameters.

    Handles patterns like `(a: number, b: string) =>` and `[number, number][]`.
    """
    import re

    # Find any `(...)` group that is NOT a function declaration parameter
    # list (those are handled when reading the function body). We only
    # need to deal with arrow function params here.
    # For simplicity, we process every `(...)` substring.
    def fix(match: re.Match) -> str:
        inner = match.group(1)
        return '(' + strip_param_types(inner) + ')'

    # Find balanced (...) groups.
    result = []
    i = 0
    while i < len(line):
        if line[i] == '(':
            # Find matching paren.
            depth = 1
            j = i + 1
            while j < len(line) and depth > 0:
                if line[j] == '(':
                    depth += 1
                elif line[j] == ')':
                    depth -= 1
                j += 1
            if depth == 0:
                inner = line[i + 1:j - 1]
                # Only strip if this looks like a parameter list with
                # type annotations: contains `:` not inside a string.
                if ':' in inner and not inner.lstrip().startswith('//'):
                    cleaned = strip_param_types(inner)
                    result.append('(' + cleaned + ')')
                else:
                    result.append(line[i:j])
                i = j
                continue
        result.append(line[i])
        i += 1
    return ''.join(result)


def split_params(params_str: str) -> list[str]:
    """Split params string by comma, respecting nested brackets."""
    parts = []
    depth = 0
    current = ''
    for ch in params_str:
        if ch in '<[({':
            depth += 1
            current += ch
        elif ch in '>)]':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += ch
    if current:
        parts.append(current)
    return parts


def strip_param_types(params_str: str) -> str:
    """Strip TypeScript type annotations from parameter list."""
    import re

    parts = split_params(params_str)
    clean_parts = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Handle "holes?: [number, number][][]" or "x: Type = val"
        # First try: identifier + ? + : + type + (optional default)
        # The : type can contain commas but we already split by commas (with bracket awareness)
        # So at this point, type shouldn't have commas unless it's inside <>
        # Use non-greedy match for the type so the optional default `= val` is
        # captured when present.
        m = re.match(r'^(\w+)\s*(\?)?\s*:\s*(.+?)(\s*=\s*[^=]+)?$', p)
        if m:
            ident = m.group(1)
            optional = m.group(2) or ''
            default = m.group(4) or ''
            clean_parts.append(ident + optional + default.strip())
            continue
        # Try: identifier? without type annotation
        m = re.match(r'^(\w+)\s*(\?)?\s*(\s*=\s*.+)?$', p)
        if m:
            ident = m.group(1)
            optional = m.group(2) or ''
            default = m.group(3) or ''
            clean_parts.append(ident + optional + default.strip())
            continue
        # Plain identifier
        if re.match(r'^\w+\s*$', p):
            clean_parts.append(p)
            continue
        # Otherwise, leave as is
        clean_parts.append(p)
    return ', '.join(clean_parts)


def ensure_dirs():
    """Ensure required directories exist."""
    PIC_PATH.mkdir(exist_ok=True)
    OUTPUT_PATH.mkdir(exist_ok=True)


def write_json_atomic(output_path: Path, payload: dict) -> None:
    """Publish JSON only after the complete payload has been written."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(output_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def compile_typescript_module(source_path: Path, output_path: Path) -> None:
    """Compile TypeScript to JavaScript and publish only validated output."""
    esbuild_name = 'esbuild.cmd' if os.name == 'nt' else 'esbuild'
    esbuild_path = NODE_MODULES / '.bin' / esbuild_name
    if not esbuild_path.exists():
        raise RuntimeError(
            f"esbuild not found at {esbuild_path}; run npm install in {DEMO_PATH}"
        )

    temporary_output = output_path.with_name(
        f'.{output_path.stem}.tmp{output_path.suffix}'
    )
    try:
        compiled = subprocess.run(
            [
                str(esbuild_path),
                str(source_path),
                '--format=esm',
                '--target=es2020',
                f'--outfile={temporary_output}',
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=60,
        )
        if compiled.returncode != 0:
            diagnostic = (compiled.stderr or compiled.stdout).strip()
            raise RuntimeError(f"esbuild failed:\n{diagnostic}")

        checked = subprocess.run(
            ['node', '--check', str(temporary_output)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )
        if checked.returncode != 0:
            diagnostic = (checked.stderr or checked.stdout).strip()
            raise RuntimeError(f"node --check failed:\n{diagnostic}")

        temporary_output.replace(output_path)
    finally:
        try:
            temporary_output.unlink()
        except FileNotFoundError:
            pass


def run_pipeline(
    image_path: str,
    object_name: str,
    *,
    vision_analyzer=analyze_pump_image,
    command_runner=subprocess.run,
    module_compiler=compile_typescript_module,
) -> dict:
    """Run the img2threejs forge pipeline."""
    global generation_status

    try:
        generation_status = {
            "status": "processing",
            "progress": 10,
            "message": "Analyzing image...",
            "modelPath": None,
            "error": None
        }

        # Stage 1: Probe image
        probe_cmd = [sys.executable, "-u", str(FORGE_PATH / "stage1_intake" / "probe_image.py"), image_path]
        result = command_runner(probe_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise Exception(f"Stage 1 failed: {result.stderr}")

        generation_status["progress"] = 25
        generation_status["message"] = "Recognizing pump components with Qwen..."

        visual_spec = vision_analyzer(Path(image_path))
        vision_out = OUTPUT_PATH / f"{object_name}-pump-vision.json"
        write_json_atomic(vision_out, visual_spec)

        generation_status["progress"] = 35
        generation_status["message"] = "Generating assessment..."

        # Stage 2a: Pre-spec assessment
        assessment_out = OUTPUT_PATH / f"{object_name}-assessment.json"
        assess_cmd = [
            sys.executable, "-u",
            str(FORGE_PATH / "stage2_spec" / "new_pre_spec_assessment.py"),
            object_name, "--image", image_path, "--out", str(assessment_out)
        ]
        result = command_runner(assess_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise Exception(f"Stage 2a failed: {result.stderr}")

        generation_status["progress"] = 50
        generation_status["message"] = "Creating sculpt spec..."

        # Stage 2b: Sculpt spec
        spec_out = OUTPUT_PATH / f"{object_name}-sculpt-spec.json"
        spec_cmd = [
            sys.executable, "-u",
            str(FORGE_PATH / "stage2_spec" / "new_sculpt_spec.py"),
            object_name, "--image", image_path, "--assessment", str(assessment_out), "--out", str(spec_out)
        ]
        result = command_runner(spec_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise Exception(f"Stage 2b failed: {result.stderr}")

        generation_status["progress"] = 65
        generation_status["message"] = "Applying parameterized pump structure..."

        adapt_cmd = [
            sys.executable, "-u",
            str(FORGE_PATH / "stage2_spec" / "apply_pump_visual_spec.py"),
            str(spec_out), str(vision_out), "--out", str(spec_out)
        ]
        result = command_runner(adapt_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise Exception(f"Pump spec adaptation failed: {result.stderr}")

        generation_status["progress"] = 75
        generation_status["message"] = "Generating Three.js model..."

        # Stage 3: Generate TypeScript and compile it to browser-ready JavaScript.
        model_out = SRC_PATH / f"create{object_name}Model.js"
        typescript_out = SRC_PATH / f".create{object_name}Model.ts"
        gen_cmd = [
            sys.executable, "-u",
            str(FORGE_PATH / "stage3_build" / "generate_threejs_factory.py"),
            str(spec_out), "--out", str(typescript_out)
        ]
        try:
            result = command_runner(gen_cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise Exception(f"Stage 3 failed: {result.stderr}")

            module_compiler(typescript_out, model_out)
            print(f"[DEBUG] Compiled TypeScript to {model_out.name}", flush=True)
        finally:
            try:
                typescript_out.unlink()
            except FileNotFoundError:
                pass

        generation_status = {
            "status": "complete",
            "progress": 100,
            "message": "Model generated successfully!",
            "modelPath": f"/src/create{object_name}Model.js",
            "modelName": object_name,
            "error": None
        }
        return generation_status

    except subprocess.TimeoutExpired:
        generation_status = {"status": "error", "progress": 0, "message": "Generation timed out", "modelPath": None, "error": "Pipeline timeout"}
        raise Exception("Generation timed out")

    except Exception as e:
        generation_status = {"status": "error", "progress": 0, "message": str(e), "modelPath": None, "error": str(e)}
        raise


class PipelineRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler for API, ES modules, and static files."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DEMO_PATH), **kwargs)

    def do_POST(self):
        """Handle POST requests for API endpoints."""
        if self.path.startswith('/api/'):
            self.handle_api_post()
        else:
            self.send_error(404, "Not Found")

    def handle_api_post(self):
        """Handle POST requests to /api/generate"""
        if self.path == '/api/generate':
            try:
                content_type = self.headers.get('Content-Type', '')
                if 'multipart/form-data' not in content_type:
                    self.send_error(400, "Expected multipart/form-data")
                    return

                content_length = int(self.headers.get('Content-Length', 0))
                if content_length == 0:
                    self.send_error(400, "Empty request body")
                    return

                form_data = self.rfile.read(content_length)

                boundary = None
                for part in content_type.split(';'):
                    if 'boundary' in part:
                        boundary = part.split('=')[1].strip()
                        break

                if not boundary:
                    self.send_error(400, "Missing boundary")
                    return

                boundary_bytes = boundary.encode()
                parts = form_data.split(b'--' + boundary_bytes)
                image_found = False

                for part in parts:
                    if b'Content-Type: image' in part:
                        header_end = part.find(b'\r\n\r\n')
                        if header_end == -1:
                            continue
                        header_end += 4
                        image_data = part[header_end:]

                        # Remove trailing boundary markers
                        image_data = image_data.rstrip(b'\r\n--')

                        timestamp = int(time.time())
                        image_path = PIC_PATH / f"input_{timestamp}.png"
                        with open(image_path, 'wb') as f:
                            f.write(image_data)

                        obj_name = f"Object_{timestamp}"
                        threading.Thread(target=run_pipeline, args=(str(image_path), obj_name), daemon=True).start()
                        self.send_json({"status": "started", "objectName": obj_name})
                        image_found = True
                        break

                if not image_found:
                    self.send_error(400, "No image found in request")

            except ValueError as e:
                self.send_json({"status": "error", "message": f"Invalid request: {str(e)}"}, 400)
            except Exception as e:
                print(f"API error: {e}")
                self.send_json({"status": "error", "message": str(e)}, 500)
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/api/status':
            self.send_json(generation_status)
        elif self.path.startswith('/node_modules/'):
            self.serve_node_module(self.path)
        else:
            self.serve_static()

    def serve_static(self):
        """Serve static files."""
        path = self.translate_path(self.path)
        if path.is_dir():
            path = path / 'index.html'
        if not path.exists():
            self.send_error(404, "File not found")
            return
        self.send_file(path)

    def serve_node_module(self, url_path):
        """Serve files from node_modules, resolving bare imports."""
        # Remove /node_modules/ prefix
        module_path = url_path[len('/node_modules/'):]

        # Only resolve package.json for top-level package requests (e.g., /three)
        parts = module_path.split('/')
        pkg_name = parts[0]
        pkg_path = NODE_MODULES / pkg_name

        # If requesting the package root, resolve via package.json
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == ''):
            if pkg_path.exists():
                pkg_json = pkg_path / 'package.json'
                if pkg_json.exists():
                    with open(pkg_json, 'r', encoding='utf-8') as f:
                        pkg = json.load(f)
                    if 'module' in pkg:
                        module_path = f"{pkg_name}/{pkg['module']}"

        full_path = NODE_MODULES / module_path

        # Try common variations
        if not full_path.exists():
            for suffix in ['.js', '/index.js']:
                if full_path.with_suffix(suffix).exists():
                    full_path = full_path.with_suffix(suffix)
                    break

        if full_path.exists() and full_path.is_file():
            self.send_file(full_path)
        else:
            self.send_error(404, f"Module not found: {module_path}")

    def proxy_to_vite(self, path):
        """Proxy request to Vite dev server."""
        try:
            url = f"http://localhost:{VITE_PORT}{path}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                content = resp.read()
                self.send_response(resp.status)
                for header, value in resp.headers.items():
                    if header.lower() not in ['transfer-encoding', 'connection', 'content-encoding']:
                        self.send_header(header, value)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            self.send_error(502, f"Vite proxy error: {e}")

    def send_file(self, filepath):
        """Send a file with correct MIME type."""
        filepath = Path(filepath)
        if not filepath.exists():
            self.send_error(404, "File not found")
            return

        ext = filepath.suffix.lower()
        mime_type = MIME_TYPES.get(ext)
        if not mime_type:
            guess = mimetypes.guess_type(str(filepath))[0]
            mime_type = guess if guess else 'application/octet-stream'

        with open(filepath, 'rb') as f:
            content = f.read()

        self.send_response(200)
        self.send_header('Content-Type', mime_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Access-Control-Allow-Origin', '*')
        # Generated model files are regenerated on every upload, so force the
        # browser to revalidate them instead of holding a stale ESM module
        # in its import map (which can throw "Unexpected token" errors when
        # the previous file has been deleted).
        # /src/main.js, index.html and the app's own JS/CSS also need to be
        # fresh on every reload while the server is being actively edited.
        no_cache_paths = (
            filepath.name.startswith('createObject_') and filepath.suffix.lower() == '.js'
        ) or filepath.name in ('main.js', 'index.html', 'style.css')
        if no_cache_paths:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        else:
            self.send_header('Cache-Control', 'max-age=3600')
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, data, status=200):
        """Send JSON response."""
        response = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(response))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)

    def translate_path(self, url_path):
        """Translate URL path to filesystem path."""
        # Remove query string
        url_path = url_path.split('?')[0].split('#')[0]
        return Path(DEMO_PATH) / url_path.lstrip('/')

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")


def main():
    """Start the server."""
    print("[DEBUG] Running ensure_dirs()...", flush=True)
    ensure_dirs()
    print("[DEBUG] Changing directory...", flush=True)
    os.chdir(DEMO_PATH)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║          img2threejs Demo Server                          ║
╠══════════════════════════════════════════════════════════╣
║  URL:         http://127.0.0.1:{PORT}                      ║
║  API:         http://127.0.0.1:{PORT}/api/                  ║
║                                                          ║
║  Endpoints:                                               ║
║    POST /api/generate  - Upload image, generate model     ║
║    GET  /api/status    - Check generation status         ║
║                                                          ║
║  Upload image via the UI button, then wait for model.   ║
╚══════════════════════════════════════════════════════════╝
""", flush=True)

    print("[DEBUG] Starting HTTP server...", flush=True)
    print(f"[DEBUG] Binding to 127.0.0.1:{PORT}", flush=True)
    with HTTPServer(("127.0.0.1", PORT), PipelineRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


if __name__ == "__main__":
    main()
