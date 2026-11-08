"""Routing scenarios for live runs. Same data for every version.

expect:
  ("route", name)          send to exactly this existing agent, create nothing
  ("route_all", [names])   send to all of these, nothing else, create nothing
  ("new",)                 create exactly one new agent, send to no existing agent
  ("ask",)                 send nothing, create nothing, reply to the user
  ("ask_or_all", [names])  either ask, or check every listed agent (a question across both)
  ("new_or_route", name)   either is defensible (same person, new topic)
recency: "recent" = most recently used agent, "old" = least recently used
ran_recently: the agent just ran (e.g. a reminder fired), which some versions count as use
"""
import random

PEOPLE = ["Priya Shah", "Dana Kim", "Omar Haddad", "Lena Novak", "Sam Lee", "Jordan Park",
          "Mei Tan", "Luca Bianchi", "Nora Ahmed", "Tom Fischer", "Ivy Zhou", "Raj Patel"]
TOPICS = [("Lunch invite", "Lunch invite to {p}"), ("Invoice question", "Invoice question from {p}"),
          ("Flight change", "Changing {p}'s flight"), ("Offer letter", "Offer letter from {p}"),
          ("Conference talk", "Conference talk proposal with {p}"), ("Refund request", "Refund request for {p}"),
          ("School pickup", "School pickup schedule with {p}"), ("Book club", "Book club picks with {p}"),
          ("Gym class", "Gym class booking with {p}"), ("Wedding RSVP", "RSVP to {p}'s wedding"),
          ("Project update", "Weekly project update to {p}"), ("Intro request", "Intro request via {p}")]


