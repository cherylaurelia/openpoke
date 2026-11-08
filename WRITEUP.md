# Write-up

OpenPoke has one assistant you text ("Poke") that hands work to persistent worker agents ("Email
to Alice", "Rent reminder"). This fork changes how Poke picks between hundreds of those agents,
adds an Activity panel that shows what was done, and adds "Waiting on", where Poke notices that
someone hasn't replied to you and offers to nudge them.

## The short version

- Part 1, agent overload. The agent's name was doing three jobs: the ID the model had to retype
  exactly, the only description of the agent, and its log filename.
  - Names are now matched after normalising. Poke sees the 20 most recently used agents and
    searches for the rest. An unknown name creates nothing, and when two agents fit equally, the
    code refuses to send and Poke asks the user.
  - On the same day and model, 52 of 52 live routing runs passed, against 42 of 52 for the
    original. All 10 of the original's failures were ambiguous requests: it messaged every
    matching agent (8) or picked one (2) instead of asking. Counting "message every match" as a
    pass, the original scores 50/52.
- Part 2, the Activity panel. It lists what needs your approval, who you're waiting on, upcoming
  reminders, and recent tasks. Each line is built from records the code writes as it runs.
- Part 3, "Waiting on". If someone hasn't replied to your email in 2 days, Poke drafts one nudge
  and asks you before sending it. Sending goes through an approval step that I moved from the
  prompt into code.
- Testing: 69 unit tests, each fix broken on purpose to confirm a test catches it (65 checks),
  live evals against the original, real Gmail, and the app checked in a browser.

## Part 1: Execution agent overload

### What was wrong

Poke routes with one tool, `send_message_to_agent(agent_name, instructions)`, in
`server/agents/interaction_agent/tools.py`.

1. An unknown name created a new agent. The tool did an exact string match and made a fresh agent,
   with no history, whenever it missed. "email to alice" and "Email to Alice" became two agents,
   and because log filenames are lowercased, both wrote to the same log file.
2. The name was the only description. The worker's prompt was `"Handle tasks related to: {name}"`.
   With "Email to Alice Chen" and "Email to Alice Wong" both listed, Poke had to guess which one
   "did alice reply?" meant.
3. Every agent was in every prompt. At 500 agents the list alone is about 24,000 characters (about
   6,000 tokens) on every turn.

### What I changed

- Agents are looked up by a normalised name, using the function that already names log files, so
  the roster and the logs agree. Old rosters with case duplicates merge into one on load.
  - I kept names instead of adding numeric IDs: there's no data migration and the logs stay
    readable. A mistyped name returns an error, while a mistyped ID could match a different real
    agent.
- Each agent has a description and a last-used time. Creating an agent requires a description;
  older agents take theirs from their first request.
- Poke's prompt lists the 20 most recently used agents and a count of the hidden ones, so it stops
  growing with the roster. Reminder runs count as use, so an agent that just reported back is
  always listed.
- A `find_agents` tool searches the rest and weights rare words higher: "email" is in hundreds of
  agents and barely counts, "lisbon" is in one and counts a lot. It's the main idea of BM25, in
  about 10 lines of standard library.
  - I chose keyword search over embeddings because Poke writes the query itself, so the model
    already handles meaning ("her" becomes "alice"). Keyword search costs nothing, gives the same
    answer every time, and doesn't treat "Alice" and "Alicia", or claim #4471 and #4417, as the
    same. A paraphrase with no words in common can be missed.
- An unknown name sends nothing and returns the closest matches, so a typo shows up as an error
  instead of a new empty agent.
- Before creating an agent, the tool checks whether it looks like a hidden existing one: most of
  the name matches, or it shares 2 or more specific words. If so, it's flagged first.
- If the user's words fit two agents equally, the send is refused and Poke has to ask which one. A
  word only one agent has ("chen"), or recent conversation about one of them, lets it through. Each
  sentence is checked separately, so in "did alice reply? also invite bob to lunch", "lunch" can't
  decide which Alice. Creating a new agent to avoid the question is also refused.
  - This is in code because the prompt alone didn't hold: in the evals the original model asked
    about "move the dentist appointment", but on questions like "did alice reply?" it messaged
    every Alice instead. Picking the wrong customer or claim is a real mistake.

### How I tested it

The unit tests in `tests/test_agent_overload.py` cover each rule above without a model: name matching, the 20-agent list, search ranking, the duplicate flag, and the
ambiguity refusal, including the cases reviewers used to break it (four Alices, a second request in
the same message, creating a new agent to avoid asking).

For routing with the real model I ran live evals against the original and this fork. The scripts
and raw results are in [`evals/`](evals/README.md). The design:

- Run the real interaction agent with the real model (`claude-sonnet-5.5` through OpenRouter) and
  record what it does with workers instead of running them, since routing is what's under test.
- Each scenario seeds a roster of 12, 50 or 200 agents. The target agents are hidden among filler
  agents with similar names ("Lunch invite - Priya Shah #14"), marked recently used or long unused.
  Some scenarios also seed chat history, such as an agent's report from the previous turn.
- Grading is plain code on what happened: which existing agents got a message, which agents were
  created, and whether Poke replied without sending anything. Each scenario has one expected
  outcome: route to this agent, route to all of these, create one new agent, or ask.
- Both versions run the same scenarios in fresh copies of the repo, twice each, on the same day.

| Group (scenarios) | What it checks | Pass | Original | This fork |
|---|---|---|---|---|
| Recent follow-ups (4) | "did alice reply yet?" right after Alice's agent reported; "yes send it" after a draft | route to that agent | 8/8 | 8/8 |
| Old agents (7) | the agent is long unused and, in this fork, off the 20-agent list: "can the lisbon hotel do late checkout?", "move the kids' teeth cleaning" | route to it, create nothing | 14/14 | 14/14 |
| New work (4) | nothing fits: "book me a haircut for saturday" among 200 agents | create one agent | 8/8 | 8/8 |
| Ambiguous (6) | two or four Alices and "did alice reply?"; two dentist appointments and "move the dentist appointment"; "anything new from alice chen?" with two Alice Chen agents | ask which one (for that last vague question, asking or checking both passes) | 2/12 | 12/12 |
| Not ambiguous (3) | "did alice wong send the contract?"; "did alice reply?" right after Alice Chen's report | route without asking | 6/6 | 6/6 |
| Several at once (2) | "did alice chen and bob ortiz reply?" | route to both | 4/4 | 4/4 |
| **Total** | | | **42/52** | **52/52** |

| | Original | This fork |
|---|---|---|
| Duplicate agents created | 0 | 0 |
| Agent list in the prompt at 500 agents | ~24,300 chars | ~2,100 chars, flat from ~50 agents up |
| Average prompt per turn | ~14.8k tokens | ~14.4k tokens |
| Errors | 0 | 0 |

What the numbers say:

- Both versions route the clear cases correctly. The difference is entirely in ambiguous requests.
- 8 of the original's 10 failures were questions where it messaged every matching agent (all four
  Alices for "did alice reply?"). I graded that as a fail because I want Poke to ask, which costs
  one short question instead of a worker run per match and doesn't open threads the user didn't
  mean. That is a grading choice: if checking every match counts as a pass, the original scores
  50/52. The other 2 were real guesses. For "anything new from alice chen?" with two Alice Chen
  agents, it checked only one.
