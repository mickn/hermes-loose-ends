# Loose ends for Hermes

Everything your Hermes bots are still waiting on you for, in one list.

Bots ask a lot of small things: *which day works*, *approve this send*, *click the verification link*. Spread over a dozen chats, some slip. Twice a day a sweep reads what every bot said since the last check and lists what's still open. For each item you can:

- **Dealt with**: close it.
- **Remind me tomorrow**: snooze it until 8 AM.
- **Next steps…**: hand it back to the bot that raised it, with your instructions. The bot does the work and its reply closes the item.

It shows as a **Loose ends** tab in the Hermes dashboard and in the Hermes Native app on iPhone, iPad, Mac and Apple Watch.

## Install

From Hermes Native: open Activity and tap **Add to My Hermes**. It installs the plugin, sets up the sweep in the bot you choose and runs a first check.

By hand:

```bash
hermes plugins install https://github.com/mickn/hermes-loose-ends
```

Then create the sweep for the bot that should run it (say `main`). Ask the dashboard for the job (`POST /api/plugins/loose-ends/setup {"profile": "main"}` writes the sweep's pre-run script into that profile and answers with the job) and create it under Scheduled tasks, or let Hermes Native do both.

## How it works

- `loose_ends.py` (standard library only) keeps the list in `$HERMES_HOME/loose-ends/items.json`.
- The sweep is a Hermes scheduled task. Its pre-run script builds a digest of every profile's recent conversations and delivered scheduled-task output (`loose_ends.py prepare`); the bot proposes new, resolved and updated items, and `loose_ends.py apply` merges them.
- Hand-offs run one turn with the owning bot through the Hermes CLI (`hermes -p <bot> chat -Q --oneshot`).
- Times use the host's time zone. Nothing leaves your host: the digest, the list and the hand-offs stay on it.

The dashboard routes (`/api/plugins/loose-ends/items`, `/decide`, `/setup`) sit behind the dashboard's normal sign-in. The plugin registers no tools, hooks or middleware.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
