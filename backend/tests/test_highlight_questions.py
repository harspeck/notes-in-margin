"""Highlights filed under research questions, breadth, and guide ideas (snapshot mode;
tests/test_cortico_api_mode.py covers the hosted behaviour)."""
import itertools

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.db import PrototypeSession
from app.main import app
from app.models_prototype import HighlightQuestion

client = TestClient(app)
_user_ids = itertools.count(94000)


@pytest.fixture
def user_id():
    uid = next(_user_ids)
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def _rq(name: str) -> int:
    return client.post("/research_questions", json={"name": name}).json()["id"]


def _participant_snippets(conversation_id: int) -> list[int]:
    snippets = client.get(f"/conversations/{conversation_id}").json()["snippets"]
    return [s["snippet_id"] for s in snippets if not s["is_facilitator"] and len(s["words"]) >= 12]


def _highlight(conversation_id: int, snippet_id: int, start: int = 0, end: int = 5, **extra) -> int:
    body = {"snippets": [{"snippet_id": snippet_id, "start_index": start, "end_index": end}], **extra}
    r = client.post(f"/conversations/{conversation_id}/highlights", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["research_question_ids"] == []
    return r.json()["id"]


def _file_insight(conversation_id: int, rq_id: int | None, text: str, evidence: list[dict]) -> int:
    vm = client.post(f"/conversations/{conversation_id}/reflections", json={"text": "a reflection"}).json()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq_id,
                           "insight_text": text, "evidence": evidence}]}
    r = client.post(f"/voice_memos/{vm['id']}/file", json=body)
    assert r.status_code == 200, r.text
    return r.json()["connections"][0]["id"]


def test_put_questions_replaces_the_set_and_every_highlight_route_returns_it(user_id):
    a, b, c = _rq("filed under a"), _rq("filed under b"), _rq("filed under c")
    (s1, *_) = _participant_snippets(12)
    h = _highlight(12, s1)

    r = client.put(f"/highlights/{h}/questions", json={"research_question_ids": [a, b, a]})
    assert r.status_code == 200, r.text
    assert r.json()["research_question_ids"] == [a, b]
    assert client.get("/conversations/12/highlights").json()[0]["research_question_ids"] == [a, b]
    assert client.patch(f"/highlights/{h}", json={"starred": True}).json()["research_question_ids"] == [a, b]

    # Replace, not add: b goes, c arrives, a's row is kept.
    with PrototypeSession() as db:
        a_row = db.query(HighlightQuestion).filter_by(highlight_id=h, research_question_id=a).one().id
    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [c, a]}).json()[
        "research_question_ids"] == [a, c]
    with PrototypeSession() as db:
        rows = db.query(HighlightQuestion).filter_by(highlight_id=h).all()
        assert {r.research_question_id for r in rows} == {a, c}
        assert a_row in {r.id for r in rows} and all(r.user_id == user_id for r in rows)

    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": []}).json()[
        "research_question_ids"] == []
    kinds = [i["kind"] for i in client.get("/activity").json()["items"]]
    assert kinds.count("highlight_filed") == 3


def test_put_questions_validates_ownership_archive_and_resolves_merged_questions(user_id):
    mine, archived, source, target = _rq("mine"), _rq("archived one"), _rq("merge source"), _rq("merge target")
    (s1, *_) = _participant_snippets(12)
    h = _highlight(12, s1)
    client.patch(f"/research_questions/{archived}", json={"archived": True})
    assert client.post(f"/research_questions/{source}/merge_into", json={"target_id": target}).status_code == 200

    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [archived]}).status_code == 409
    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [999_999_999]}).status_code == 404
    # A merged question is archived, but it resolves to its target, which is stored.
    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [source, mine]}).json()[
        "research_question_ids"] == [target, mine]
    with PrototypeSession() as db:
        assert {r.research_question_id for r in db.query(HighlightQuestion).filter_by(highlight_id=h)} == {target, mine}

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    other_rq = _rq("someone else's")
    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [other_rq]}).status_code == 404
    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id)
    assert client.put(f"/highlights/{h}/questions", json={"research_question_ids": [other_rq]}).status_code == 404


def test_question_highlights_lists_only_those_no_filed_insight_cites(user_id):
    rq, other = _rq("loose highlights"), _rq("another question")
    s1, s2, s3 = _participant_snippets(12)[:3]
    loose = _highlight(12, s1, 2, 7, notes="worth a second look")
    cited = _highlight(12, s2, 0, 6)
    elsewhere = _highlight(12, s3, 0, 4)
    for h in (loose, cited):
        client.put(f"/highlights/{h}/questions", json={"research_question_ids": [rq]})
    client.put(f"/highlights/{elsewhere}/questions", json={"research_question_ids": [other]})
    client.patch(f"/highlights/{loose}", json={"starred": True})
    # Cited by a filed insight under ANOTHER question: still cited, so not loose.
    _file_insight(12, other, "cites the second", [{"highlight_id": cited, "snippet_id": s2, "start_index": 0, "end_index": 6}])

    body = client.get(f"/research_questions/{rq}/highlights").json()
    assert body["research_question_id"] == rq
    (item,) = body["highlights"]
    assert (item["highlight_id"], item["conversation_id"], item["notes"], item["starred"]) == (
        loose, 12, "worth a second look", True)
    assert item["conversation_title"] and item["research_question_ids"] == [rq]
    assert item["snippets"] == [{"snippet_id": s1, "start_index": 2, "end_index": 7, "text": item["text"]}]
    assert len(item["text"].split()) == 5

    # Read through the merged group: `other` merged into rq brings its loose highlight along.
    assert client.post(f"/research_questions/{other}/merge_into", json={"target_id": rq}).status_code == 200
    for asked in (rq, other):
        got = client.get(f"/research_questions/{asked}/highlights").json()
        assert got["research_question_id"] == rq
        assert [h["highlight_id"] for h in got["highlights"]] == [loose, elsewhere]

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    assert client.get(f"/research_questions/{rq}/highlights").status_code == 404


