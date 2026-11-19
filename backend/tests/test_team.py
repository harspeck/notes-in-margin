"""The Team Page and team codebook: what a viewer may see of teammates' work, adopting a
question, question groups and team themes. The model is never called: `structured_json` in
services/team.py is replaced with a fake that returns hand-built output.

The test that matters most is `test_a_narrower_workspace_sees_nothing_made_outside_it`: the
conversation and snippet caches are shared by every user, so services/team.py is the only
thing keeping a teammate's work on another project's conversation out of a team page."""
import itertools
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_cortico_api_mode import FakeCortico, _login, _v2_calls, _workspace

from app import auth as auth_module
from app.auth import CurrentUser, current_user
from app.config import get_config
from app.db import PrototypeSession
from app.llm import StructuredResult
from app.main import app
from app.models_prototype import (
    Adoption,
    CodeFamily,
    ConnectionEvidence,
    NextStepSuggestion,
    ReflectionConnection,
    ResearchQuestion,
    TeamQuestionGroup,
    TeamTheme,
    TeamThemeRevision,
    VoiceMemo,
)
from app.services import team

client = TestClient(app)
_user_ids = itertools.count(98000)
MISSING = 999_999_999


@pytest.fixture
def act_as():
    """Snapshot mode: switch between fresh users, because every test file shares one database."""

    def switch(uid: int) -> None:
        app.dependency_overrides[current_user] = lambda: CurrentUser(id=uid)

    yield switch
    app.dependency_overrides.pop(current_user, None)


class FakeModel:
    def __init__(self, monkeypatch, data):
        self.data = data
        self.calls: list[dict] = []
        monkeypatch.setattr(team, "structured_json", self)

    def __call__(self, *, system, user, schema, name, effort=None, model=None):
        self.calls.append({"payload": json.loads(user), "model": model, "effort": effort, "name": name})
        return StructuredResult(data=self.data, provider="anthropic", model=model or "default-model", reasoning_effort=effort or "low")


def _accept(text, rq_id, evidence=()):
    return {"connection_id": None, "action": "accept", "research_question_id": rq_id, "insight_text": text,
            "evidence": list(evidence)}


def _file(http, vm_id, *decisions) -> dict[str, int]:
    """Files the reflection and returns its connections' ids by insight text."""
    r = http.post(f"/voice_memos/{vm_id}/file", json={"decisions": list(decisions)})
    assert r.status_code == 200, r.text
    with PrototypeSession() as db:
        rows = db.query(ReflectionConnection).filter(ReflectionConnection.voice_memo_id == vm_id)
        return {c.insight_text: c.id for c in rows}


def _seed_connection(vm_id: int, text: str, status: str, rq_id: int | None = None) -> int:
    with PrototypeSession() as db:
        c = ReflectionConnection(voice_memo_id=vm_id, research_question_id=rq_id, insight_text=text, source="ai", status=status)
        db.add(c)
        db.commit()
        return c.id


def _question(name: str, description: str | None = None) -> int:
    r = client.post("/research_questions", json={"name": name, "description": description})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _snippet_9() -> dict:
    return client.get("/conversations/9").json()["snippets"][0]


def _filed_insight(rq_id: int, text: str, reflection: str = "a reflection") -> int:
    """A typed reflection on conversation 9 with one insight filed under rq_id, as the acting user."""
    vm = client.post("/conversations/9/reflections", json={"text": reflection}).json()
    span = {"snippet_id": _snippet_9()["snippet_id"], "start_index": 0, "end_index": 2}
    return _file(client, vm["id"], _accept(text, rq_id, [span]))[text]


def _by(rows, key, value):
    return next((row for row in rows if row[key] == value), None)


# --- 1. what a teammate sees ------------------------------------------------------------------