- The average prompt is about the same size in both, because these rosters stop at 200 agents.
  The savings grow with the roster (the 500-agent row is measured separately), and under about 50
  agents this fork's prompt is slightly larger because descriptions are included.
- On the day of the final run, the model often didn't search for hidden agents. In the Tokyo
  scenario the duplicate check stopped "Tokyo flight status" from becoming a second Tokyo agent,
  and Poke used the existing one.
- Later I changed one line of Poke's prompt that told it not to worry if an agent's name was
  unrelated (it had filed new reminders under an old "drink water" reminder agent). I reran this
  fork with a scenario for that added: 57/57. The original also passed that scenario (3/3), so the
  prompt change is reasonable but not proven.

### Limits

- The evals are 2 runs per scenario on made-up rosters, so they catch regressions but aren't a
  precise accuracy number.

- The duplicate check misses some rewordings. An adversarial reviewer first got 5 of 5 through;
  now 4 of 5 are caught. "Alice follow-up" next to "Email to Alice" still gets created.
- The thresholds (20 listed agents, 2 shared specific words, 70% name match) are starting values
  tuned on synthetic scenarios.
- A word that appears in more than half of all agents is treated as generic. If 4 of 4 agents
  mention "Alice", "alice" counts like "email", and Poke isn't stopped from guessing.

