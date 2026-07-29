# Security Policy

## Supported Versions

This project is pre-1.0. Security fixes are applied to the latest release
only; there is no long-term-support branch yet.

## Reporting a Vulnerability

Please report security issues privately rather than opening a public GitHub
issue. Email **hello@heyhi.design** with:

- A description of the issue and its potential impact
- Steps to reproduce (a minimal example is ideal)
- Any suggested fix, if you have one

You should receive an acknowledgment within 5 business days. We'll work with
you to understand and confirm the issue, agree on a disclosure timeline, and
credit you in the fix (unless you'd rather stay anonymous).

## Scope

This repository is a schema, a CLI, and documentation. It has no network
calls, no server component, and no runtime that processes untrusted input
beyond the YAML files and command-line flags you point it at directly. The
realistic risk surface is:

- Malformed or adversarial YAML in a registry file (the CLI uses
  `yaml.safe_load`, which does not execute arbitrary Python objects — if you
  find a path where that protection doesn't hold, that's exactly the kind of
  report this policy wants)
- Anything in `src/` that could be tricked into writing outside the intended
  registry file

If you're reporting a vulnerability in a tool this project integrates with
(a specific agent runtime, a specific provider's API), please report it to
that project directly — we're happy to be looped in, but we aren't the right
first responder for someone else's surface.