def test_a_teammate_sees_filed_work_and_nothing_undecided_or_set_aside(act_as):
    a, b = next(_user_ids), next(_user_ids)
    tag = f"t{a}"
    act_as(a)
    rq = _question(f"{tag} question", "why it matters")
    snippet = _snippet_9()
    span = {"snippet_id": snippet["snippet_id"], "start_index": 0, "end_index": 2}
    quote = " ".join(w["word"] for w in snippet["words"][:2])
    highlight = client.post("/conversations/9/highlights", json={"notes": f"{tag} note", "snippets": [span]}).json()
    assert client.put(f"/highlights/{highlight['id']}/questions", json={"research_question_ids": [rq]}).status_code == 200
    assert client.patch(f"/highlights/{highlight['id']}", json={"starred": True}).status_code == 200
    vm = client.post("/conversations/9/reflections", json={"text": f"{tag} reflection"}).json()
    rejected = _seed_connection(vm["id"], f"{tag} hidden rejected", "suggested", rq)
    ids = _file(
        client, vm["id"], {"connection_id": rejected, "action": "reject"},
        _accept(f"{tag} filed insight", rq, [span]), _accept(f"{tag} hidden retracted", rq),
    )
    assert client.post(f"/memo/connections/{ids[f'{tag} hidden retracted']}/retract").status_code == 200
    _seed_connection(vm["id"], f"{tag} hidden suggested", "suggested", rq)
    _seed_connection(vm["id"], f"{tag} hidden merged", "merged", rq)
    with PrototypeSession() as db:  # A's undecided AI suggestions
        db.add(NextStepSuggestion(user_id=a, research_question_id=rq, suggested_direction=f"{tag} hidden next step", status="suggested"))
        db.add(CodeFamily(user_id=a, research_question_id=rq, name=f"{tag} hidden family", definition="d", status="suggested"))
        db.commit()
    assert f"{tag} hidden rejected" in client.get("/memo/set_aside").text  # A's own set-aside list holds it

    act_as(b)
    mine = client.post("/conversations/9/highlights", json={"notes": "my own", "snippets": [span]}).json()
    responses = {path: client.get(path) for path in (
        "/team", f"/team/questions/{rq}", f"/team/people/{a}", "/conversations/9/team_highlights",
        "/memo/set_aside", "/next_steps", "/activity?limit=200",
    )}
    for path, r in responses.items():
        assert r.status_code == 200, (path, r.text)
        assert f"{tag} hidden" not in r.text, path
    assert tag not in responses["/memo/set_aside"].text and tag not in responses["/activity?limit=200"].text

    overview = responses["/team"].json()
    assert overview["people"][0] == {
        "user_id": b, "name": "You", "is_you": True, "conversations_opened": 0, "highlight_count": 1,
        "insight_count": 0, "question_count": 0, "reflection_count": 0,
    }
    assert _by(overview["people"], "user_id", a) == {
        "user_id": a, "name": f"Teammate {a}", "is_you": False, "conversations_opened": 0, "highlight_count": 1,
        "insight_count": 1, "question_count": 1, "reflection_count": 1,
    }
    author = {"user_id": a, "name": f"Teammate {a}", "is_you": False}
    question = {
        "research_question_id": rq, "name": f"{tag} question", "description": "why it matters", "author": author,
        "insight_count": 1, "highlight_count": 1, "conversation_count": 1, "group_id": None, "adopted_by_you": False,
    }
    assert _by(overview["questions"], "research_question_id", rq) == question
    assert overview["conversation_count"] == 201

    detail = responses[f"/team/questions/{rq}"].json()
    assert detail["question"] == question
    (insight,) = detail["insights"]
    assert (insight["connection_id"], insight["insight_text"], insight["author"]) == (
        ids[f"{tag} filed insight"], f"{tag} filed insight", author)
    assert (insight["conversation_id"], insight["voice_memo_id"]) == (9, vm["id"])
    assert insight["conversation_title"] and insight["filed_at"].endswith("+00:00")
    assert insight["evidence"] == [{
        "snippet_id": span["snippet_id"], "conversation_id": 9, "conversation_title": insight["conversation_title"],
        "text": quote, "start_index": 0, "end_index": 2,
    }]
    assert detail["highlights"] == [{
        "highlight_id": highlight["id"], "author": author, "conversation_id": 9,
        "conversation_title": insight["conversation_title"], "text": quote, "notes": f"{tag} note", "starred": True,
    }]

    person = responses[f"/team/people/{a}"].json()
    assert person["person"]["user_id"] == a and person["questions"] == [question]
    (reflection,) = person["reflections"]
    assert (reflection["voice_memo_id"], reflection["conversation_id"], reflection["typed"], reflection["text"]) == (
        vm["id"], 9, True, f"{tag} reflection")
    assert reflection["recorded_at"].endswith("+00:00")

    on_transcript = responses["/conversations/9/team_highlights"].json()
    assert mine["id"] not in [h["highlight_id"] for h in on_transcript]  # never the viewer's own
    theirs = _by(on_transcript, "highlight_id", highlight["id"])
    assert theirs["created_at"].endswith("+00:00")
    assert {k: v for k, v in theirs.items() if k != "created_at"} == {
        "highlight_id": highlight["id"], "author": author, "notes": f"{tag} note", "starred": True,
        "question_names": [f"{tag} question"], "snippets": [span], "cortico_id": None,
    }

    assert client.get(f"/team/questions/{MISSING}").status_code == 404
    assert client.get(f"/team/people/{MISSING}").status_code == 404
    assert client.get(f"/conversations/{MISSING}/team_highlights").status_code == 404
    # The teammate's own pages still refuse another user.
    assert client.get(f"/voice_memos/{vm['id']}").status_code == 404
    assert client.get(f"/research_questions/{rq}/highlights").status_code == 404

    # An archived question leaves the team page.
    act_as(a)
    assert client.patch(f"/research_questions/{rq}", json={"archived": True}).status_code == 200
    act_as(b)
    assert client.get(f"/team/questions/{rq}").status_code == 404


# --- 2. the access rule in cortico_api mode -----------------------------------------------------

WIDE, NARROW, ELSEWHERE = 9101, 9102, 9103


