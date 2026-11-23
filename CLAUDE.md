# Instructions for Claude in this repo

## How to work with me
- I'm learning Python and AI engineering. Comment code line by line: what each line does and why.
- Correct my technical terms and definitions whenever I use one loosely.
- Explain what a command does before running it or asking me to approve it.
- I work in short gaps. Don't leave values for me to fill in (versions, paths, names). If you need
  information, tell me how to find it or ask, then build it into the code.
- Make changes on a new branch, not directly on `main`, so I can review before merging.
- Never commit secrets. API keys go in `.env` (ignored by git); list the key names in `.env.example`.

## This project
- A job-board finder for nonprofit and philanthropy roles in impact, data and evaluation.
- This repo is PUBLIC. Never put personal details in code, comments, commit messages or PRs:
  no names of people, no employers, no locations tied to anyone. Anything personal (like
  organizations to leave out) goes in a GitHub secret, never in the repo.
- Standard library only; must stay compatible with Python 3.8. The tests run on 3.12 in Actions.
- Runs on GitHub Actions, because the IRS and other data sites aren't reachable from Claude's
  network. Builds push their output to `results/...` side branches, never straight to `main`.
- Organizations to leave out come from the `EXCLUDE_NAMES` secret (semicolon-separated).
- Tests use sample data only and never touch the internet:
  `python3 -m unittest discover -s tests -v`
