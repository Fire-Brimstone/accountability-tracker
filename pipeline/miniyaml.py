"""Minimal YAML reader for the block-style files in openstates/people.

Supports: nested mappings, sequences ("- "), sequences of mappings, plain/quoted scalars,
comments, and `key: [a, b]` flow lists. That is all the openstates people files use.
Not a general YAML parser — use PyYAML if you need one.
"""
from __future__ import annotations

import json
import re


def _scalar(s: str):
    s = s.strip()
    if not s or s == "~" or s == "null":
        return None
    if s[0] in "'\"" and s[-1] == s[0] and len(s) >= 2:
        if s[0] == "'":
            return s[1:-1].replace("''", "'")
        try:
            return json.loads(s)
        except ValueError:  # YAML escapes JSON doesn't know, e.g. "\."
            body = s[1:-1]
            body = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), body)
            body = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), body)
            return re.sub(r"\\(.)", r"\1", body)
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(x) for x in s[1:-1].split(",") if x.strip()]
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _strip_comment(line: str) -> str:
    # drop " # comment" outside quotes
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1] == " "):
            break
        out.append(ch)
    return "".join(out).rstrip()


def loads(text: str):
    lines = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip() and line.strip() != "---":
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    value, _ = _block(lines, 0, lines[0][0] if lines else 0)
    return value


def _split_kv(s: str):
    m = re.match(r"^((?:\"[^\"]*\"|'[^']*'|[^:])+?):(?:\s+(.*))?$", s)
    if not m:
        return None
    return _scalar(m.group(1)), (m.group(2) or "")


def _block(lines, i, indent):
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        out = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest = lines[i][1][2:].strip() if lines[i][1] != "-" else ""
            if not rest:
                val, i = _block(lines, i + 1, lines[i + 1][0] if i + 1 < len(lines) else indent + 2)
                out.append(val)
                continue
            kv = _split_kv(rest)
            if kv and not rest.startswith(("'", '"')):
                # a mapping starting on the dash line; its other keys are indented by indent+2
                lines[i] = (indent + 2, rest)
                val, i = _mapping(lines, i, indent + 2)
                out.append(val)
            else:
                out.append(_scalar(rest))
                i += 1
        return out, i
    return _mapping(lines, i, indent)


def _mapping(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        kv = _split_kv(lines[i][1])
        if not kv:
            raise ValueError(f"cannot parse line: {lines[i][1]!r}")
        key, rest = kv
        i += 1
        if rest in ("|", ">", "|-", ">-"):
            buf = []
            while i < len(lines) and lines[i][0] > indent:
                buf.append(lines[i][1])
                i += 1
            out[key] = ("\n" if rest.startswith("|") else " ").join(buf)
        elif rest:
            out[key] = _scalar(rest)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
            out[key], i = _block(lines, i, lines[i][0])
        else:
            out[key] = None
    return out, i
