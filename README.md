# Happening Intelligence

Open-source software and scripts from Happening Intelligence, the team behind HappeningIQ.

## First project: AI-assisted observability

This Python prototype connects observability to the backlog. It reads unresolved
Sentry errors in a chosen environment, samples a matching event for each issue,
and proposes a title, investigation summary, and priority. It can create Azure
Boards work items and post a Slack summary with links to both systems.

OpenRouter can suggest a priority from 1 (highest) to 4 (lowest) and a next
investigation step. Each issue has one sample, so the suggestion is a starting
point for review. The script writes it as guidance in the ticket.

## Quick start

Use Python 3.11 or later. No third-party Python packages are required. From the
repository root, copy `.env.local.example` to `.env.local` and fill in the Sentry
organization, project slug or ID, and an integration token with `event:read`.
Use the exact Sentry environment label when running the command.

```sh
python triage.py --environment dev
python triage.py --environment dev --slack
python triage.py --environment dev --apply
python triage.py --environment dev --apply --slack
```

- With neither flag, results appear in the terminal. The default scan considers
  up to 20 issues from the last seven days. Adjust it with `--limit` and `--days`.
- `--apply` requires the Azure DevOps settings, a PAT with Work Items Read & Write
  access, and valid project area and backlog iteration paths. Choose a work-item
  type visible on the selected team's backlog.
- `--slack` posts to the channel associated with `SLACK_WEBHOOK_URL`.
- Set `OPENROUTER_API_KEY` for AI enrichment, including during preview. Without
  it, output uses a local evidence summary and a neutral priority of 3.
  `OPENROUTER_MODEL` can override the default model.

## Data handling

Keep local configuration, credentials, SQLite files, and operational logs out
of Git. The example configuration contains no real account values. The source
connector selects exception type, HTTP status, timestamp, and up to six
application frames. It excludes raw messages, request bodies, full paths,
user data, and frame variables. Azure Boards can receive the selected frames.
OpenRouter receives the exception type and HTTP status by default. Set
`AI_INCLUDE_STACK=true` to include the selected stack symbols in model requests.
Review what your destination services may receive before enabling them.

SQLite records completed writes and prevents simultaneous runs on the same
database from submitting the same issue twice. Each work item also contains a
source reference for Azure Boards lookup. Keep the ledger between runs so
deleting an item does not cause it to be recreated. If a create response is
uncertain, later runs look up the item by its source reference. Use one shared
ledger for scheduled and manual runs against the same destination.

## Contributing

Run the offline tests with `python -m unittest -v`. Use synthetic events and
fake credentials in tests, issues, and pull requests.

The CLI coordinates the workflow and selects the built-in connectors. Each
connector lives in its own module under [connectors](connectors/).

| Role | First connector | Boundary |
| --- | --- | --- |
| Observability | Sentry | List issue IDs, sample and minimize evidence, and return a stable identity and source link |
| AI enrichment | OpenRouter | Turn minimized evidence into an assessment with a title, summary, and suggested priority |
| Work tracking | Azure Boards | Find by source reference, create a work item, and return its link |
| Messaging | Slack | Format and deliver the run summary |

To propose a new connector, open an issue describing the service and workflow.
An implementation should follow the existing role's interface and include
offline tests. This prototype wires connectors explicitly in the CLI.

## License

[Apache License 2.0](LICENSE)
