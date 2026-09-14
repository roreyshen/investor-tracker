# Mac texting agent

Sends real messages to your phone using **Messages.app**, driven by AppleScript.
Free, no Twilio, no carrier gateway.

## Why it works this way

`408-748-6507` is on Xfinity Mobile (a Verizon MVNO). Its email-to-SMS gateway
is widely reported to bounce, and Verizon retires that gateway on 2027-03-31.
Twilio costs money and needs A2P 10DLC registration. But a Mac signed into
iMessage can send to your phone directly, for free.

**The split:** GitHub Actions runs 24/7 and detects trades, writing them to
`state/outbox.json`. This agent runs on your Mac, pulls the repo, and texts
whatever is unsent. Your Mac being asleep is expected — alerts queue in the
repo and go out on the next wake, so nothing is lost.

The agent only ever *reads* the repo. Sent-state lives in
`~/.investor-tracker-sent.json` and is never committed, so it can't collide
with the scheduled runs.

## Install

```bash
bash mac_agent/install.sh
python3 mac_agent/agent.py --test
```

**macOS will ask permission to control Messages — click Allow.** It asks once.
If the test hangs for ~25 seconds and reports a timeout, that dialog is waiting
for you (possibly behind another window).

If you ever need to grant it manually:
System Settings → Privacy & Security → Automation → Terminal → enable **Messages**.

## Everyday use

```bash
python3 mac_agent/agent.py            # send whatever is pending
python3 mac_agent/agent.py --dry-run  # show what would send
tail -f mac_agent/agent.log           # watch it work
bash mac_agent/install.sh remove      # uninstall the schedule
```

## Behavior worth knowing

- Runs every **5 minutes** via launchd, and once immediately on login.
- At most **8 individual texts** per run; the rest collapse into one
  "+N more" message. A busy filing day shouldn't mean 40 buzzes.
- Alerts that waited more than **12 hours** (you were asleep) are summarized
  into a single digest instead of arriving one by one.
- On a send failure it stops and leaves the queue intact, so the next run
  retries rather than dropping alerts.

## Sending somewhere else

```bash
echo '{"recipient": "+15551234567"}' > ~/.investor-tracker-agent.json
```

## The tradeoff

This only sends while your Mac is awake and online. Discord is still the
always-on channel and gets everything immediately. If you later want texts
that don't depend on the Mac, the Twilio sender is already written — see the
main README.