## Part 2: The Activity panel

![Activity panel](docs/screenshots/activity-panel.png)

The panel opens beside the chat from an Activity button (on phones it's a full-screen sheet). The
button's badge counts items that need you. Four sections:

1. Needs you: emails waiting for approval, with Send and Don't send.
2. Waiting on: people you're waiting to hear back from, with Stop (Part 3).
3. Coming up: reminders in plain English ("Fri 9:00 · every Fri — Remind you to pay rent"), with
   Cancel and Undo.
4. Recent: one row per task. Clicking one opens its timeline.

### Records instead of the AI's summary

Each line comes from records the code writes as steps happen, not from what the AI says it did. I
found a case where the two disagree. If a worker sends an email and then times out, OpenPoke marks
the task failed, so a user would likely ask again and the email would go out twice. The panel shows
"Sent the email to alice@… ✓", then "Took too long and stopped", then "The email above was already
sent. Check your Sent folder before asking again."

![Task timeline: the email was sent even though the task timed out](docs/screenshots/task-timeline.png)

### Other decisions

- The chat is still the main way to use Poke. The panel is for checking what happened and stepping
  in.
- The panel never says "agent". OpenPoke's prompt tells Poke not to mention agents to the user, so
  steps are described in plain words. Search queries, IDs and error details are in a collapsed Raw
  log.
- Errors include the next step, for example "Out of AI credit. Add credit, then ask again."
- Cancelling a reminder pauses it instead of deleting it, so Undo works.
- I went through an interface checklist: raised text contrast from 2.5:1 to 4.8:1, made the
  "Working" dot respect reduced motion, labelled status icons for screen readers, and added a "Show
  full email" button where text was being cut off.

Left out on purpose: live streaming (polling every few seconds is enough), editing reminders in
the panel (you can do that in chat), search and filters, and analytics.

## Part 3 (my feature): "Waiting on"

![Waiting on in the Activity panel](docs/screenshots/waiting-on.png)

### What it does

People forget to follow up when someone doesn't reply, and in insurance a lot of the work is
following up: asking a customer for documents, or an adjuster for an answer. With this feature,
when you email someone, Poke keeps track that you're waiting on them. If there's no reply in 2
days, it drafts a short nudge in the same thread and texts you: *"Dana hasn't replied about claim
#4471 since Tuesday. A short nudge is ready. Send it?"* If you say yes, it sends. When Dana
replies, Poke tells you and quotes the first line.

- Poke drafts and asks on its own, but it never sends without your approval.
- It nudges once. If there's still no reply 2 days after the nudge, Poke tells you and stops. A
  reply at any point closes it and withdraws a nudge you haven't sent.
- It doesn't bring up nudges between 10pm and 8am. You can stop it from the panel, by declining the
  nudge, or by telling Poke "stop chasing Dana".
- The checking is plain code: one Gmail search per open conversation every 5 minutes, with no model
  calls. The model only writes the text message to you.
- It reuses what OpenPoke already had: the background loop that runs reminders, Gmail search, the
  Activity panel, and the approval step below.

![The drafted nudge waiting for approval](docs/screenshots/follow-up-nudge.png)

### Why it's worth a user's attention

General Magic's agents text insurance customers, and much of insurance involves waiting on
someone: a claim can't move until the customer sends photos, a renewal waits on a signature. This
is the same problem from the individual's side. The safeguards are ones business texting needs
too: quiet hours (US rules such as the TCPA limit when businesses can text), a limit on nudges, an
easy way to stop, and a person approving each message.

I limited it to email because that's the only channel OpenPoke has.

### Approval in code

OpenPoke's only protection against sending the wrong email was a line in the prompt asking for
confirmation. The model can still skip it, and an incoming email with hidden instructions can push
it to send. I moved the rule into code:

- The worker's send, reply and forward tools create a request instead of sending. You approve it in
  chat ("yes, send it") or with the Send button. A chat approval only counts on a turn you started,
  not on an agent's report.
- Each request sends at most once. It's claimed under a lock before sending, and five simultaneous
  approvals send one email (tested).
- The approval card lists every recipient, including cc and bcc. An earlier version showed only
  "To", which could hide a bcc; three separate reviewers caught it.
- Emails only to yourself skip approval, so "email me a summary every night" still works.
- Live check, 3 runs each: "yes send it" sent exactly one email every time, and "actually make it
  1pm instead" declined the old draft and sent nothing every time.

## Other fixes

- The default model, `anthropic/claude-sonnet-4`, was removed from OpenRouter, so every message
  failed. The default is now `claude-sonnet-5.5`, and `OPENPOKE_MODEL` overrides it.
- Reminders could run twice. The scheduler marked a reminder done only after it ran, so a restart
  mid-run ran it again. It's now marked first. If the server dies mid-run, the reminder shows as
  "Didn't finish" for you to rerun.
- Newer models called `wait` over and over until the 8-step limit, so about 7 in 24 turns ended in
  an error. A step that only waits now ends the turn.
- Every call reserved 65,536 output tokens, which fails on keys with little credit. It's now 4,096.

## With another week

1. Test "Waiting on" end to end with real people. An approved send to someone else, a real reply
   and a real nudge have only been tested with fakes and demo data. Sending to myself and Gmail
   search have been tested for real.
2. Be smarter about what counts as a reply. Right now any email from that person counts, even on
   another topic. The nudge is also a fixed template; Poke should write it from the thread.
3. Add the reverse: "you owe a reply" ("Priya asked you something on Tuesday"), using the same
   pieces plus the existing important-email watcher.
4. Prepare for more than one user:
   - Data is in JSON files and SQLite on one machine, so two servers would both run every reminder.
     It needs one shared database, with each reminder claimed by a single update.
   - There's no login, and any website can call the server. That has to change before a real
     deployment.
   - Gmail is polled. At scale it should use Gmail's push notifications.
5. Cap worker memory. Each worker resends its whole history on every call; it should keep recent
   messages plus a summary.
6. Improve the evals. Two runs per scenario on made-up rosters catches regressions but isn't a
   precise accuracy number. I'd add scenarios with no shared words to find out whether embeddings
   are needed.

## How I verified it

- 69 unit tests, which run without API keys, models or Gmail: `python -m pytest tests -q`.
- 65 break-it checks: for each fix, a script breaks it in a throwaway copy and confirms a test
  fails, so no test is passing by accident.
- Live evals with the real model against the original, plus reruns after each fix. About $29 of
  OpenRouter credit in total.
- Real Gmail: connected an account, ran searches, and sent an email to myself that Gmail confirmed.
- In a browser at desktop and phone width: approving and declining, cancelling and undoing a
  reminder, and checking the console for errors.
- Adversarial review: two reviewers on different models were asked to break the code and prove
  each break with a command. They reported 31 issues and all of them reproduced. The ones that
  contradicted something this write-up claims are fixed, each with a test. The rest are edge cases,
  the main ones listed under Part 1's limits.
