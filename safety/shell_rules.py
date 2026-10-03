"""Static analysis of PowerShell commands before they are shown to the user.

The confirmation must show exactly what runs, so anything that hides what runs is refused outright:
  * -EncodedCommand / -enc (and abbreviations) and base64 payloads (FromBase64String, certutil -decode, long blobs)
  * Invoke-Expression / iex and [scriptblock]::Create (they execute strings we cannot show)
  * elevation tricks inside the command (-Verb RunAs, runas): admin is its own separate confirmation
Catastrophic commands (format, diskpart, bcdedit, deleting shadow copies, cipher /w, recursive delete of a drive root
or the user profile, disabling Defender or the firewall) stay allowed but need the user to type CONFIRM.
This is a safety net on top of the user reading the command, not a sandbox: it is easy to evade on purpose, which is why
the user always sees the exact text.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ShellAnalysis:
    refused: str | None = None
    catastrophic: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _norm(command: str) -> str:
    """Lowercase, backticks (PowerShell escapes/obfuscation) removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", command.replace("`", "")).strip().lower()


_REFUSE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?<![\w-])-encodedc\w*"), "it uses -EncodedCommand, which hides what actually runs"),
    (re.compile(r"\bfrombase64(?:string|transform)\b|\bcertutil\b[^;|]*-decode|\[convert\]::frombase64"),
     "it decodes base64 data, so the real command isn't visible"),
    (re.compile(r"[a-z0-9+/]{80,}={0,2}"), "it contains a long encoded blob I can't show in a readable form"),
    (re.compile(r"\b(?:iex|invoke-expression)\b|\[scriptblock\]::create"),
     "it runs text as code (iex / Invoke-Expression), which can't be shown in advance"),
    (re.compile(r"-verb\s+runas|\brunas(?:\.exe)?\b"), "it tries to elevate itself; ask for administrator rights with the admin option instead"),
]
# abbreviations (-e, -ec, -en, -enc ...) only count when followed by a blob, so `Out-File -Encoding utf8` is fine
_PS_ENC_ABBREV = re.compile(r"(?<![\w-])-(?:e|ec|en|enc|enco|encod|encode|encoded)\s+['\"]?[a-z0-9+/=]{12,}")

_CATASTROPHIC: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bformat(?:\.com)?\s+[a-z]:|\bformat-volume\b"), "formats a disk"),
    (re.compile(r"\b(?:clear-disk|initialize-disk|remove-partition)\b"), "wipes or repartitions a disk"),
    (re.compile(r"\bdiskpart\b"), "diskpart can erase disks"),
    (re.compile(r"\bbcdedit\b"), "changes the boot configuration"),
    (re.compile(r"\bvssadmin(?:\.exe)?\b[^;|]*\bdelete\b|\bdelete\s+shadows\b|\bwin32_shadowcopy\b[^;|]*\b(?:delete|remove)"),
     "deletes shadow copies (restore points / backups)"),
    (re.compile(r"\bwmic\b[^;|]*\bshadowcopy\b[^;|]*\bdelete\b"), "deletes shadow copies (restore points / backups)"),
    (re.compile(r"\bcipher(?:\.exe)?\s+/w"), "overwrites free disk space (cipher /w)"),
    (re.compile(r"\bset-mppreference\b[^;|]*-disable\w+\s+(?:\$true|1|true)|disableantispyware|disablerealtimemonitoring"),
     "disables Windows Defender"),
    (re.compile(r"\b(?:set|add)-mppreference\b[^;|]*-exclusion\w*"), "weakens Windows Defender with an exclusion"),
    (re.compile(r"\bstop-service\b[^;|]*(?:windefend|wdnissvc|sense)|\bsc(?:\.exe)?\s+(?:stop|config|delete)\s+windefend"),
     "stops Windows Defender"),
    (re.compile(r"uninstall-windowsfeature[^;|]*defender"), "removes Windows Defender"),
    (re.compile(r"\bset-netfirewallprofile\b[^;|]*-enabled\s+(?:false|0)|\bnetsh\s+advfirewall\s+set\s+\w+\s+state\s+off|"
                r"\bnetsh\s+firewall\s+set\s+opmode\s+disable"), "turns off the Windows firewall"),
    (re.compile(r"\bstop-service\b[^;|]*mpssvc|\bsc(?:\.exe)?\s+(?:stop|config|delete)\s+mpssvc"),
     "stops the Windows firewall service"),
]

_WARN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"c:\\windows|\\system32|program files|\\\.ssh|\\appdata\\local\\(?:google|microsoft\\edge|bravesoftware)"),
     "touches a protected location"),
    (re.compile(r"\bhk(?:lm|cu|cr|u|cc):|\bhkey_|\breg(?:\.exe)?\s+(?:add|delete|import|load|unload)\b"), "changes the registry"),
    (re.compile(r"\bset-executionpolicy\b"), "changes the PowerShell execution policy"),
    (re.compile(r"\b(?:invoke-webrequest|iwr|invoke-restmethod|irm|curl|wget|start-bitstransfer)\b|\.downloadfile|\.downloadstring"),
     "downloads from the internet"),
    (re.compile(r"\b(?:stop-computer|restart-computer)\b|\bshutdown(?:\.exe)?\s+/"), "shuts down or restarts the PC"),
    (re.compile(r"\b(?:net\s+user|net\s+localgroup|new-localuser|schtasks(?:\.exe)?\s+/create|register-scheduledtask|new-service|set-service)\b"),
     "changes accounts, services or scheduled tasks"),
    (re.compile(r"\b(?:stop-process|taskkill)\b"), "stops running programs"),
]

_DELETE_VERB = re.compile(r"(?:^|[;&|\n(]|\bcmd(?:\.exe)?\s+/c\s+)\s*(remove-item|ri|rm|del|erase|rmdir|rd)\b([^;&|\n]*)")
_RECURSIVE = re.compile(r"(?<!\S)(?:-r\w*|-recurse|/s)(?!\S)")
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")


def _expand(token: str) -> str:
    t = token.strip("\"'")
    home = str(Path.home())
    t = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), t, flags=re.I)
    t = re.sub(r"%(\w+)%", lambda m: os.environ.get(m.group(1), m.group(0)), t)
    t = re.sub(r"\$home\b", lambda m: home, t, flags=re.I)
    if t == "~" or t.startswith(("~\\", "~/")):
        t = home + t[1:]
    t = t.replace("/", "\\")
    t = re.sub(r"\\\*(?:\.\*)?$", "", t)                     # C:\Users\me\*  ->  C:\Users\me
    return os.path.normcase(os.path.normpath(t)) if t else t


def _dangerous_targets() -> set[str]:
    drive = os.environ.get("SystemDrive", "C:")
    targets = {os.path.normcase(str(Path.home())), os.path.normcase(drive + "\\users"),
               os.path.normcase(drive + "\\windows"), os.path.normcase(drive + "\\program files"),
               os.path.normcase(drive + "\\program files (x86)")}
    for env_name in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        if v := os.environ.get(env_name):
            targets.add(os.path.normcase(os.path.normpath(v)))
    return targets


def _recursive_delete_hits(norm: str) -> list[str]:
    hits: list[str] = []
    danger = _dangerous_targets()
    for m in _DELETE_VERB.finditer(norm):
        args = m.group(2)
        if not _RECURSIVE.search(args):
            continue
        for tok in _TOKEN.findall(args):
            if tok.startswith(("-", "/")) and not re.match(r"^/[a-z]:", tok):
                continue
            path = _expand(tok)
            if re.fullmatch(r"[a-z]:\\?", path) or path in danger:
                hits.append(f"deletes everything under {tok.strip(chr(34) + chr(39))} recursively")
    return hits


def analyze(command: str) -> ShellAnalysis:
    """Inspect a PowerShell command string (it is never executed here)."""
    a = ShellAnalysis()
    if not command.strip():
        a.refused = "the command is empty"
        return a
    norm = _norm(command)
    for rx, why in _REFUSE:
        if rx.search(norm):
            a.refused = f"I won't run this: {why}."
            return a
    if re.search(r"\b(?:powershell|pwsh)(?:\.exe)?\b", norm) and _PS_ENC_ABBREV.search(norm):
        a.refused = "I won't run this: it uses -EncodedCommand, which hides what actually runs."
        return a
    for rx, why in _CATASTROPHIC:
        if rx.search(norm) and why not in a.catastrophic:
            a.catastrophic.append(why)
    a.catastrophic += [h for h in _recursive_delete_hits(norm) if h not in a.catastrophic]
    for rx, why in _WARN:
        if rx.search(norm):
            a.warnings.append(why)
    return a
