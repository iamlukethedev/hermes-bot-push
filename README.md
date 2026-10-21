# hermes-bot-push

A [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) plugin that sends a push notification to the
[Herald](https://heraldapp.dev) app when one of your bots finishes a turn or is waiting for your approval, even
while your phone is asleep.

Herald talks straight to the Hermes Agent on your own computer, and your phone puts that connection to sleep soon
after you leave the app. This plugin runs inside Hermes and closes the gap: when a turn finishes or a tool call
needs your approval, it hands a short banner to Expo's push service, which delivers it through Apple (APNs) or
Google (FCM). It only observes. It never changes, delays or blocks a turn, and a push that cannot go out is
dropped quietly.

## Requirements

- [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started/installation) with the
  `hermes plugins` command, serving Herald with `hermes serve` (or `hermes dashboard`). If `hermes plugins install`
  is missing, run `hermes update`.
- [Herald](https://heraldapp.dev) on a real iPhone, iPad or Android phone. Simulators and emulators cannot receive
  push.
- Herald paired with this computer as its primary connection, because push comes from the primary connection.
  [`npx herald-setup`](https://heraldapp.dev/setup) pairs it.
- Outbound HTTPS from this computer to `exp.host`. No account, API key or other secret is needed.

## Install

On the computer that runs `hermes serve`:

```bash
hermes plugins install iamlukethedev/hermes-bot-push --enable
```

Hermes warns that this is a custom source rather than one from its plugin catalog; that is expected. Then restart
`hermes serve`, because Herald's chats run there: `hermes serve --stop`, then start it the way you normally do
(`npx herald-setup`, `hermes serve --port 9119` or your service manager). Hermes may also suggest
`hermes gateway restart`; that restarts its messaging gateway (Telegram and the like), which only matters once you
widen `platforms` below. `hermes plugins list` should show `hermes-bot-push` as enabled.

Then, in Herald, open **Settings › General › Push notifications**, tap **Register** next to this phone and
**Send a test push**. The test proves the phone side; after that, the next finished turn or approval request
arrives as a banner.

**Hermes in Docker:** run the same command inside the container, then restart it:

```bash
docker compose exec dashboard hermes plugins install iamlukethedev/hermes-bot-push --enable
docker compose restart dashboard
```

## What is sent

Only who finished or who needs approval, never what was said. A banner reads like "Fixer finished",
"Fixer needs approval" or "Fixer replied in <room>", where "Fixer" is the bot's name as Herald shows it (its
Hermes profile name when Herald has not registered one).
Alongside the banner travel the few fields Herald needs to open the right chat when you tap it: the Hermes profile
name, the session id, the group room id for group chats and a timestamp. Reply text, prompts and tool output never
leave your computer.

Messages go from your computer to Expo's push service (`https://exp.host`) and from there to Apple or Google.
The push tokens come from Herald: tapping **Register** writes this phone's Expo push token and name into your
default Hermes profile (`profile.yaml`, under `ui_meta.hermes-bot-push`), and **Remove** deletes them. The plugin
reads them from there; it stores nothing itself.

## Configuration

By default the plugin pushes only for chats Herald manages (each bot's chat and Herald's group rooms), not for your
terminal, Telegram or other sessions. To change that, add this to `~/.hermes/config.yaml` and restart
`hermes serve`:

```yaml
plugins:
  entries:
    hermes-bot-push:
      settings:
        platforms: [tui] # turns from these platforms push; [] pushes for every platform
        all_sessions: false # true also pushes for sessions Herald did not create
```

Set the `HERMES_BOT_PUSH_DISABLED` environment variable to any value to keep the plugin loaded but silent.

## Update or remove

```bash
hermes plugins update hermes-bot-push
hermes plugins remove hermes-bot-push
```

Restart `hermes serve` after either.

## Troubleshooting

- **Send a test push does not arrive:** the phone side is the problem. Check notifications are allowed for Herald
  and that you tapped Register on a real device.
- **The test arrives, real banners do not:** check `hermes plugins list` shows the plugin as enabled, that you
  restarted `hermes serve`, that `HERMES_BOT_PUSH_DISABLED` is not set, and that the bot runs on Herald's primary
  connection.
- More help: [Push notifications never arrive](https://heraldapp.dev/help/push-notifications) and
  [Herald support](https://heraldapp.dev/support).

## How it works

The plugin registers two Hermes hooks, `post_llm_call` (a turn finished) and `pre_approval_request` (a person is
being asked to approve a tool call; the automatic "smart" approval pass is skipped). For each event it checks the
session belongs to Herald, reads the registered push tokens from the default profile and posts one message per
phone to Expo on a background thread with a five-second timeout. Everything is plain Python standard library plus
the YAML parser Hermes already ships: PyYAML on Hermes 0.21 and earlier, ruamel.yaml from 0.22.

## License

[MIT](LICENSE). Made by lukethedev ([@iamlukethedev](https://x.com/iamlukethedev)) for
[Herald](https://heraldapp.dev). Herald is an independent app and is not made by, endorsed by or affiliated with
Nous Research.
