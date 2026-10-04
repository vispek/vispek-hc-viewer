# Security policy

## Reporting

Report vulnerabilities privately through GitHub's "Report a vulnerability" button on
this repository, or by e-mail to <contact@vispek.cn>. Do not open a public issue.

**UV safety defects are security issues.** Anything that can light LEDs 1-4 without
`--allow-uv`, or leave an LED lit, goes through the same private channel. So does
anything that lets a page or program other than the viewer's own page drive the device
([docs/security.md](docs/security.md) says what the server defends against).

## Supported versions

The latest 0.x minor release.