def test_deleting_a_highlight_deletes_its_question_links(user_id):
    rq = _rq("links go with the highlight")
    (s1, *_) = _participant_snippets(12)
    h = _highlight(12, s1)
    client.put(f"/highlights/{h}/questions", json={"research_question_ids": [rq]})
    assert client.delete(f"/highlights/{h}").json()["deleted"] is True
    with PrototypeSession() as db:
        assert db.query(HighlightQuestion).filter_by(highlight_id=h).count() == 0
    assert client.get(f"/research_questions/{rq}/highlights").json()["highlights"] == []


def test_breadth_splits_insights_from_highlight_only_and_counts_what_was_opened(user_id):
    a, b, empty = _rq("breadth a"), _rq("breadth b"), _rq("breadth empty")
    corpus = client.get("/conversations").json()
    project_of = {c["id"]: c["project_id"] for c in corpus}

    client.put("/conversations/14/reading_position")  # opened, nothing made in it
    (s9, *_) = _participant_snippets(9)
    (s12, *_) = _participant_snippets(12)
    (s13, *_) = _participant_snippets(13)
    _file_insight(9, a, "heard in nine", [{"snippet_id": s9, "start_index": 0, "end_index": 4}])
    _file_insight(9, None, "fits nowhere yet", [])
    h12 = _highlight(12, s12)
    h13 = _highlight(13, s13)
    client.put(f"/highlights/{h12}/questions", json={"research_question_ids": [a]})
    client.put(f"/highlights/{h13}/questions", json={"research_question_ids": [b]})

    body = client.get("/research_questions/breadth").json()
    assert body["opened_conversation_ids"] == [9, 12, 13, 14] and body["opened_conversation_count"] == 4
    assert body["projects_in_scope_count"] == len(set(project_of.values())) == len(body["projects_in_scope"])
    by_id = {q["research_question_id"]: q for q in body["questions"]}
    assert set(by_id) == {a, b, empty}

    qa = by_id[a]
    assert qa["insight_conversation_ids"] == [9] and qa["highlight_only_conversation_ids"] == [12]
    assert qa["conversation_ids"] == [9, 12] and qa["conversation_count"] == 2
    assert qa["project_ids_heard"] == sorted({project_of[9], project_of[12]}) and qa["projects_heard_count"] == 2
    assert (qa["insight_count"], qa["highlight_count"]) == (1, 1)
    assert by_id[b]["highlight_only_conversation_ids"] == [13] and by_id[b]["insight_conversation_ids"] == []
    assert by_id[empty]["conversation_count"] == 0 and by_id[empty]["project_ids_heard"] == []
    assert body["unfiled"]["insight_count"] == 1 and body["unfiled"]["conversation_ids"] == [9]

    # A merged question reads under its target, and is no longer listed itself.
    assert client.post(f"/research_questions/{b}/merge_into", json={"target_id": a}).status_code == 200
    merged = {q["research_question_id"]: q for q in client.get("/research_questions/breadth").json()["questions"]}
    assert set(merged) == {a, empty}
    assert merged[a]["conversation_ids"] == [9, 12, 13] and merged[a]["highlight_count"] == 2
    assert merged[a]["highlight_only_conversation_ids"] == [12, 13]

    app.dependency_overrides[current_user] = lambda: CurrentUser(id=user_id + 500000)
    other = client.get("/research_questions/breadth").json()
    assert other["questions"] == [] and other["opened_conversation_count"] == 0


def test_guide_ideas_are_distinct_guide_questions_not_already_asked(user_id):
    body = client.get("/research_questions/guide_ideas").json()
    ideas = body["ideas"]
    in_project_5 = sum(1 for c in client.get("/conversations").json() if c["project_id"] == 5)
    assert 0 < len(ideas) <= 12 and body["projects_not_loaded"] == []
    assert len({i["text"].casefold() for i in ideas}) == len(ideas)
    # Project 5 has the snapshot's only guide.
    assert all(i["project_ids"] == [5] and i["conversation_count"] == in_project_5 for i in ideas)
    assert {"Consent", "Guidelines", "Q1 Intros"} <= {i["shorthand_code"] for i in ideas}

    taken = ideas[0]["text"]
    _rq(f"  {taken.upper()} ")  # compared without case or outer spaces
    after = [i["text"] for i in client.get("/research_questions/guide_ideas").json()["ideas"]]
    assert taken not in after and len(after) == len(ideas) - 1