def filler(n, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        p = rng.choice(PEOPLE)
        title, desc = rng.choice(TOPICS)
        out.append((f"{title} - {p} #{i}", desc.format(p=p)))
    return out


ALICE_CHEN = ("Email to Alice Chen", "Lunch invite to Alice Chen for Friday")
ALICE_WONG = ("Email to Alice Wong", "Contract review with Alice Wong")
CHEN_REPORT = [("user_message", "invite alice chen to lunch friday"),
               ("agent_message", ("Email to Alice Chen", "Sent the lunch invite to Alice Chen (alice.chen@acme.co).")),
               ("poke_reply", "Invite's out to Alice Chen for Friday.")]

S = []


def add(sid, targets, size, message, expect, history=(), recency="recent", ran_recently=()):
    S.append(dict(id=sid, targets=list(targets), size=size, message=message, expect=expect,
                  history=list(history), recency=recency, ran_recently=list(ran_recently)))


# Follow-ups to agents that are already in view
add("recent_followup_small", [ALICE_CHEN], 12, "did alice reply yet?", ("route", ALICE_CHEN[0]), CHEN_REPORT)
add("recent_followup_large", [ALICE_CHEN], 200, "did alice reply yet?", ("route", ALICE_CHEN[0]), CHEN_REPORT)
add("recent_paraphrase", [("Q3 budget review", "Reviewing the Q3 budget spreadsheet with finance")], 200,
    "what did finance say about the q3 numbers?", ("route", "Q3 budget review"))
add("generic_followup_after_draft", [ALICE_CHEN, ALICE_WONG], 50, "yes send it",
    ("route", ALICE_CHEN[0]),
    [("user_message", "ask alice chen if she's free for lunch friday"),
     ("agent_message", ("Email to Alice Chen", "Draft ready to alice.chen@acme.co: 'Free for lunch Friday?' Awaiting confirmation to send.")),
     ("poke_reply", "Draft to Alice Chen: 'Free for lunch Friday?' Send it?")])

# Old agents, off any short list
add("old_keyword_hotel", [("Hotel booking - Lisbon", "Hotel stay in Lisbon for the March conference")], 200,
    "can you check if the lisbon hotel has late checkout?", ("route", "Hotel booking - Lisbon"), recency="old")
add("old_keyword_vercel", [("Vercel offer", "Start date negotiation with the Vercel recruiter")], 200,
    "push my vercel start date to june 2", ("route", "Vercel offer"), recency="old")
add("old_paraphrase_dentist", [("Dentist appointment - kids", "Kids' dental cleaning at Bright Smiles")], 200,
    "move the kids' teeth cleaning to next week", ("route", "Dentist appointment - kids"), recency="old")
add("old_person_only", [("Email to Marco Rossi", "Design review feedback from Marco Rossi")], 200,
    "did marco ever get back to me?", ("route", "Email to Marco Rossi"), recency="old")
add("old_lease", [("Lease renewal - Elm St", "Lease renewal paperwork with the Elm St landlord")], 200,
    "did the landlord sign the renewal yet?", ("route", "Lease renewal - Elm St"), recency="old")
add("old_vs_recent_similar",
    [("Flight to Tokyo - March", "Flight to Tokyo for the March trip"),
     ("Flight to Lisbon - April", "Flight to Lisbon for the April trip")], 200,
    "is my tokyo flight still on time?", ("route", "Flight to Tokyo - March"), recency=["old", "recent"])
add("old_context_only", [("Rent reminder", "Monthly rent reminder for the Elm St apartment")], 200,
    "snooze it a week", ("route", "Rent reminder"),
    [("agent_message", ("Rent reminder", "Reminder fired: rent for Elm St is due Friday.")),
     ("poke_reply", "Rent's due Friday.")],
    recency="old", ran_recently=["Rent reminder"])

# New work
add("new_small", [ALICE_CHEN], 12, "find me a birthday gift for my dad under $80", ("new",))
add("new_large", [ALICE_CHEN], 200, "book me a haircut for saturday morning", ("new",))
add("new_common_words", [ALICE_CHEN], 200, "email my landlord that the heater is broken", ("new",))
add("new_same_person_new_topic",
    [("Invoice question - Bob Ortiz", "Invoice question from Bob Ortiz")], 200,
    "ask bob ortiz if he wants to play tennis sunday", ("new_or_route", "Invoice question - Bob Ortiz"))

# Ambiguous: must ask
add("ambiguous_alices_small", [ALICE_CHEN, ALICE_WONG], 12, "did alice reply?", ("ask",))
add("ambiguous_alices_large", [ALICE_CHEN, ALICE_WONG], 200, "did alice reply?", ("ask",))
add("ambiguous_dentists",
    [("Dentist appointment - mine", "My cleaning at Dr. Lee's office"),
     ("Dentist appointment - kids", "Kids' cleaning at Bright Smiles")], 50,
    "move the dentist appointment to friday", ("ask",))
# Found by adversarial review (2026-11-07)
add("ambiguous_plus_other_request", [ALICE_CHEN, ALICE_WONG], 12,
    "did alice reply? also invite bob to lunch on friday", ("ask_plus_new",))
add("ambiguous_four_alices",
    [ALICE_CHEN, ALICE_WONG, ("Email to Alice Park", "Quote request from Alice Park"),
     ("Email to Alice Kim", "Claim follow-up with Alice Kim")], 12, "did alice reply?", ("ask",))

# Found by using the app (2026-11-07): Poke filed new reminders under an unrelated reminder agent
add("new_reminder_not_reused", [("Reminder - drink water", "One-off reminder in 2 minutes for the user to drink water")], 12,
    "remind me every friday at 9am to call alice chen about lunch", ("new",))

# Not ambiguous after all
add("distinguishing_word", [ALICE_CHEN, ALICE_WONG], 50, "did alice wong send back the contract?",
    ("route", ALICE_WONG[0]))
add("context_decides", [ALICE_CHEN, ALICE_WONG], 50, "did alice reply?", ("route", ALICE_CHEN[0]), CHEN_REPORT)
add("same_person_two_topics",
    [ALICE_CHEN, ("Alice Chen visa paperwork", "Visa paperwork for Alice Chen's work permit")], 50,
    "any update on alice's visa forms?", ("route", "Alice Chen visa paperwork"))
add("same_person_vague",
    [ALICE_CHEN, ("Alice Chen visa paperwork", "Visa paperwork for Alice Chen's work permit")], 50,
    "anything new from alice chen?", ("ask_or_all", [ALICE_CHEN[0], "Alice Chen visa paperwork"]))

# Several at once
add("multi_agent", [ALICE_CHEN, ("Invoice question - Bob Ortiz", "Invoice question from Bob Ortiz")], 50,
    "did alice chen and bob ortiz reply?", ("route_all", [ALICE_CHEN[0], "Invoice question - Bob Ortiz"]))
add("multi_agent_old", [("Hotel booking - Lisbon", "Hotel stay in Lisbon for the March conference"),
                        ("Vercel offer", "Start date negotiation with the Vercel recruiter")], 200,
    "any news on the lisbon hotel or my vercel start date?",
    ("route_all", ["Hotel booking - Lisbon", "Vercel offer"]), recency="old")

SCENARIOS = {s["id"]: s for s in S}