@pytest.fixture
def cortico(monkeypatch):
    config = get_config()
    fake = FakeCortico()
    fake.users.update({"pw-team-wide": WIDE, "pw-team-narrow": NARROW, "pw-team-elsewhere": ELSEWHERE})
    monkeypatch.setattr(config, "data_source", "cortico_api")
    monkeypatch.setattr(config, "session_secret", "test-secret")
    monkeypatch.setattr(auth_module, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def _seed_question(user_id: int, project_id: int, name: str) -> int:
    with PrototypeSession() as db:
        rq = ResearchQuestion(user_id=user_id, project_id=project_id, name=name, display_order=0)
        db.add(rq)
        db.commit()
        return rq.id


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_a_narrower_workspace_sees_nothing_made_outside_it(cortico, monkeypatch):
    """WIDE's workspace holds projects 501 and 502; NARROW's holds 501 only, so conversation
    7002 (project 502) is outside it. Everything WIDE made on 7002 carries "zz-secret"."""
    wide = _login("pw-team-wide")
    _workspace(wide)
    wide.get("/conversations")
    for cid in (7001, 7002):  # caches both transcripts: snippets 1-3 and 11-13
        assert wide.get(f"/conversations/{cid}").status_code == 200
        wide.put(f"/conversations/{cid}/reading_position")

    q_open = _seed_question(WIDE, 502, "open question")  # visible through what is filed on 7001
    q_open2 = _seed_question(WIDE, 501, "open question two")  # visible through its project
    q_closed = _seed_question(WIDE, 502, "zz-secret question")  # only 7002 work under it
    q_closed2 = _seed_question(WIDE, 502, "zz-secret question two")
    q_closed3 = _seed_question(WIDE, 502, "zz-secret question three")

    h_open = _ok(wide.post("/conversations/7001/highlights", json={"notes": "open note", "snippets": [
        {"snippet_id": 1, "start_index": 0, "end_index": 2}]}))
    assert h_open["cortico_sync"]["status"] == "synced"
    _ok(wide.put(f"/highlights/{h_open['id']}/questions", json={"research_question_ids": [q_open]}))
    h_closed = _ok(wide.post("/conversations/7002/highlights", json={"notes": "zz-secret note", "snippets": [
        {"snippet_id": 11, "start_index": 0, "end_index": 2}]}))
    _ok(wide.put(f"/highlights/{h_closed['id']}/questions", json={"research_question_ids": [q_closed, q_open]}))

    vm_open = _ok(wide.post("/conversations/7001/reflections", json={"text": "open reflection"}))
    i_open = _file(wide, vm_open["id"], _accept("open insight", q_open, [{"snippet_id": 1, "start_index": 1, "end_index": 5}]))["open insight"]
    vm_closed = _ok(wide.post("/conversations/7002/reflections", json={"text": "zz-secret reflection"}))
    closed = _file(
        wide, vm_closed["id"],
        _accept("zz-secret insight", q_closed, [{"snippet_id": 11, "start_index": 0, "end_index": 2}]),
        # An insight from 7002 filed under the question NARROW can see.
        _accept("zz-secret insight under the open question", q_open, [{"snippet_id": 11, "start_index": 1, "end_index": 5}]),
    )
    i_secret, i_secret2 = closed["zz-secret insight"], closed["zz-secret insight under the open question"]
    with PrototypeSession() as db:  # as an insight merge leaves it: evidence from another conversation
        db.add(ConnectionEvidence(connection_id=i_open, snippet_id=12, start_index=0, end_index=4, source="user"))
        db.commit()

    group = _ok(wide.post("/team/question_groups", json={"name": "shared group", "research_question_ids": [q_open, q_closed]}))
    hidden_group = _ok(wide.post("/team/question_groups", json={"name": "zz-secret group", "research_question_ids": [q_closed2]}))
    t_hidden = _ok(wide.post(f"/team/question_groups/{group['id']}/themes", json={
        "name": "zz-secret theme", "definition": "zz-secret definition", "connection_ids": [i_secret]}))
    t_mixed = _ok(wide.post(f"/team/question_groups/{group['id']}/themes", json={
        "name": "mixed theme", "definition": "from both", "connection_ids": [i_open, i_secret2]}))
    _ok(wide.patch(f"/team/themes/{t_mixed['id']}", json={"definition": "from both conversations"}))
    t_elsewhere = _ok(wide.post(f"/team/question_groups/{hidden_group['id']}/themes", json={
        "name": "zz-secret theme elsewhere", "definition": "d", "connection_ids": []}))

    elsewhere = _login("pw-team-elsewhere")  # a teammate who only ever worked on 7002
    _workspace(elsewhere)
    elsewhere.get("/conversations")
    _ok(elsewhere.post("/conversations/7002/highlights", json={"notes": "zz-secret other note", "snippets": [
        {"snippet_id": 12, "start_index": 0, "end_index": 2}]}))
    _ok(elsewhere.post("/conversations/7002/reflections", json={"text": "zz-secret other reflection"}))

    # WIDE sees all of it, so the checks below are not passing on an empty page.
    wide_group = _ok(wide.get(f"/team/question_groups/{group['id']}"))
    assert {i["connection_id"] for i in wide_group["insights"]} == {i_open, i_secret, i_secret2}
    assert {t["id"] for t in wide_group["themes"]} == {t_hidden["id"], t_mixed["id"]}
    assert wide_group["group"]["question_ids"] == [q_open, q_closed]
    wide_question = _ok(wide.get(f"/team/questions/{q_open}"))
    assert [e["snippet_id"] for e in _by(wide_question["insights"], "connection_id", i_open)["evidence"]] == [1, 12]
    assert {h["highlight_id"] for h in wide_question["highlights"]} == {h_open["id"], h_closed["id"]}
    assert _by(_ok(wide.get("/team"))["people"], "user_id", WIDE)["conversations_opened"] == 2
    assert _by(_ok(wide.get("/team"))["people"], "user_id", ELSEWHERE)["name"] == f"Test {ELSEWHERE}"
    assert _ok(wide.get(f"/team/people/{ELSEWHERE}"))["reflections"][0]["text"] == "zz-secret other reflection"
    assert "zz-secret other note" in wide.get("/conversations/7002/team_highlights").text

    narrow = _login("pw-team-narrow")
    assert _ok(narrow.get("/team"))["conversation_count"] == 0  # no workspace yet: nothing is in scope
    assert narrow.get(f"/team/questions/{q_open}").status_code == 404
    _workspace(narrow, (501,))
    narrow.get("/conversations")
    own = _ok(narrow.post("/research_questions", json={"name": "narrow's own question"}))["id"]

    # Every team read route: nothing from conversation 7002, by text or by id.
    before = len(cortico.calls)
    reads = {path: narrow.get(path) for path in (
        "/team", f"/team/questions/{q_open}", f"/team/people/{WIDE}", "/conversations/7001/team_highlights",
        f"/team/question_groups/{group['id']}", f"/team/question_groups/{group['id']}?include_dismissed=true",
        f"/team/themes/{t_mixed['id']}/history",
    )}
    assert _v2_calls(cortico, before) == []  # the cached workspace listing, never a call per conversation
    for path, r in reads.items():
        assert r.status_code == 200, (path, r.text)
        assert "zz-secret" not in r.text, path
        assert "Conversation 7002" not in r.text and '"conversation_id":7002' not in r.text, path
        assert "@example.org" not in r.text, path  # never an email

    overview = reads["/team"].json()
    assert overview["conversation_count"] == 1
    assert _by(overview["people"], "user_id", ELSEWHERE) is None
    assert overview["people"][0]["user_id"] == NARROW and overview["people"][0]["is_you"] is True
    assert _by(overview["people"], "user_id", WIDE) == {
        "user_id": WIDE, "name": f"Test {WIDE}", "is_you": False, "conversations_opened": 1, "highlight_count": 1,
        "insight_count": 1, "question_count": 2, "reflection_count": 1,
    }
    wide_questions = [q for q in overview["questions"] if q["author"]["user_id"] == WIDE]
    assert [q["research_question_id"] for q in wide_questions] == [q_open, q_open2]
    assert (wide_questions[0]["insight_count"], wide_questions[0]["highlight_count"], wide_questions[0]["conversation_count"]) == (1, 1, 1)
    assert wide_questions[0]["group_id"] == group["id"]
    assert hidden_group["id"] not in [g["id"] for g in overview["groups"]]
    assert _by(overview["groups"], "id", group["id"]) == {
        "id": group["id"], "name": "shared group", "status": "active", "rationale": None,
        "created_by": {"user_id": WIDE, "name": f"Test {WIDE}", "is_you": False},
        "question_ids": [q_open], "theme_count": 1,
    }

    detail = reads[f"/team/questions/{q_open}"].json()
    (insight,) = detail["insights"]
    assert insight["connection_id"] == i_open
    assert [(e["snippet_id"], e["conversation_id"], e["text"]) for e in insight["evidence"]] == [(1, 7001, "library knows my name")]
    assert [h["highlight_id"] for h in detail["highlights"]] == [h_open["id"]]

    person = reads[f"/team/people/{WIDE}"].json()
    assert [(m["conversation_id"], m["text"]) for m in person["reflections"]] == [(7001, "open reflection")]
    assert [q["research_question_id"] for q in person["questions"]] == [q_open, q_open2]

    on_transcript = [h for h in reads["/conversations/7001/team_highlights"].json() if h["author"]["user_id"] == WIDE]
    assert [(h["highlight_id"], h["question_names"], h["cortico_id"]) for h in on_transcript] == [
        (h_open["id"], ["open question"], h_open["cortico_sync"]["cortico_id"])]

    for path in (f"/team/question_groups/{group['id']}", f"/team/question_groups/{group['id']}?include_dismissed=true"):
        seen = reads[path].json()
        assert seen["group"]["question_ids"] == [q_open] and [q["research_question_id"] for q in seen["questions"]] == [q_open]
        assert [i["connection_id"] for i in seen["insights"]] == [i_open]
        assert [(t["id"], t["member_connection_ids"]) for t in seen["themes"]] == [(t_mixed["id"], [i_open])]
    (revision,) = reads[f"/team/themes/{t_mixed['id']}/history"].json()
    assert revision["member_connection_ids"] == [i_open] and revision["edited_by"]["user_id"] == WIDE

    # Direct ids outside the scope answer 404, like ids that do not exist.
    hidden = {
        "get": [f"/team/questions/{q_closed}", f"/team/questions/{q_closed2}", f"/team/people/{ELSEWHERE}",
                "/conversations/7002/team_highlights", f"/team/question_groups/{hidden_group['id']}",
                f"/team/themes/{t_hidden['id']}/history", f"/team/themes/{t_elsewhere['id']}/history"],
        "post": [f"/team/questions/{q_closed}/adopt", f"/team/question_groups/{hidden_group['id']}/accept",
                 f"/team/question_groups/{hidden_group['id']}/dismiss",
                 f"/team/question_groups/{hidden_group['id']}/themes/synthesize",
                 f"/team/themes/{t_hidden['id']}/accept", f"/team/themes/{t_hidden['id']}/dismiss"],
        "delete": [f"/team/question_groups/{group['id']}/questions/{q_closed}",
                   f"/team/question_groups/{hidden_group['id']}/questions/{q_closed2}"],
    }
    for method, paths in hidden.items():
        for path in paths:
            assert getattr(narrow, method)(path).status_code == 404, (method, path)
    with_body = [
        ("post", "/team/question_groups", {"research_question_ids": [own, q_closed]}),
        ("post", f"/team/question_groups/{group['id']}/questions", {"research_question_id": q_closed3}),
        ("patch", f"/team/question_groups/{hidden_group['id']}", {"name": "renamed"}),
        ("post", f"/team/question_groups/{hidden_group['id']}/questions", {"research_question_id": own}),
        ("post", f"/team/question_groups/{hidden_group['id']}/themes", {"name": "n", "definition": "d", "connection_ids": []}),
        ("patch", f"/team/themes/{t_hidden['id']}", {"name": "renamed"}),
        ("put", f"/team/themes/{t_hidden['id']}/members", {"connection_ids": []}),
    ]
    for method, path, body in with_body:
        assert getattr(narrow, method)(path, json=body).status_code == 404, (method, path)
    # An insight outside the scope is refused like one that does not exist.
    for cid in (i_secret2, MISSING):
        assert narrow.put(f"/team/themes/{t_mixed['id']}/members", json={"connection_ids": [cid]}).status_code == 422
        assert narrow.post(f"/team/question_groups/{group['id']}/themes", json={
            "name": "n", "definition": "d", "connection_ids": [cid]}).status_code == 422

    # Replacing the members NARROW can see leaves the one it cannot see in place.
    assert _ok(narrow.put(f"/team/themes/{t_mixed['id']}/members", json={"connection_ids": [i_open]}))["member_connection_ids"] == [i_open]
    assert _by(_ok(wide.get(f"/team/question_groups/{group['id']}"))["themes"], "id", t_mixed["id"])["member_connection_ids"] == sorted([i_open, i_secret2])

    # Neither paid route shows the model anything outside the scope, or saves an id from outside it.
    model = FakeModel(monkeypatch, {"groups": [
        {"name": "Same question?", "rationale": "They overlap.", "question_ids": [q_open2, q_closed3, own]}]})
    (suggested,) = _ok(narrow.post("/team/question_groups/suggest"))
    sent_ids = {q["id"] for q in model.calls[0]["payload"]["questions"]}
    assert {q_open2, own} <= sent_ids and not sent_ids & {q_open, q_closed, q_closed2, q_closed3}
    assert "zz-secret" not in json.dumps(model.calls[0]["payload"])
    assert suggested["question_ids"] == [q_open2, own] and suggested["status"] == "suggested"
    with PrototypeSession() as db:
        assert sorted(m.research_question_id for m in db.get(TeamQuestionGroup, suggested["id"]).members) == sorted([q_open2, own])

    model = FakeModel(monkeypatch, {"themes": [
        {"name": "Known by name", "definition": "People are recognised.", "connection_ids": [i_open, i_secret]}]})
    (theme,) = _ok(narrow.post(f"/team/question_groups/{group['id']}/themes/synthesize"))
    payload = model.calls[0]["payload"]
    assert [i["connection_id"] for i in payload["insights"]] == [i_open]
    assert "zz-secret" not in json.dumps(payload) and "7002" not in json.dumps(payload)
    assert [t["name"] for t in payload["existing_themes"]] == ["mixed theme"]
    assert theme["member_connection_ids"] == [i_open]
    with PrototypeSession() as db:
        assert [m.connection_id for m in db.get(TeamTheme, theme["id"]).members] == [i_open]


# --- 3. adopting a question ---------------------------------------------------------------------


def test_adopting_a_question_copies_it_once(act_as):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    theirs = _question(f"adoptable {a}", "their reasons")
    act_as(b)
    first = _question("b's first question")
    r = client.post(f"/team/questions/{theirs}/adopt")
    assert r.status_code == 200, r.text
    new = r.json()
    made_the_usual_way = client.post("/research_questions", json={"name": "b's third"}).json()
    assert set(new) == set(made_the_usual_way)  # the same shape POST /research_questions returns
    assert (new["name"], new["description"], new["display_order"], new["archived"]) == (f"adoptable {a}", "their reasons", 1, False)
    assert new["id"] not in (theirs, first) and made_the_usual_way["display_order"] == 2
    with PrototypeSession() as db:
        row = db.get(ResearchQuestion, new["id"])
        assert (row.user_id, row.project_id) == (b, get_config().default_project_id)
        (adoption,) = db.query(Adoption).filter(Adoption.user_id == b).all()
        assert (adoption.kind, adoption.source_id, adoption.source_user_id, adoption.new_id) == ("question", theirs, a, new["id"])
    assert new["id"] in [q["id"] for q in client.get("/research_questions").json()]

    assert client.post(f"/team/questions/{theirs}/adopt").status_code == 409  # already adopted
    assert client.post(f"/team/questions/{first}/adopt").status_code == 409  # already theirs
    assert client.post(f"/team/questions/{MISSING}/adopt").status_code == 404
    questions = client.get("/team").json()["questions"]
    assert _by(questions, "research_question_id", theirs)["adopted_by_you"] is True
    assert _by(questions, "research_question_id", new["id"])["adopted_by_you"] is False
    kinds = [item["kind"] for item in client.get("/activity?limit=50").json()["items"]]
    assert kinds.count("question_adopted") == 1 and kinds.count("rq_created") == 2  # the adoption is not also "created"


# --- 4. question groups -------------------------------------------------------------------------


def test_question_groups_by_hand_and_from_the_ai(act_as, monkeypatch):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    qa = _question(f"how do people find out? {a}")
    act_as(b)
    qb1, qb2, qb3, qb4 = (_question(f"group question {b} {n}", f"description {n}") for n in range(4))
    you = {"user_id": b, "name": "You", "is_you": True}

    assert client.post("/team/question_groups", json={"research_question_ids": []}).status_code == 422
    assert client.post("/team/question_groups", json={"research_question_ids": [qa, MISSING]}).status_code == 404
    group = _ok(client.post("/team/question_groups", json={"research_question_ids": [qa]}))
    assert group == {"id": group["id"], "name": f"how do people find out? {a}", "status": "active", "rationale": None,
                     "created_by": you, "question_ids": [qa], "theme_count": 0}
    assert _ok(client.post(f"/team/question_groups/{group['id']}/questions", json={"research_question_id": qb1}))["question_ids"] == [qa, qb1]
    # Adding it again changes nothing.
    assert _ok(client.post(f"/team/question_groups/{group['id']}/questions", json={"research_question_id": qb1}))["question_ids"] == [qa, qb1]

    # One question is in at most one active group.
    assert client.post("/team/question_groups", json={"research_question_ids": [qb1]}).status_code == 409
    other = _ok(client.post("/team/question_groups", json={"name": "  Another  ", "research_question_ids": [qb2]}))
    assert other["name"] == "Another"
    assert client.post(f"/team/question_groups/{other['id']}/questions", json={"research_question_id": qa}).status_code == 409
    assert client.post(f"/team/question_groups/{other['id']}/questions", json={"research_question_id": MISSING}).status_code == 404

    assert _ok(client.patch(f"/team/question_groups/{group['id']}", json={"name": "Finding out"}))["name"] == "Finding out"
    assert client.patch(f"/team/question_groups/{group['id']}", json={"name": " "}).status_code == 422
    assert client.patch(f"/team/question_groups/{MISSING}", json={"name": "x"}).status_code == 404
    overview = client.get("/team").json()
    assert _by(overview["questions"], "research_question_id", qb1)["group_id"] == group["id"]
    assert _by(overview["groups"], "id", group["id"])["question_ids"] == [qa, qb1]
    assert _ok(client.delete(f"/team/question_groups/{group['id']}/questions/{qb1}"))["question_ids"] == [qa]
    assert client.delete(f"/team/question_groups/{group['id']}/questions/{qb1}").status_code == 404
    assert _by(client.get("/team").json()["questions"], "research_question_id", qb1)["group_id"] is None

    # The AI: an invented id and a grouped question are dropped, and a group needs two left.
    model = FakeModel(monkeypatch, {"groups": [
        {"name": "Who hears about it?", "rationale": "Both ask how word spreads.", "question_ids": [qb1, MISSING, qb3, qb1]},
        {"name": "One real question", "rationale": "r", "question_ids": [qa, qb4]},  # qa is in a group already
        {"name": "", "rationale": "no name", "question_ids": [qb3, qb4]},
    ]})
    (suggested,) = _ok(client.post("/team/question_groups/suggest"))
    assert suggested == {"id": suggested["id"], "name": "Who hears about it?", "status": "suggested",
                         "rationale": "Both ask how word spreads.", "created_by": None, "question_ids": [qb1, qb3],
                         "theme_count": 0}
    call = model.calls[0]
    cfg = get_config().team
    assert (call["model"], call["effort"]) == (cfg.model, cfg.reasoning_effort) == ("claude-haiku-5-5", "low")
    sent = {q["id"]: q for q in call["payload"]["questions"]}
    assert sent[qb1] == {"id": qb1, "question": f"group question {b} 0", "description": "description 0", "asked_by": "You"}
    assert qa not in sent and qb2 not in sent and {qb3, qb4} <= set(sent)  # grouped questions are not sent
    with PrototypeSession() as db:
        row = db.get(TeamQuestionGroup, suggested["id"])
        metadata = json.loads(row.ai_metadata)
        assert (row.status, row.created_by) == ("suggested", None)
        assert metadata["model"] == cfg.model and metadata["reasoning_effort"] == "low" and metadata["prompt"] == team.QUESTION_GROUP_PROMPT
    # A suggested group does not take its questions out of play yet.
    assert _by(client.get("/team").json()["questions"], "research_question_id", qb1)["group_id"] is None
    assert suggested["id"] in [g["id"] for g in client.get("/team").json()["groups"]]

    # A pair already together in a suggested group is not suggested again.
    model.data = {"groups": [
        {"name": "Again", "rationale": "r", "question_ids": [qb3, qb1]},
        {"name": "A new pair", "rationale": "r", "question_ids": [qb3, qb4]},
    ]}
    (second,) = _ok(client.post("/team/question_groups/suggest"))
    assert (second["name"], second["question_ids"]) == ("A new pair", [qb3, qb4])

    accepted = _ok(client.post(f"/team/question_groups/{suggested['id']}/accept"))
    assert accepted["status"] == "active"
    assert _by(client.get("/team").json()["questions"], "research_question_id", qb3)["group_id"] == suggested["id"]
    # qb3 has joined an active group meanwhile.
    assert client.post(f"/team/question_groups/{second['id']}/accept").status_code == 409
    assert client.post(f"/team/question_groups/{suggested['id']}/dismiss").status_code == 409  # active, not suggested
    assert _ok(client.post(f"/team/question_groups/{second['id']}/dismiss"))["status"] == "dismissed"
    assert client.post(f"/team/question_groups/{second['id']}/accept").status_code == 409
    assert second["id"] not in [g["id"] for g in client.get("/team").json()["groups"]]
    assert client.get(f"/team/question_groups/{second['id']}").status_code == 404
    with PrototypeSession() as db:
        assert db.get(TeamQuestionGroup, second["id"]).status == "dismissed"  # a status change, never a delete

    # "Keep separate" sticks: the dismissed pair is not suggested again.
    _ok(client.delete(f"/team/question_groups/{suggested['id']}/questions/{qb3}"))
    model.data = {"groups": [{"name": "Once more", "rationale": "r", "question_ids": [qb4, qb3]}]}
    assert _ok(client.post("/team/question_groups/suggest")) == []

    items = client.get("/activity?limit=50").json()["items"]
    kinds = [item["kind"] for item in items]
    assert kinds.count("team_group_created") == 3 and kinds.count("team_groups_suggested") == 3
    assert kinds.count("team_group_dismissed") == 1
    assert {i["actor"] for i in items if i["kind"] == "team_groups_suggested"} == {"ai"}
    assert [i["kind"] for i in client.get("/activity?filter=set_aside").json()["items"]] == ["team_group_dismissed"]


# --- 5. team themes -----------------------------------------------------------------------------


def test_theme_results_drop_unknown_insights_and_repeated_names():
    with PrototypeSession() as db:
        memo = VoiceMemo(user_id=next(_user_ids), conversation_id=9, audio_path="", status="filed")
        group = TeamQuestionGroup(name="apply-step group", status="active", created_by=1)
        db.add_all([memo, group])
        db.flush()
        rows = [ReflectionConnection(voice_memo_id=memo.id, insight_text=t, source="user", status="accepted") for t in "ab"]
        db.add_all(rows)
        db.commit()
        one, two = rows[0].id, rows[1].id
        created = team._apply_theme_results(
            db,
            group_id=group.id,
            raw_themes=[
                {"name": "Known by name", "definition": "People are recognised.", "connection_ids": [one, MISSING, two, one]},
                {"name": "Nothing real", "definition": "Every id is invented.", "connection_ids": [MISSING]},
                {"name": "  known BY  name ", "definition": "The same name again.", "connection_ids": [two]},
                {"name": "Already Here", "definition": "The team has this one.", "connection_ids": [one]},
                {"name": "No definition", "definition": " ", "connection_ids": [one]},
                {"name": "Second", "definition": "d", "connection_ids": [two]},
                {"name": "Past the cap", "definition": "d", "connection_ids": [one]},
            ],
            valid_connection_ids={one, two},
            existing_names={"already  here"},
            ai_metadata={"model": "m", "reasoning_effort": "low", "prompt": "p"},
            max_new=2,
        )
        assert [(t.name, t.status, t.created_by, [m.connection_id for m in t.members]) for t in created] == [
            ("Known by name", "suggested", None, [one, two]), ("Second", "suggested", None, [two])]
        assert json.loads(created[0].ai_metadata) == {"model": "m", "reasoning_effort": "low", "prompt": "p"}
        assert db.query(TeamTheme).filter(TeamTheme.group_id == group.id).count() == 2


def test_team_themes_are_suggested_edited_with_a_revision_and_decided(act_as, monkeypatch):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    qa = _question(f"theme question {a}")
    ia = _filed_insight(qa, f"insight by {a}")
    act_as(b)
    qb = _question(f"theme question {b}")
    ib = _filed_insight(qb, f"insight by {b}")
    outside = _filed_insight(_question(f"ungrouped question {b}"), "not under the group")
    group = _ok(client.post("/team/question_groups", json={"research_question_ids": [qa, qb]}))
    themes_url = f"/team/question_groups/{group['id']}/themes"
    you = {"user_id": b, "name": "You", "is_you": True}

    empty = _ok(client.post("/team/question_groups", json={"research_question_ids": [_question(f"nothing filed {b}")]}))
    model = FakeModel(monkeypatch, {"themes": [
        {"name": "Trusted places", "definition": "Where people already go.", "connection_ids": [ia, ib, outside, MISSING]},
        {"name": "Elsewhere", "definition": "Only an insight from another question.", "connection_ids": [outside]},
    ]})
    assert _ok(client.post(f"/team/question_groups/{empty['id']}/themes/synthesize")) == [] and model.calls == []  # nothing to send

    (theme,) = _ok(client.post(f"{themes_url}/synthesize"))
    assert {k: v for k, v in theme.items() if k != "updated_at"} == {
        "id": theme["id"], "name": "Trusted places", "definition": "Where people already go.", "status": "suggested",
        "created_by": None, "updated_by": None, "member_connection_ids": sorted([ia, ib]),
    }
    call = model.calls[0]
    assert call["model"] is None and call["effort"] is None  # config.ai_review's model, no override
    assert call["payload"]["max_new_themes"] == get_config().team.max_new_themes_per_run == 3
    assert [(i["connection_id"], i["insight"], i["written_by"]) for i in call["payload"]["insights"]] == [
        (ia, f"insight by {a}", f"Teammate {a}"), (ib, f"insight by {b}", "You")]
    assert all(i["conversation"] for i in call["payload"]["insights"])
    with PrototypeSession() as db:
        metadata = json.loads(db.get(TeamTheme, theme["id"]).ai_metadata)
        assert metadata["prompt"] == team.TEAM_THEME_PROMPT and metadata["model"] == "default-model"
    # The same name is not suggested twice, and the model is told what exists.
    assert _ok(client.post(f"{themes_url}/synthesize")) == []
    assert [t["name"] for t in model.calls[1]["payload"]["existing_themes"]] == ["Trusted places"]

    # Every edit writes a revision first, with who made it.
    renamed = _ok(client.patch(f"/team/themes/{theme['id']}", json={"name": "Places people trust"}))
    assert (renamed["name"], renamed["definition"], renamed["updated_by"]) == ("Places people trust", "Where people already go.", you)
    assert renamed["updated_at"].endswith("+00:00")
    assert client.patch(f"/team/themes/{theme['id']}", json={"name": " "}).status_code == 422
    assert _ok(client.patch(f"/team/themes/{theme['id']}", json={}))["name"] == "Places people trust"  # nothing to change, no revision
    assert client.put(f"/team/themes/{theme['id']}/members", json={"connection_ids": [ia, outside]}).status_code == 422
    assert _ok(client.put(f"/team/themes/{theme['id']}/members", json={"connection_ids": [ia]}))["member_connection_ids"] == [ia]
    act_as(a)
    _ok(client.patch(f"/team/themes/{theme['id']}", json={"definition": "Rewritten by a teammate."}))
    history = _ok(client.get(f"/team/themes/{theme['id']}/history"))
    assert [(r["name"], r["definition"], r["member_connection_ids"], r["edited_by"]["user_id"]) for r in history] == [
        ("Places people trust", "Where people already go.", [ia], a),
        ("Places people trust", "Where people already go.", sorted([ia, ib]), b),
        ("Trusted places", "Where people already go.", sorted([ia, ib]), b),
    ]
    assert history[0]["edited_by"] == {"user_id": a, "name": "You", "is_you": True}
    assert all(r["created_at"].endswith("+00:00") for r in history)
    with PrototypeSession() as db:
        assert db.query(TeamThemeRevision).filter(TeamThemeRevision.theme_id == theme["id"]).count() == 3
    act_as(b)

    by_hand = _ok(client.post(themes_url, json={"name": "By hand", "definition": "Made by a person.", "connection_ids": [ib]}))
    assert (by_hand["status"], by_hand["created_by"], by_hand["member_connection_ids"]) == ("accepted", you, [ib])
    assert client.post(themes_url, json={"name": "x", "definition": "d", "connection_ids": [outside]}).status_code == 422
    assert client.post(f"/team/question_groups/{MISSING}/themes", json={"name": "x", "definition": "d"}).status_code == 404

    assert _ok(client.post(f"/team/themes/{theme['id']}/accept"))["status"] == "accepted"
    detail = _ok(client.get(f"/team/question_groups/{group['id']}"))
    assert [(t["id"], t["status"]) for t in detail["themes"]] == [(theme["id"], "accepted"), (by_hand["id"], "accepted")]
    assert detail["group"]["theme_count"] == 2
    assert [q["research_question_id"] for q in detail["questions"]] == [qa, qb]
    assert [(i["connection_id"], i["author"]["user_id"]) for i in detail["insights"]] == [(ia, a), (ib, b)]
    assert detail["insights"][0]["evidence"][0]["conversation_id"] == 9

    assert _ok(client.post(f"/team/themes/{by_hand['id']}/dismiss"))["status"] == "dismissed"
    assert [t["id"] for t in _ok(client.get(f"/team/question_groups/{group['id']}"))["themes"]] == [theme["id"]]
    with_dismissed = _ok(client.get(f"/team/question_groups/{group['id']}?include_dismissed=true"))
    assert [(t["id"], t["status"]) for t in with_dismissed["themes"]] == [(theme["id"], "accepted"), (by_hand["id"], "dismissed")]
    for method, path in (("get", f"/team/themes/{MISSING}/history"), ("post", f"/team/themes/{MISSING}/accept"),
                         ("post", f"/team/themes/{MISSING}/dismiss"), ("get", f"/team/question_groups/{MISSING}")):
        assert getattr(client, method)(path).status_code == 404

    # A filed insight that is later retracted leaves the group and its themes' members.
    assert client.post(f"/memo/connections/{ib}/retract").status_code == 200
    after = _ok(client.get(f"/team/question_groups/{group['id']}?include_dismissed=true"))
    assert [i["connection_id"] for i in after["insights"]] == [ia]
    assert _by(after["themes"], "id", by_hand["id"]) is None  # its only member is no longer visible

    items = client.get("/activity?limit=50").json()["items"]
    kinds = [item["kind"] for item in items]
    assert kinds.count("team_themes_suggested") == 2 and kinds.count("team_theme_edited") == 2
    assert kinds.count("team_theme_accepted") == 2 and kinds.count("team_theme_dismissed") == 1
    assert {i["kind"] for i in client.get("/activity?filter=changed").json()["items"]} >= {"team_theme_edited"}
