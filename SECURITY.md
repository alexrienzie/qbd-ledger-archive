# Security and confidentiality

## Your data

An archive built with this tool holds a company's complete books. Treat everything it writes as confidential: `raw/`, `requests/`, `manifest.json`, the `.sqlite` file, `validation/`, and the bridge's `io\` folder on the QuickBooks machine.

- Keep archives outside any folder you share or sync to a public place. The included `.gitignore` covers the default locations, but check before committing anything from a clone you've worked in.
- **Never attach raw responses, databases, validation files or logs to a GitHub issue.** Describe the problem, and if a sample helps, build one from made-up data.
- The tool only reads from QuickBooks and sends no data anywhere except through the transport you configure.

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" link on this repository's Security tab, not in a public issue.
