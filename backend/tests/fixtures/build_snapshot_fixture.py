"""Builds a small synthetic stand-in for acp_pipeline_snapshot.db.

The real snapshot is a gitignored 146 MB copy of Cortico's database. The tests need only a
few of its facts, so this script recreates those facts with invented data. All text is
generic and synthetic; nothing comes from a real transcript.

Usage: python build_snapshot_fixture.py OUT_PATH

The output is deterministic. The tables come from db/schema.sql, executed into the new file
only. The script refuses to write over an existing file, so it can never touch a real
snapshot.

Facts mirrored from the real snapshot (docs/prototype-log.md, docs/architecture/tests.md):
- 201 conversations (ids 1-201) across 54 projects. Conversation 1 is in project 1, which
  has no guide. Conversations 9 and 10 are the only ones in project 5.
- Project 5 has the only conversation guide. Its guide sections restart question
  order_index at 0, and the questions run "Consent", "Guidelines", "Q1 Intros", ...
  Guide question 4 belongs to it.
- conversation_guide_id and audio_url are NULL for every conversation.
- Conversations 160, 162 and 201 have no snippets. Every other conversation has at least
  20, most of them 20 words or longer, and snippets 1936 and 1938 are in conversation 9.
- snippet.words is a JSON list of {word, start, end, confidence} dicts whose words, joined
  with spaces, equal snippet.text.
- Every cortico_id is the local id plus 100000.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db" / "schema.sql"

SEED = 20261106
CORTICO_ID_OFFSET = 100_000
N_PROJECTS = 54
N_CONVERSATIONS = 201
GUIDE_PROJECT_ID = 5
GUIDE_PROJECT_CONVERSATIONS = (9, 10)
EMPTY_CONVERSATIONS = (160, 162, 201)
SNIPPET_ID_BLOCK = 240  # conversation c owns snippet ids (c-1)*240+1 .. c*240
MIN_SNIPPETS, MAX_SNIPPETS = 20, 36
WORD_SECONDS = 0.4

GUIDE_SECTIONS = [
    ("Opening", [
        ("Consent", "Synthetic prompt: confirm that everyone agrees to be recorded."),
        ("Guidelines", "Synthetic prompt: review the conversation guidelines together."),
    ]),
    ("Introductions", [
        ("Q1 Intros", "Synthetic prompt: share your name and a place that matters to you."),
    ]),
    ("Discussion", [
        ("Q2 Place", "Synthetic prompt: what do you value most about where you live?"),
        ("Q3 Change", "Synthetic prompt: describe a change you have noticed recently."),
        ("Q4 Hopes", "Synthetic prompt: what would you like to see in the coming years?"),
    ]),
    ("Closing", [
        ("Q5 Closing", "Synthetic prompt: is there anything else you want others to hear?"),
    ]),
]

MECHANICAL_OPENER = "Okay, the synthetic recording is running now. Thanks for joining."

FACILITATOR_LINES = [
    "Thank you for sharing that. Could you tell us a little more about what that looked like for you?",
    "That is a helpful example. How do other people in the group feel about the same question?",
    "Let us move to the next prompt. What do you value most about the place where you live?",
    "I want to make sure everyone gets a turn. Would anyone who has not spoken yet like to add something?",
    "We have a few minutes left on this part. What would you want a newcomer to understand about it?",
    "That connects to something said earlier. Can you say how those two experiences relate for you?",
]

PARTICIPANT_SENTENCES = [
    "In this synthetic example, the speaker describes a weekly market near the library.",
    "I think the sample park on the corner brings people together on weekend mornings.",
    "Our placeholder neighborhood has a bus route that many older residents depend on.",
    "When the example community center added evening hours, more families started coming by.",
    "Honestly, the invented downtown feels quieter than it did a few years ago.",
    "My imaginary neighbors and I trade garden vegetables over the fence every summer.",
    "The test school runs a volunteer reading program that my cousin helps organize.",
    "Sometimes it is hard to find out about local meetings until they are already over.",
    "I would like to see more shaded benches along the made-up river trail.",
    "A fictional bakery on the main street closed, and people still talk about it.",
    "We started a small sample group chat so people could share rides to appointments.",
    "The generic town hall should post its decisions somewhere that is easier to read.",
    "I moved here for a placeholder job and stayed because people were welcoming.",
    "Rent in this invented area keeps rising, which worries the younger residents.",
    "There is a synthetic festival each autumn where every block brings a dish.",
    "Kids in the example district need more safe places to go after school ends.",
    "I trust my neighbors more than the news when I want to know what is happening.",
    "The pretend clinic is far away, so a shuttle would make a real difference.",
]


def _words_for(text: str, start: float, rng: random.Random) -> list[dict]:
    words = []
    t = start
    for token in text.split(" "):
        words.append(
            {
                "word": token,
                "start": round(t, 2),
                "end": round(t + WORD_SECONDS * 0.9, 2),
                "confidence": round(rng.uniform(0.82, 0.99), 3),
            }
        )
        t += WORD_SECONDS
    return words


def _participant_text(rng: random.Random) -> str:
    sentences: list[str] = []
    while sum(len(s.split(" ")) for s in sentences) < 22:
        sentences.append(rng.choice(PARTICIPANT_SENTENCES))
    return " ".join(sentences)


def build(out_path: str | Path) -> Path:
    out = Path(out_path)
    if out.exists():
        raise FileExistsError(f"{out} already exists; the fixture is only ever built into a new file")
    out.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    conn = sqlite3.connect(out)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        _insert_rows(conn, rng)
        conn.commit()
    finally:
        conn.close()
    return out


def _cid(local_id: int) -> int:
    return local_id + CORTICO_ID_OFFSET


def _project_for(conversation_id: int) -> int:
    if conversation_id in GUIDE_PROJECT_CONVERSATIONS:
        return GUIDE_PROJECT_ID
    # Spread the rest over every project except 5, starting with conversation 1 in project 1.
    p = (conversation_id - 1) % (N_PROJECTS - 1) + 1
    return p + 1 if p >= GUIDE_PROJECT_ID else p


def _insert_rows(conn: sqlite3.Connection, rng: random.Random) -> None:
    conn.execute("INSERT INTO organization (id, cortico_id, name) VALUES (1, ?, ?)", (_cid(1), "Synthetic Organization"))
    conn.execute(
        "INSERT INTO community (id, cortico_id, organization_id, name, description) VALUES (1, ?, 1, ?, ?)",
        (_cid(1), "Synthetic Community", "Invented community for tests."),
    )
    conn.execute(
        "INSERT INTO forum (id, cortico_id, community_id, name, is_public) VALUES (1, ?, 1, ?, 0)",
        (_cid(1), "Synthetic Forum"),
    )
    conn.executemany(
        "INSERT INTO project (id, cortico_id, organization_id, name, description) VALUES (?, ?, 1, ?, ?)",
        [(p, _cid(p), f"Synthetic Project {p}", f"Invented project {p} for tests.") for p in range(1, N_PROJECTS + 1)],
    )

    conn.execute(
        "INSERT INTO conversationguide (id, cortico_id, project_id, name, version) VALUES (1, ?, ?, ?, 1)",
        (_cid(1), GUIDE_PROJECT_ID, "Synthetic Conversation Guide"),
    )
    question_id = 0
    for section_order, (title, questions) in enumerate(GUIDE_SECTIONS):
        section_id = section_order + 1
        conn.execute(
            "INSERT INTO guidesection (id, conversation_guide_id, title, order_index) VALUES (?, 1, ?, ?)",
            (section_id, title, section_order),
        )
        for question_order, (code, text) in enumerate(questions):
            question_id += 1
            conn.execute(
                "INSERT INTO guidequestion (id, guide_section_id, text, shorthand_code, order_index)"
                " VALUES (?, ?, ?, ?, ?)",
                (question_id, section_id, text, code, question_order),
            )

    participant_id = 0
    for c in range(1, N_CONVERSATIONS + 1):
        facilitator_id = participant_id + 1
        speaker_ids = []
        for i, (name, is_facilitator) in enumerate(
            [(f"Facilitator {c}", True), (f"Participant {c}-A", False), (f"Participant {c}-B", False), (f"Participant {c}-C", False)]
        ):
            participant_id += 1
            conn.execute(
                "INSERT INTO participant (id, conversation_id, name, is_facilitator, is_registered_user, cortico_user_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (participant_id, c, name, int(is_facilitator), int(is_facilitator), _cid(participant_id) if is_facilitator else None),
            )
            if not is_facilitator:
                speaker_ids.append(participant_id)

        duration = _insert_snippets(conn, rng, c, facilitator_id, speaker_ids)
        conn.execute(
            "INSERT INTO conversation (id, cortico_id, forum_id, project_id, conversation_guide_id, title, audio_url,"
            " duration_seconds, language, is_open, facilitator_participant_id)"
            " VALUES (?, ?, 1, ?, NULL, ?, NULL, ?, 'en', 0, ?)",
            (c, _cid(c), _project_for(c), f"Synthetic Conversation {c}", duration, facilitator_id),
        )


def _insert_snippets(
    conn: sqlite3.Connection, rng: random.Random, conversation_id: int, facilitator_id: int, speaker_ids: list[int]
) -> float:
    if conversation_id in EMPTY_CONVERSATIONS:
        return 0.0
    n = rng.randint(MIN_SNIPPETS, MAX_SNIPPETS)
    base_id = (conversation_id - 1) * SNIPPET_ID_BLOCK + 1
    t = 0.0
    for order in range(n):
        if order == 0:
            text, speaker, is_prompt, is_mechanical = MECHANICAL_OPENER, facilitator_id, False, True
        elif order % 4 == 1:
            text, speaker, is_prompt, is_mechanical = rng.choice(FACILITATOR_LINES), facilitator_id, True, False
        else:
            text, speaker, is_prompt, is_mechanical = _participant_text(rng), rng.choice(speaker_ids), False, False
        words = _words_for(text, t, rng)
        end = words[-1]["end"]
        snippet_id = base_id + order
        conn.execute(
            "INSERT INTO snippet (id, cortico_id, conversation_id, participant_id, order_index, start_offset,"
            " end_offset, text, words, is_facilitator_prompt, is_mechanical, ai_score)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snippet_id,
                _cid(snippet_id),
                conversation_id,
                speaker,
                order,
                round(t, 2),
                end,
                text,
                json.dumps(words),
                int(is_prompt),
                int(is_mechanical),
                None if is_mechanical else round(rng.random(), 3),
            ),
        )
        t = end + 0.8
    return round(t, 2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python build_snapshot_fixture.py OUT_PATH")
    print(build(sys.argv[1]))
