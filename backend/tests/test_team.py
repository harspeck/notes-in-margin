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
from app.llm import AIProviderError, StructuredResult
from app.main import app
from app.models_prototype import (
    Adoption,
    CodeFamily,
    ConnectionEvidence,
    HighlightQuestion,
    NextStepSuggestion,
    ReflectionConnection,
    ResearchQuestion,
    RLHighlight,
    RLSession,
    TeamAIRun,
    TeamQuestionGroup,
    TeamQuestionMember,
    TeamTheme,
    TeamThemeRevision,
    UserName,
    VoiceMemo,
)
from app.services import team
from app.services.cortico_access import clear_access_cache

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
        self.error: Exception | None = None
        self.calls: list[dict] = []
        monkeypatch.setattr(team, "structured_json", self)

    def __call__(self, *, system, user, schema, name, effort=None, model=None):
        self.calls.append({"payload": json.loads(user), "model": model, "effort": effort, "name": name})
        if self.error is not None:
            raise self.error
        return StructuredResult(data=self.data, provider="anthropic", model=model or "default-model", reasoning_effort=effort or "low")


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def _refused(r, status: int) -> str:
    """The response is the expected refusal, and its message is a sentence with no bare id."""
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and detail.endswith(".") and not any(ch.isdigit() for ch in detail), detail
    return detail


def _accept(text, rq_id, evidence=()):
    return {"connection_id": None, "action": "accept", "research_question_id": rq_id, "insight_text": text,
            "evidence": list(evidence)}


def _file(http, vm_id, *decisions) -> dict[str, int]:
    """Files the reflection and returns its connections' ids by insight text."""
    _ok(http.post(f"/voice_memos/{vm_id}/file", json={"decisions": list(decisions)}))
    with PrototypeSession() as db:
        rows = db.query(ReflectionConnection).filter(ReflectionConnection.voice_memo_id == vm_id)
        return {c.insight_text: c.id for c in rows}


def _seed(*rows) -> list[int]:
    with PrototypeSession() as db:
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def _seed_connection(vm_id: int, text: str, status: str, rq_id: int | None = None) -> int:
    return _seed(ReflectionConnection(voice_memo_id=vm_id, research_question_id=rq_id, insight_text=text, source="ai", status=status))[0]


def _question(name: str, description: str | None = None) -> int:
    return _ok(client.post("/research_questions", json={"name": name, "description": description}))["id"]


def _snippet_9() -> dict:
    return client.get("/conversations/9").json()["snippets"][0]


def _span_9() -> dict:
    return {"snippet_id": _snippet_9()["snippet_id"], "start_index": 0, "end_index": 2}


def _filed_insight(rq_id: int, text: str, reflection: str = "a reflection") -> int:
    """A typed reflection on conversation 9 with one insight filed under rq_id, as the acting user."""
    vm = _ok(client.post("/conversations/9/reflections", json={"text": reflection}))
    return _file(client, vm["id"], _accept(text, rq_id, [_span_9()]))[text]


def _shared_question(name: str, description: str | None = None) -> int:
    """A question teammates can see: one with a highlight filed under it."""
    rq = _question(name, description)
    highlight = _ok(client.post("/conversations/9/highlights", json={"snippets": [_span_9()]}))
    _ok(client.put(f"/highlights/{highlight['id']}/questions", json={"research_question_ids": [rq]}))
    return rq


def _by(rows, key, value):
    return next((row for row in rows if row[key] == value), None)


def _kinds() -> list[str]:
    return [item["kind"] for item in client.get("/activity?limit=200").json()["items"]]


# --- 1. what a teammate sees ------------------------------------------------------------------


def test_a_teammate_sees_filed_work_and_nothing_undecided_or_set_aside(act_as):
    a, b, c, d = (next(_user_ids) for _ in range(4))
    tag = f"t{a}"
    act_as(a)
    rq = _question(f"{tag} question", "why it matters")
    nothing_filed = _question(f"{tag} hidden question with nothing filed")
    snippet = _snippet_9()
    span = _span_9()
    quote = " ".join(w["word"] for w in snippet["words"][:2])
    highlight = _ok(client.post("/conversations/9/highlights", json={"notes": f"{tag} note", "snippets": [span]}))
    _ok(client.put(f"/highlights/{highlight['id']}/questions", json={"research_question_ids": [rq]}))
    _ok(client.patch(f"/highlights/{highlight['id']}", json={"starred": True}))
    vm = _ok(client.post("/conversations/9/reflections", json={"text": f"{tag} reflection"}))
    rejected = _seed_connection(vm["id"], f"{tag} hidden rejected", "suggested", rq)
    ids = _file(
        client, vm["id"], {"connection_id": rejected, "action": "reject"},
        _accept(f"{tag} filed insight", rq, [span]), _accept(f"{tag} hidden retracted", rq),
    )
    _ok(client.post(f"/memo/connections/{ids[f'{tag} hidden retracted']}/retract"))
    _seed_connection(vm["id"], f"{tag} hidden suggested", "suggested", rq)
    _seed_connection(vm["id"], f"{tag} hidden merged", "merged", rq)
    _seed(  # A's undecided AI suggestions, and a reflection that is not finished
        NextStepSuggestion(user_id=a, research_question_id=rq, suggested_direction=f"{tag} hidden next step", status="suggested"),
        CodeFamily(user_id=a, research_question_id=rq, name=f"{tag} hidden family", definition="d", status="suggested"),
        VoiceMemo(user_id=a, conversation_id=9, audio_path="a.webm", transcription_edited=f"{tag} hidden unfinished", status="transcribing"),
        VoiceMemo(user_id=a, conversation_id=9, audio_path="b.webm", transcription_edited=f"{tag} hidden failed", status="failed"),
    )
    assert f"{tag} hidden rejected" in client.get("/memo/set_aside").text  # A's own set-aside list holds it
    assert client.get(f"/team/questions/{nothing_filed}").status_code == 200  # your own question is always yours to see

    # C filed an insight and a highlight under A's question. They are C's rows, not work under A's question.
    c_memo, c_highlight = _seed(
        VoiceMemo(user_id=c, conversation_id=9, audio_path="", transcription_edited="c's reflection", status="filed"),
        RLHighlight(user_id=c, conversation_id=9, project_id=5, notes="c's note"),
    )
    (c_insight,) = _seed(ReflectionConnection(
        voice_memo_id=c_memo, research_question_id=rq, insight_text=f"{tag} hidden other owner", source="user", status="accepted"))
    _seed(HighlightQuestion(highlight_id=c_highlight, research_question_id=rq, user_id=c))
    # D only has a question, with nothing filed under it.
    act_as(d)
    d_question = _question(f"{tag} hidden question of someone with no work")

    act_as(b)
    mine = _ok(client.post("/conversations/9/highlights", json={"notes": "my own", "snippets": [span]}))
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
        "insight_count": 1, "question_count": 1, "reflection_count": 1,  # the two unfinished reflections are not counted
    }
    assert _by(overview["people"], "user_id", c)["insight_count"] == 1
    assert _by(overview["people"], "user_id", d) is None  # nothing visible, so not a person here
    author = {"user_id": a, "name": f"Teammate {a}", "is_you": False}
    question = {
        "research_question_id": rq, "name": f"{tag} question", "description": "why it matters", "author": author,
        "insight_count": 1, "highlight_count": 1, "conversation_count": 1, "group_id": None, "adopted_by_you": False,
    }
    assert _by(overview["questions"], "research_question_id", rq) == question
    assert overview["conversation_count"] == 201

    detail = responses[f"/team/questions/{rq}"].json()
    assert detail["question"] == question
    (insight,) = detail["insights"]  # not C's, which sits under a question C does not own
    assert insight["connection_id"] != c_insight
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
    assert _by(on_transcript, "highlight_id", c_highlight)["question_names"] == []

    # A teammate's question with nothing visible filed under it is not on the page.
    for path in (f"/team/questions/{nothing_filed}", f"/team/questions/{d_question}", f"/team/questions/{MISSING}",
                 f"/team/people/{d}", f"/team/people/{MISSING}", f"/conversations/{MISSING}/team_highlights"):
        _refused(client.get(path), 404)
    _refused(client.post(f"/team/questions/{nothing_filed}/adopt"), 404)
    # The teammate's own pages still refuse another user.
    assert client.get(f"/voice_memos/{vm['id']}").status_code == 404
    assert client.get(f"/research_questions/{rq}/highlights").status_code == 404

    # An archived question leaves the team page.
    act_as(a)
    _ok(client.patch(f"/research_questions/{rq}", json={"archived": True}))
    act_as(b)
    assert client.get(f"/team/questions/{rq}").status_code == 404


# --- 2. the access rule in cortico_api mode -----------------------------------------------------

WIDE, NARROW, ELSEWHERE, REVOKED, MAKER, NOBODY, LEAVER = 9101, 9102, 9103, 9104, 9105, 9106, 9107


class TeamCortico(FakeCortico):
    """The fake Cortico, with roles that can be taken away and a /v2/me that can fail."""

    def __init__(self):
        super().__init__()
        self.users.update({
            "pw-team-wide": WIDE, "pw-team-narrow": NARROW, "pw-team-elsewhere": ELSEWHERE,
            "pw-team-revoked": REVOKED, "pw-team-maker": MAKER, "pw-team-nobody": NOBODY, "pw-team-leaver": LEAVER,
        })
        self.revoked: dict[int, set[int]] = {}
        self.me_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        uid = self.access.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if request.url.path != "/v2/me" or uid is None:
            return super().handler(request)
        self.calls.append("GET /v2/me")
        if self.me_down:
            return httpx.Response(503, json={"detail": "busy"})
        projects = [pid for pid in (501, 502) if pid not in self.revoked.get(uid, set())]
        return httpx.Response(200, json={
            "id": uid, "email": f"u{uid}@example.org", "first_name": "Test", "last_name": str(uid),
            "organizations": [{"id": 353, "name": "American Conversation Project"}],
            "roles": [{"id": pid, "role_type": "host", "project_id": pid} for pid in projects],
        })


@pytest.fixture
def cortico(monkeypatch):
    config = get_config()
    fake = TeamCortico()
    monkeypatch.setattr(config, "data_source", "cortico_api")
    monkeypatch.setattr(config, "session_secret", "test-secret")
    monkeypatch.setattr(auth_module, "TRANSPORT", httpx.MockTransport(fake.handler))
    clear_access_cache()
    yield fake
    clear_access_cache()


def _hosted(password: str, project_ids=(501, 502)) -> TestClient:
    http = _login(password)
    _workspace(http, project_ids)
    http.get("/conversations")
    for cid in (7001, 7002)[: len(project_ids)]:  # caches the transcripts: snippets 1-3 and 11-13
        assert http.get(f"/conversations/{cid}").status_code == 200
    return http


def _seed_question(user_id: int, name: str) -> int:
    return _seed(ResearchQuestion(user_id=user_id, project_id=501, name=name, display_order=0))[0]


def _highlight(http, conversation_id: int, snippet_id: int, note: str, rq_ids=()) -> dict:
    h = _ok(http.post(f"/conversations/{conversation_id}/highlights", json={"notes": note, "snippets": [
        {"snippet_id": snippet_id, "start_index": 0, "end_index": 2}]}))
    if rq_ids:
        _ok(http.put(f"/highlights/{h['id']}/questions", json={"research_question_ids": list(rq_ids)}))
    return h


def test_a_narrower_workspace_sees_nothing_made_outside_it(cortico, monkeypatch):
    """WIDE's workspace holds projects 501 and 502; NARROW's holds 501 only, so conversation
    7002 (project 502) is outside it. Everything that describes WIDE's work on 7002 carries
    "zz-secret", and none of it may reach NARROW."""
    wide = _hosted("pw-team-wide")
    for cid in (7001, 7002):
        wide.put(f"/conversations/{cid}/reading_position")

    # Every question below has project_id 501, NARROW's project: a question's project shows nothing.
    q_open = _seed_question(WIDE, "open question")
    q_open2 = _seed_question(WIDE, "open question two")
    q_open3 = _seed_question(WIDE, "open question three")
    q_closed = _seed_question(WIDE, "zz-secret question")  # only 7002 work under it
    q_closed2 = _seed_question(WIDE, "zz-secret question two")
    q_closed3 = _seed_question(WIDE, "zz-secret question three")
    q_bare = _seed_question(WIDE, "zz-secret question with nothing filed")

    h_open = _highlight(wide, 7001, 1, "open note", [q_open])
    assert h_open["cortico_sync"]["status"] == "synced"
    _highlight(wide, 7001, 2, "open note two", [q_open2])
    _highlight(wide, 7001, 3, "open note three", [q_open3])
    h_closed = _highlight(wide, 7002, 11, "zz-secret note", [q_closed, q_open])
    _highlight(wide, 7002, 12, "zz-secret note two", [q_closed2])
    _highlight(wide, 7002, 13, "zz-secret note three", [q_closed3])

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
    _seed(ConnectionEvidence(  # as an insight merge leaves it: evidence from another conversation
        connection_id=i_open, snippet_id=12, start_index=0, end_index=4, source="user"))

    elsewhere = _hosted("pw-team-elsewhere")  # a teammate who only ever worked on 7002
    _highlight(elsewhere, 7002, 12, "zz-secret other note")
    _ok(elsewhere.post("/conversations/7002/reflections", json={"text": "zz-secret other reflection"}))

    # A group that reaches beyond NARROW's scope: its name was written about both questions.
    group = _ok(wide.post("/team/question_groups", json={"name": "zz-secret group name", "research_question_ids": [q_open, q_closed]}))
    hidden_group = _ok(wide.post("/team/question_groups", json={"name": "zz-secret group", "research_question_ids": [q_closed2]}))
    whole_group = _ok(wide.post("/team/question_groups", json={"name": "A name everyone may read", "research_question_ids": [q_open2]}))
    suggested_group = _seed(TeamQuestionGroup(
        name="zz-secret matched name", status="suggested", rationale="zz-secret rationale", ai_metadata="{}"))[0]
    _seed(*(TeamQuestionMember(group_id=suggested_group, research_question_id=q) for q in (q_open3, q_closed3)))

    themes_url = f"/team/question_groups/{group['id']}/themes"
    t_hidden = _ok(wide.post(themes_url, json={"name": "zz-secret theme", "definition": "zz-secret definition", "connection_ids": [i_secret]}))
    t_mixed = _ok(wide.post(themes_url, json={"name": "zz-secret mixed theme", "definition": "zz-secret, from both", "connection_ids": [i_open, i_secret2]}))
    # A theme that once held a hidden insight stays hidden: its words were written about it.
    t_once = _ok(wide.post(themes_url, json={"name": "zz-secret theme that once held more", "definition": "zz-secret", "connection_ids": [i_open, i_secret2]}))
    _ok(wide.put(f"/team/themes/{t_once['id']}/members", json={"connection_ids": [i_open]}))
    t_open = _ok(wide.post(themes_url, json={"name": "open theme draft", "definition": "d", "connection_ids": [i_open]}))
    _ok(wide.patch(f"/team/themes/{t_open['id']}", json={"name": "open theme"}))
    _ok(elsewhere.patch(f"/team/themes/{t_open['id']}", json={"definition": "about being known"}))
    t_empty = _ok(elsewhere.post(themes_url, json={"name": "a theme with nothing in it yet", "definition": "d", "connection_ids": []}))
    t_elsewhere = _ok(wide.post(f"/team/question_groups/{hidden_group['id']}/themes", json={
        "name": "zz-secret theme elsewhere", "definition": "d", "connection_ids": []}))

    # WIDE sees all of it, so the checks below are not passing on an empty page.
    wide_group = _ok(wide.get(f"/team/question_groups/{group['id']}"))
    assert {i["connection_id"] for i in wide_group["insights"]} == {i_open, i_secret, i_secret2}
    assert {t["id"] for t in wide_group["themes"]} == {t_hidden["id"], t_mixed["id"], t_once["id"], t_open["id"], t_empty["id"]}
    assert (wide_group["group"]["name"], wide_group["group"]["question_ids"]) == ("zz-secret group name", [q_open, q_closed])
    assert _by(wide_group["themes"], "id", t_empty["id"])["created_by"] == {"user_id": ELSEWHERE, "name": f"Test {ELSEWHERE}", "is_you": False}
    assert [r["edited_by"]["user_id"] for r in _ok(wide.get(f"/team/themes/{t_open['id']}/history"))] == [ELSEWHERE, WIDE]
    wide_question = _ok(wide.get(f"/team/questions/{q_open}"))
    assert [e["snippet_id"] for e in _by(wide_question["insights"], "connection_id", i_open)["evidence"]] == [1, 12]
    assert {h["highlight_id"] for h in wide_question["highlights"]} == {h_open["id"], h_closed["id"]}
    wide_overview = _ok(wide.get("/team"))
    assert _by(wide_overview["people"], "user_id", WIDE)["conversations_opened"] == 2
    assert _by(wide_overview["people"], "user_id", ELSEWHERE)["name"] == f"Test {ELSEWHERE}"
    assert _by(wide_overview["groups"], "id", suggested_group)["rationale"] == "zz-secret rationale"
    assert _by(wide_overview["questions"], "research_question_id", q_bare) is not None  # WIDE's own
    assert _ok(wide.get(f"/team/people/{ELSEWHERE}"))["reflections"][0]["text"] == "zz-secret other reflection"
    assert "zz-secret other note" in wide.get("/conversations/7002/team_highlights").text

    narrow = _login("pw-team-narrow")
    assert _ok(narrow.get("/team"))["conversation_count"] == 0  # no workspace yet: nothing is in scope
    _refused(narrow.get(f"/team/questions/{q_open}"), 404)
    _workspace(narrow, (501,))
    narrow.get("/conversations")
    own = _ok(narrow.post("/research_questions", json={"name": "narrow's own question"}))["id"]
    narrow.get("/team")  # asks Cortico once which projects this account can reach

    # Every team read route: nothing from conversation 7002, by text or by id.
    before = len(cortico.calls)
    reads = {path: narrow.get(path) for path in (
        "/team", f"/team/questions/{q_open}", f"/team/people/{WIDE}", "/conversations/7001/team_highlights",
        f"/team/question_groups/{group['id']}", f"/team/question_groups/{group['id']}?include_dismissed=true",
        f"/team/question_groups/{suggested_group}", f"/team/themes/{t_open['id']}/history",
    )}
    assert _v2_calls(cortico, before) == []  # the cached listing and the cached access check: no call per conversation
    for path, r in reads.items():
        assert r.status_code == 200, (path, r.text)
        assert "zz-secret" not in r.text, path
        assert "Conversation 7002" not in r.text and '"conversation_id":7002' not in r.text, path
        assert "@example.org" not in r.text, path  # never an email
        assert f"Test {ELSEWHERE}" not in r.text and f'"user_id":{ELSEWHERE}' not in r.text, path

    overview = reads["/team"].json()
    assert overview["conversation_count"] == 1
    assert _by(overview["people"], "user_id", ELSEWHERE) is None
    assert overview["people"][0]["user_id"] == NARROW and overview["people"][0]["is_you"] is True
    assert _by(overview["people"], "user_id", WIDE) == {
        "user_id": WIDE, "name": f"Test {WIDE}", "is_you": False, "conversations_opened": 1, "highlight_count": 3,
        "insight_count": 1, "question_count": 3, "reflection_count": 1,
    }
    wide_questions = [q for q in overview["questions"] if q["author"]["user_id"] == WIDE]
    assert [q["research_question_id"] for q in wide_questions] == [q_open, q_open2, q_open3]  # not q_bare, in NARROW's own project
    assert (wide_questions[0]["insight_count"], wide_questions[0]["highlight_count"], wide_questions[0]["conversation_count"]) == (1, 1, 1)
    assert wide_questions[0]["group_id"] == group["id"]
    assert _by(overview["questions"], "research_question_id", own)["author"]["is_you"] is True  # nothing filed, still theirs
    assert hidden_group["id"] not in [g["id"] for g in overview["groups"]]
    wide_author = {"user_id": WIDE, "name": f"Test {WIDE}", "is_you": False}
    # A group with a hidden question shows the first visible question's name and no rationale.
    assert _by(overview["groups"], "id", group["id"]) == {
        "id": group["id"], "name": "open question", "status": "active", "rationale": None,
        "created_by": wide_author, "question_ids": [q_open], "theme_count": 2,
    }
    assert _by(overview["groups"], "id", suggested_group) == {
        "id": suggested_group, "name": "open question three", "status": "suggested", "rationale": None,
        "created_by": None, "question_ids": [q_open3], "theme_count": 0,
    }
    assert _by(overview["groups"], "id", whole_group["id"])["name"] == "A name everyone may read"

    detail = reads[f"/team/questions/{q_open}"].json()
    (insight,) = detail["insights"]
    assert insight["connection_id"] == i_open
    assert [(e["snippet_id"], e["conversation_id"], e["text"]) for e in insight["evidence"]] == [(1, 7001, "library knows my name")]
    assert [h["highlight_id"] for h in detail["highlights"]] == [h_open["id"]]

    person = reads[f"/team/people/{WIDE}"].json()
    assert [(m["conversation_id"], m["text"]) for m in person["reflections"]] == [(7001, "open reflection")]
    assert [q["research_question_id"] for q in person["questions"]] == [q_open, q_open2, q_open3]

    on_transcript = [h for h in reads["/conversations/7001/team_highlights"].json() if h["author"]["user_id"] == WIDE]
    assert (on_transcript[0]["highlight_id"], on_transcript[0]["question_names"], on_transcript[0]["cortico_id"]) == (
        h_open["id"], ["open question"], h_open["cortico_sync"]["cortico_id"])

    # A theme is shown only when every member is visible; who made it only when they are a person here.
    for path in (f"/team/question_groups/{group['id']}", f"/team/question_groups/{group['id']}?include_dismissed=true"):
        seen = reads[path].json()
        assert seen["group"]["question_ids"] == [q_open] and [q["research_question_id"] for q in seen["questions"]] == [q_open]
        assert [i["connection_id"] for i in seen["insights"]] == [i_open]
        assert [(t["id"], t["name"], t["member_connection_ids"]) for t in seen["themes"]] == [
            (t_open["id"], "open theme", [i_open]), (t_empty["id"], "a theme with nothing in it yet", [])]
        assert (seen["themes"][0]["created_by"], seen["themes"][0]["updated_by"]) == (wide_author, None)  # ELSEWHERE edited it last
        assert (seen["themes"][1]["created_by"], seen["themes"][1]["updated_by"]) == (None, None)
    assert [(r["name"], r["definition"], r["member_connection_ids"], r["edited_by"]) for r in reads[f"/team/themes/{t_open['id']}/history"].json()] == [
        ("open theme", "d", [i_open], None), ("open theme draft", "d", [i_open], wide_author)]

    # Direct ids outside the scope answer 404, like ids that do not exist, in a plain sentence.
    hidden = {
        "get": [f"/team/questions/{q_closed}", f"/team/questions/{q_closed2}", f"/team/questions/{q_bare}",
                f"/team/people/{ELSEWHERE}", "/conversations/7002/team_highlights",
                f"/team/question_groups/{hidden_group['id']}", f"/team/themes/{t_hidden['id']}/history",
                f"/team/themes/{t_mixed['id']}/history", f"/team/themes/{t_once['id']}/history",
                f"/team/themes/{t_elsewhere['id']}/history"],
        "post": [f"/team/questions/{q_closed}/adopt", f"/team/questions/{q_bare}/adopt",
                 f"/team/question_groups/{hidden_group['id']}/accept", f"/team/question_groups/{hidden_group['id']}/dismiss",
                 f"/team/question_groups/{hidden_group['id']}/themes/synthesize",
                 f"/team/themes/{t_hidden['id']}/accept", f"/team/themes/{t_mixed['id']}/accept",
                 f"/team/themes/{t_once['id']}/accept",
                 f"/team/themes/{t_mixed['id']}/dismiss"],
        "delete": [f"/team/question_groups/{group['id']}/questions/{q_closed}",
                   f"/team/question_groups/{hidden_group['id']}/questions/{q_closed2}",
                   f"/team/themes/{t_mixed['id']}/members/{i_open}"],
    }
    for method, paths in hidden.items():
        for path in paths:
            _refused(getattr(narrow, method)(path), 404)
    with_body = [
        ("post", "/team/question_groups", {"research_question_ids": [own, q_closed]}),
        ("post", "/team/question_groups", {"research_question_ids": [q_bare]}),
        ("post", f"/team/question_groups/{whole_group['id']}/questions", {"research_question_id": q_closed3}),
        ("patch", f"/team/question_groups/{hidden_group['id']}", {"name": "renamed"}),
        ("post", f"/team/question_groups/{hidden_group['id']}/questions", {"research_question_id": own}),
        ("post", f"/team/question_groups/{hidden_group['id']}/themes", {"name": "n", "definition": "d", "connection_ids": []}),
        ("patch", f"/team/themes/{t_hidden['id']}", {"name": "renamed"}),
        ("patch", f"/team/themes/{t_mixed['id']}", {"name": "renamed"}),
        ("patch", f"/team/themes/{t_once['id']}", {"name": "renamed"}),
        ("put", f"/team/themes/{t_mixed['id']}/members", {"connection_ids": [i_open]}),
        ("post", f"/team/themes/{t_mixed['id']}/members", {"connection_id": i_open}),
    ]
    for method, path, body in with_body:
        _refused(getattr(narrow, method)(path, json=body), 404)
    # An insight outside the scope is refused like one that does not exist.
    for cid in (i_secret2, MISSING):
        _refused(narrow.put(f"/team/themes/{t_open['id']}/members", json={"connection_ids": [cid]}), 422)
        _refused(narrow.post(f"/team/themes/{t_open['id']}/members", json={"connection_id": cid}), 422)
        _refused(narrow.delete(f"/team/themes/{t_open['id']}/members/{cid}"), 422)
        _refused(narrow.post(themes_url, json={"name": "n", "definition": "d", "connection_ids": [cid]}), 422)

    # NARROW cannot overwrite a name it cannot read, and a refusal never names what it cannot see.
    assert "zz-secret" not in _refused(narrow.patch(f"/team/question_groups/{group['id']}", json={"name": "renamed"}), 409)
    _ok(wide.post(f"/team/question_groups/{hidden_group['id']}/questions", json={"research_question_id": q_closed3}))
    assert _refused(narrow.post(f"/team/question_groups/{suggested_group}/accept"), 409) == "One of these questions is already in another group."
    _ok(wide.delete(f"/team/question_groups/{hidden_group['id']}/questions/{q_closed3}"))
    _ok(wide.post(f"/team/question_groups/{whole_group['id']}/questions", json={"research_question_id": q_open3}))
    assert _refused(narrow.post(f"/team/question_groups/{suggested_group}/accept"), 409) == "“open question three” is already in another group."
    assert _refused(wide.post("/team/question_groups", json={"research_question_ids": [q_open3]}), 409).startswith("“open question three”")
    _ok(wide.delete(f"/team/question_groups/{whole_group['id']}/questions/{q_open3}"))

    # Neither paid route shows the model anything outside the scope, or saves an id from outside it.
    model = FakeModel(monkeypatch, {"groups": [
        {"name": "Same question?", "rationale": "They overlap.", "question_ids": [q_open2, q_closed, q_open3, own]}]})
    _ok(wide.delete(f"/team/question_groups/{whole_group['id']}/questions/{q_open2}"))
    (suggested,) = _ok(narrow.post("/team/question_groups/suggest"))
    sent = {q["id"]: q for q in model.calls[0]["payload"]["questions"]}
    # q_open is in an active group, q_open3 waits in a suggested one, and the rest are hidden.
    assert {q_open2, own} <= set(sent) and not set(sent) & {q_open, q_open3, q_closed, q_closed2, q_closed3, q_bare}
    assert (sent[q_open2]["asked_by"], sent[own]["asked_by"]) == (f"Test {WIDE}", f"Test {NARROW}")
    assert "zz-secret" not in json.dumps(model.calls[0]["payload"])
    assert (suggested["question_ids"], suggested["status"], suggested["name"]) == ([q_open2, own], "suggested", "Same question?")
    with PrototypeSession() as db:
        assert sorted(m.research_question_id for m in db.get(TeamQuestionGroup, suggested["id"]).members) == sorted([q_open2, own])

    model = FakeModel(monkeypatch, {"themes": [
        {"name": "Known by name", "definition": "People are recognised.", "connection_ids": [i_open, i_secret]}]})
    (theme,) = _ok(narrow.post(f"{themes_url}/synthesize"))
    payload = model.calls[0]["payload"]
    assert [i["connection_id"] for i in payload["insights"]] == [i_open]
    assert "zz-secret" not in json.dumps(payload) and "7002" not in json.dumps(payload)
    assert [t["name"] for t in payload["existing_themes"]] == ["open theme", "a theme with nothing in it yet"]
    assert payload["questions"] == ["open question"]
    assert theme["member_connection_ids"] == [i_open]
    with PrototypeSession() as db:
        assert [m.connection_id for m in db.get(TeamTheme, theme["id"]).members] == [i_open]


def _maker_work(maker) -> dict:
    """A teammate's question with a highlight on each conversation, a filed insight, a group and a theme."""
    rq = _seed_question(MAKER, "the maker's question")
    on_7001 = _highlight(maker, 7001, 1, "maker's note on 7001", [rq])
    on_7002 = _highlight(maker, 7002, 11, "maker's note on 7002", [rq])
    vm = _ok(maker.post("/conversations/7002/reflections", json={"text": "the maker's reflection"}))
    insight = _file(maker, vm["id"], _accept("the maker's insight", rq, [{"snippet_id": 11, "start_index": 0, "end_index": 2}]))["the maker's insight"]
    group = _ok(maker.post("/team/question_groups", json={"research_question_ids": [rq]}))
    theme = _ok(maker.post(f"/team/question_groups/{group['id']}/themes", json={"name": "the maker's theme", "definition": "d", "connection_ids": [insight]}))
    return {"rq": rq, "on_7001": on_7001["id"], "on_7002": on_7002["id"], "insight": insight, "group": group["id"], "theme": theme["id"]}


def test_the_scope_follows_what_the_cortico_account_can_reach_now(cortico):
    """A workspace's projects were checked when it was saved. Roles change afterwards."""
    made = _maker_work(_hosted("pw-team-maker"))
    viewer = _hosted("pw-team-revoked")

    def seen() -> tuple:
        question = viewer.get(f"/team/questions/{made['rq']}")
        return (
            _ok(viewer.get("/team"))["conversation_count"],
            sorted(h["highlight_id"] for h in question.json()["highlights"]) if question.status_code == 200 else None,
            viewer.get("/conversations/7002/team_highlights").status_code,
            viewer.get(f"/team/themes/{made['theme']}/history").status_code,
        )

    before = len(cortico.calls)
    everything = (2, sorted([made["on_7001"], made["on_7002"]]), 200, 200)
    assert seen() == everything and seen() == everything
    # Eight team requests, one lookup: who the account is and its projects (two pages).
    assert _v2_calls(cortico, before) == ["GET /v2/me", "GET /v2/projects", "GET /v2/projects"]

    cortico.revoked[REVOKED] = {502}
    assert seen() == everything  # within the window, the last answer is reused
    clear_access_cache()  # the window ends
    assert seen() == (1, [made["on_7001"]], 404, 404)
    assert "7002" not in viewer.get(f"/team/questions/{made['rq']}").text and "the maker's insight" not in viewer.get(f"/team/questions/{made['rq']}").text
    assert _ok(viewer.get(f"/team/question_groups/{made['group']}"))["themes"] == []

    # Cortico not answering fails the request. It never falls back to the unchecked workspace.
    clear_access_cache()
    cortico.me_down = True
    r = viewer.get("/team")
    assert r.status_code == 503 and r.json()["source"] == "cortico"
    assert viewer.get(f"/team/questions/{made['rq']}").status_code == 503
    cortico.me_down = False
    cortico.revoked.clear()
    assert seen() == everything  # the failure was not remembered


def test_a_signed_in_user_with_no_workspace_can_change_nothing(cortico, monkeypatch):
    made = _maker_work(_hosted("pw-team-maker"))
    maker_second = _seed_question(MAKER, "the maker's second question")
    suggested = _seed(TeamQuestionGroup(name="matched", status="suggested", ai_metadata="{}"))[0]
    _seed(*(TeamQuestionMember(group_id=suggested, research_question_id=q) for q in (made["rq"], maker_second)))

    nobody = _login("pw-team-nobody")
    own = _ok(nobody.post("/research_questions", json={"name": "a question with no workspace"}))["id"]
    model = FakeModel(monkeypatch, {"groups": [], "themes": []})
    overview = _ok(nobody.get("/team"))
    assert (overview["conversation_count"], overview["questions"], overview["groups"]) == (0, [], [])
    assert [p["user_id"] for p in overview["people"]] == [NOBODY]

    group, theme = made["group"], made["theme"]
    writes = [
        ("post", f"/team/questions/{made['rq']}/adopt", None),
        ("post", "/team/question_groups", {"research_question_ids": [own]}),
        ("post", "/team/question_groups", {"research_question_ids": [made["rq"]]}),
        ("patch", f"/team/question_groups/{group}", {"name": "renamed"}),
        ("post", f"/team/question_groups/{group}/questions", {"research_question_id": own}),
        ("delete", f"/team/question_groups/{group}/questions/{made['rq']}", None),
        ("post", f"/team/question_groups/{suggested}/accept", None),
        ("post", f"/team/question_groups/{suggested}/dismiss", None),
        ("post", f"/team/question_groups/{group}/themes/synthesize", None),
        ("post", f"/team/question_groups/{group}/themes", {"name": "n", "definition": "d", "connection_ids": []}),
        ("patch", f"/team/themes/{theme}", {"name": "renamed"}),
        ("put", f"/team/themes/{theme}/members", {"connection_ids": []}),
        ("post", f"/team/themes/{theme}/members", {"connection_id": made["insight"]}),
        ("delete", f"/team/themes/{theme}/members/{made['insight']}", None),
        ("post", f"/team/themes/{theme}/accept", None),
        ("post", f"/team/themes/{theme}/dismiss", None),
    ]
    for method, path, body in writes:
        r = getattr(nobody, method)(path, json=body) if body is not None else getattr(nobody, method)(path)
        _refused(r, 404)
    # The one write with no id to refuse: it has nothing to send, so it asks nobody.
    assert _ok(nobody.post("/team/question_groups/suggest")) == [] and model.calls == []
    with PrototypeSession() as db:
        assert (db.get(TeamTheme, theme).name, db.get(TeamQuestionGroup, suggested).status) == ("the maker's theme", "suggested")
        assert db.query(Adoption).filter(Adoption.user_id == NOBODY).count() == 0


def test_a_teammates_name_outlives_their_session(cortico):
    leaver = _hosted("pw-team-leaver", (501,))
    _highlight(leaver, 7001, 2, "left behind")
    viewer = _hosted("pw-team-maker")
    assert _by(_ok(viewer.get("/team"))["people"], "user_id", LEAVER)["name"] == f"Test {LEAVER}"
    _ok(leaver.post("/auth/logout"))
    with PrototypeSession() as db:
        db.query(RLSession).filter(RLSession.user_id == LEAVER).delete()
        db.commit()
        assert db.get(UserName, LEAVER).display_name == f"Test {LEAVER}"
    assert _by(_ok(viewer.get("/team"))["people"], "user_id", LEAVER)["name"] == f"Test {LEAVER}"
    with PrototypeSession() as db:  # with neither a session nor a remembered name, a stand-in
        db.query(UserName).filter(UserName.user_id == LEAVER).delete()
        db.commit()
    assert _by(_ok(viewer.get("/team"))["people"], "user_id", LEAVER)["name"] == f"Teammate {LEAVER}"


# --- 3. adopting a question ---------------------------------------------------------------------


def test_adopting_a_question_copies_it_once(act_as):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    theirs = _shared_question(f"adoptable {a}", "their reasons")
    act_as(b)
    first = _question("b's first question")
    new = _ok(client.post(f"/team/questions/{theirs}/adopt"))
    made_the_usual_way = _ok(client.post("/research_questions", json={"name": "b's third"}))
    assert set(new) == set(made_the_usual_way)  # the same shape POST /research_questions returns
    assert (new["name"], new["description"], new["display_order"], new["archived"]) == (f"adoptable {a}", "their reasons", 1, False)
    assert new["id"] not in (theirs, first) and made_the_usual_way["display_order"] == 2
    with PrototypeSession() as db:
        row = db.get(ResearchQuestion, new["id"])
        assert (row.user_id, row.project_id) == (b, get_config().default_project_id)
        (adoption,) = db.query(Adoption).filter(Adoption.user_id == b).all()
        assert (adoption.kind, adoption.source_id, adoption.source_user_id, adoption.new_id) == ("question", theirs, a, new["id"])
    assert new["id"] in [q["id"] for q in client.get("/research_questions").json()]

    _refused(client.post(f"/team/questions/{theirs}/adopt"), 409)  # already adopted
    _refused(client.post(f"/team/questions/{first}/adopt"), 409)  # already theirs
    _refused(client.post(f"/team/questions/{MISSING}/adopt"), 404)
    questions = client.get("/team").json()["questions"]
    assert _by(questions, "research_question_id", theirs)["adopted_by_you"] is True
    assert _by(questions, "research_question_id", new["id"])["adopted_by_you"] is False
    assert _kinds().count("question_adopted") == 1 and _kinds().count("rq_created") == 2  # the adoption is not also "created"


# --- 4. question groups -------------------------------------------------------------------------


def _group_runs() -> list[TeamAIRun]:
    with PrototypeSession() as db:
        return db.query(TeamAIRun).filter(TeamAIRun.kind == "groups").order_by(TeamAIRun.id).all()


def test_question_groups_by_hand_and_from_the_ai(act_as, monkeypatch):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    qa = _shared_question("How do people find out?")
    act_as(b)
    q = [_question(f"Group question {'abcdefghij'[n]}", f"description {'abcdefghij'[n]}") for n in range(10)]
    you = {"user_id": b, "name": "You", "is_you": True}
    groups_url = "/team/question_groups"

    assert client.post(groups_url, json={"research_question_ids": []}).status_code == 422
    _refused(client.post(groups_url, json={"research_question_ids": [qa, MISSING]}), 404)
    group = _ok(client.post(groups_url, json={"research_question_ids": [qa]}))
    assert group == {"id": group["id"], "name": "How do people find out?", "status": "active", "rationale": None,
                     "created_by": you, "question_ids": [qa], "theme_count": 0}
    with PrototypeSession() as db:
        assert db.get(TeamQuestionGroup, group["id"]).name == ""  # a question's name is never stored as the group's
    assert _ok(client.post(f"{groups_url}/{group['id']}/questions", json={"research_question_id": q[0]}))["question_ids"] == [qa, q[0]]
    # Adding it again changes nothing.
    assert _ok(client.post(f"{groups_url}/{group['id']}/questions", json={"research_question_id": q[0]}))["question_ids"] == [qa, q[0]]

    # One question is in at most one active group, and the refusal says which, by name.
    assert _refused(client.post(groups_url, json={"research_question_ids": [q[0]]}), 409) == "“Group question a” is already in another group."
    other = _ok(client.post(groups_url, json={"name": "  Another  ", "research_question_ids": [q[1]]}))
    assert other["name"] == "Another"
    _refused(client.post(f"{groups_url}/{other['id']}/questions", json={"research_question_id": qa}), 409)
    _refused(client.post(f"{groups_url}/{other['id']}/questions", json={"research_question_id": MISSING}), 404)

    assert _ok(client.patch(f"{groups_url}/{group['id']}", json={"name": "Finding out"}))["name"] == "Finding out"
    assert _ok(client.patch(f"{groups_url}/{group['id']}", json={"name": "Finding out"}))["name"] == "Finding out"  # no change, no log
    _refused(client.patch(f"{groups_url}/{group['id']}", json={"name": " "}), 422)
    _refused(client.patch(f"{groups_url}/{MISSING}", json={"name": "x"}), 404)
    overview = client.get("/team").json()
    assert _by(overview["questions"], "research_question_id", q[0])["group_id"] == group["id"]
    assert _by(overview["groups"], "id", group["id"])["question_ids"] == [qa, q[0]]
    assert _ok(client.delete(f"{groups_url}/{group['id']}/questions/{q[0]}"))["question_ids"] == [qa]
    _refused(client.delete(f"{groups_url}/{group['id']}/questions/{q[0]}"), 404)
    assert _by(client.get("/team").json()["questions"], "research_question_id", q[0])["group_id"] is None

    # The AI: an invented id and a grouped question are dropped, and a group needs two left.
    runs_before = len(_group_runs())
    model = FakeModel(monkeypatch, {"groups": [
        {"name": "Who hears about it?", "rationale": "Both ask how word spreads.", "question_ids": [q[0], MISSING, q[2], q[0]]},
        {"name": "One real question", "rationale": "r", "question_ids": [qa, q[3]]},  # qa is in a group already
        {"name": "", "rationale": "no name", "question_ids": [q[2], q[3]]},
    ]})
    (suggested,) = _ok(client.post(f"{groups_url}/suggest"))
    assert suggested == {"id": suggested["id"], "name": "Who hears about it?", "status": "suggested",
                         "rationale": "Both ask how word spreads.", "created_by": None, "question_ids": [q[0], q[2]],
                         "theme_count": 0}
    call = model.calls[0]
    cfg = get_config().team
    assert (call["model"], call["effort"]) == (cfg.model, cfg.reasoning_effort) == ("claude-haiku-5-5", "low")
    sent = {row["id"]: row for row in call["payload"]["questions"]}
    # The asker is named as anyone would read it, so the same questions make the same request whoever asks.
    assert sent[q[0]] == {"id": q[0], "question": "Group question a", "description": "description a", "asked_by": f"Teammate {b}"}
    assert qa not in sent and q[1] not in sent and set(q[2:]) <= set(sent)  # grouped questions are not sent
    with PrototypeSession() as db:
        row = db.get(TeamQuestionGroup, suggested["id"])
        metadata = json.loads(row.ai_metadata)
        assert (row.status, row.created_by) == ("suggested", None)
        assert metadata["model"] == cfg.model and metadata["reasoning_effort"] == "low" and metadata["prompt"] == team.QUESTION_GROUP_PROMPT
    (run,) = _group_runs()[runs_before:]
    assert (run.status, run.user_id, run.group_id, len(run.fingerprint)) == ("done", b, None, 64)
    # A suggested group does not take its questions out of play yet, and cannot be changed before it is decided.
    assert _by(client.get("/team").json()["questions"], "research_question_id", q[0])["group_id"] is None
    assert suggested["id"] in [g["id"] for g in client.get("/team").json()["groups"]]
    _refused(client.post(f"{groups_url}/{suggested['id']}/questions", json={"research_question_id": q[3]}), 409)
    _refused(client.delete(f"{groups_url}/{suggested['id']}/questions/{q[2]}"), 409)

    # Questions waiting in a suggested group are not sent again.
    model.data = {"groups": [
        {"name": "Again", "rationale": "r", "question_ids": [q[2], q[0]]},
        {"name": "A new pair", "rationale": "r", "question_ids": [q[3], q[4]]},
        {"name": "A pair to keep apart", "rationale": "r", "question_ids": [q[5], q[6]]},
        {"name": "A pair that goes stale", "rationale": "r", "question_ids": [q[7], q[8]]},
    ]}
    second, apart, stale = _ok(client.post(f"{groups_url}/suggest"))
    assert not {q[0], q[2]} & {row["id"] for row in model.calls[1]["payload"]["questions"]}
    assert (second["name"], second["question_ids"], apart["question_ids"]) == ("A new pair", [q[3], q[4]], [q[5], q[6]])

    # Asking again with nothing changed since the last run costs nothing.
    model.data = {"groups": []}
    assert _ok(client.post(f"{groups_url}/suggest")) == [] and len(model.calls) == 3  # one question (q[9]) and others' are left
    assert _ok(client.post(f"{groups_url}/suggest")) == [] and len(model.calls) == 3
    # A failed call is marked, answers as an AI error, and is tried again next time.
    extra = _question("Group question k")
    model.error = AIProviderError("Claude is busy right now. Try again in a minute.", 503)
    r = client.post(f"{groups_url}/suggest")
    assert (r.status_code, r.json()["source"]) == (503, "ai")
    assert [run.status for run in _group_runs()[runs_before:]] == ["done", "done", "done", "failed"]
    model.error = None
    assert _ok(client.post(f"{groups_url}/suggest")) == [] and len(model.calls) == 5
    assert _group_runs()[-1].status == "done"

    assert _ok(client.post(f"{groups_url}/{suggested['id']}/accept"))["status"] == "active"
    assert _by(client.get("/team").json()["questions"], "research_question_id", q[2])["group_id"] == suggested["id"]
    _refused(client.post(f"{groups_url}/{suggested['id']}/dismiss"), 409)  # active, not suggested
    # q[3] joins another active group meanwhile, so the suggestion that holds it cannot be accepted.
    _ok(client.post(f"{groups_url}/{other['id']}/questions", json={"research_question_id": q[3]}))
    assert _refused(client.post(f"{groups_url}/{second['id']}/accept"), 409) == "“Group question d” is already in another group."
    # Fewer than two of its questions are still open.
    _ok(client.patch(f"/research_questions/{q[8]}", json={"archived": True}))
    assert "Fewer than two" in _refused(client.post(f"{groups_url}/{stale['id']}/accept"), 409)
    with PrototypeSession() as db:
        assert db.get(TeamQuestionGroup, stale["id"]).status == "suggested"

    assert _ok(client.post(f"{groups_url}/{apart['id']}/dismiss"))["status"] == "dismissed"
    _refused(client.post(f"{groups_url}/{apart['id']}/accept"), 409)
    assert apart["id"] not in [g["id"] for g in client.get("/team").json()["groups"]]
    _refused(client.get(f"{groups_url}/{apart['id']}"), 404)
    with PrototypeSession() as db:
        assert db.get(TeamQuestionGroup, apart["id"]).status == "dismissed"  # a status change, never a delete
    # "Keep separate" sticks: the pair is sent again, and offering it again is dropped.
    model.data = {"groups": [{"name": "Once more", "rationale": "r", "question_ids": [q[6], q[5], MISSING]}]}
    assert _ok(client.post(f"{groups_url}/suggest")) == [] and len(model.calls) == 6
    assert {q[5], q[6], extra} <= {row["id"] for row in model.calls[5]["payload"]["questions"]}

    kinds = _kinds()
    assert kinds.count("team_group_created") == 3 and kinds.count("team_groups_suggested") == 5
    assert (kinds.count("team_group_dismissed"), kinds.count("team_group_renamed")) == (1, 1)
    assert [i["kind"] for i in client.get("/activity?filter=set_aside").json()["items"]] == ["team_group_dismissed"]
    assert "team_group_renamed" in [i["kind"] for i in client.get("/activity?filter=changed").json()["items"]]


def test_a_merged_question_sits_in_one_group_the_lowest(act_as):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    first, second = _question("first wording"), _question("second wording")
    i_first, i_second = _filed_insight(first, "filed under the first"), _filed_insight(second, "filed under the second")
    act_as(b)
    mine, spare = _question("b's question"), _question("b's spare question")
    g_low = _ok(client.post("/team/question_groups", json={"name": "low", "research_question_ids": [first]}))
    g_high = _ok(client.post("/team/question_groups", json={"name": "high", "research_question_ids": [second, mine]}))
    g_spare = _ok(client.post("/team/question_groups", json={"name": "spare", "research_question_ids": [spare]}))
    assert g_low["id"] < g_high["id"]

    def state() -> dict:
        overview = _ok(client.get("/team"))
        low, high = (_ok(client.get(f"/team/question_groups/{g['id']}")) for g in (g_low, g_high))
        return {
            "group_of": {qid: (_by(overview["questions"], "research_question_id", qid) or {}).get("group_id", "gone") for qid in (first, second)},
            "listed": {g["name"]: g["question_ids"] for g in overview["groups"] if g["id"] in (g_low["id"], g_high["id"])},
            "low": (low["group"]["question_ids"], [i["connection_id"] for i in low["insights"]]),
            "high": (high["group"]["question_ids"], [i["connection_id"] for i in high["insights"]]),
        }

    assert state() == {
        "group_of": {first: g_low["id"], second: g_high["id"]},
        "listed": {"low": [first], "high": [second, mine]},
        "low": ([first], [i_first]), "high": ([second, mine], [i_second]),
    }

    # A merges the first question into the second, which now sits in both groups: the lowest holds it.
    act_as(a)
    merge = _ok(client.post(f"/research_questions/{first}/merge_into", json={"target_id": second}))["merge"]
    act_as(b)
    assert state() == {
        "group_of": {first: "gone", second: g_low["id"]},
        "listed": {"low": [second], "high": [mine]},
        "low": ([second], [i_first, i_second]), "high": ([mine], []),
    }
    _refused(client.get(f"/team/questions/{first}"), 404)
    assert [i["connection_id"] for i in _ok(client.get(f"/team/questions/{second}"))["insights"]] == [i_first, i_second]
    # It is held, so it cannot join a third group, and it leaves the one that holds it, not the other.
    _refused(client.post(f"/team/question_groups/{g_spare['id']}/questions", json={"research_question_id": second}), 409)
    _refused(client.post("/team/question_groups", json={"research_question_ids": [second]}), 409)
    _refused(client.delete(f"/team/question_groups/{g_high['id']}/questions/{second}"), 404)

    act_as(a)
    _ok(client.post(f"/rq_merges/{merge['id']}/undo"))
    act_as(b)
    assert state()["group_of"] == {first: g_low["id"], second: g_high["id"]}
    assert state()["high"] == ([second, mine], [i_second])


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


def _revisions(theme_id: int) -> int:
    with PrototypeSession() as db:
        return db.query(TeamThemeRevision).filter(TeamThemeRevision.theme_id == theme_id).count()


def test_team_themes_are_suggested_edited_with_a_revision_and_decided(act_as, monkeypatch):
    a, b = next(_user_ids), next(_user_ids)
    act_as(a)
    qa = _question("theme question of a")
    ia = _filed_insight(qa, "insight by a")
    act_as(b)
    qb = _question("theme question of b")
    ib = _filed_insight(qb, "insight by b")
    outside = _filed_insight(_question("ungrouped question of b"), "not under the group")
    group = _ok(client.post("/team/question_groups", json={"research_question_ids": [qa, qb]}))
    themes_url = f"/team/question_groups/{group['id']}/themes"
    you = {"user_id": b, "name": "You", "is_you": True}

    empty = _ok(client.post("/team/question_groups", json={"research_question_ids": [_question("nothing filed")]}))
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
        (ia, "insight by a", f"Teammate {a}"), (ib, "insight by b", f"Teammate {b}")]
    assert all(i["conversation"] for i in call["payload"]["insights"])
    with PrototypeSession() as db:
        metadata = json.loads(db.get(TeamTheme, theme["id"]).ai_metadata)
        assert metadata["prompt"] == team.TEAM_THEME_PROMPT and metadata["model"] == "default-model"
        (run,) = db.query(TeamAIRun).filter(TeamAIRun.kind == "themes", TeamAIRun.group_id == group["id"]).all()
        assert (run.status, run.user_id) == ("done", b)
    # The model is told what exists, and the same name is not suggested twice.
    assert _ok(client.post(f"{themes_url}/synthesize")) == []
    assert [t["name"] for t in model.calls[1]["payload"]["existing_themes"]] == ["Trusted places"]
    # Nothing changed since: no call, for the asker or a teammate who sees the same.
    assert _ok(client.post(f"{themes_url}/synthesize")) == [] and len(model.calls) == 2
    act_as(a)
    assert _ok(client.post(f"{themes_url}/synthesize")) == [] and len(model.calls) == 2
    act_as(b)
    # At the cap of undecided suggestions, no call either, whatever changed.
    cfg = get_config().team
    assert cfg.max_suggested_themes == 6
    monkeypatch.setattr(cfg, "max_suggested_themes", 1)
    later = _filed_insight(qb, "a later insight by b")
    assert _ok(client.post(f"{themes_url}/synthesize")) == [] and len(model.calls) == 2
    monkeypatch.setattr(cfg, "max_suggested_themes", 2)  # room for one more, so the model is asked for one
    model.data = {"themes": [{"name": "One", "definition": "d", "connection_ids": [later]}, {"name": "Two", "definition": "d", "connection_ids": [later]}]}
    (one,) = _ok(client.post(f"{themes_url}/synthesize"))
    assert one["name"] == "One" and model.calls[2]["payload"]["max_new_themes"] == 1
    _ok(client.post(f"/team/themes/{one['id']}/dismiss"))
    monkeypatch.setattr(cfg, "max_suggested_themes", 6)

    # Every edit writes a revision first, with who made it.
    renamed = _ok(client.patch(f"/team/themes/{theme['id']}", json={"name": "Places people trust"}))
    assert (renamed["name"], renamed["definition"], renamed["updated_by"]) == ("Places people trust", "Where people already go.", you)
    assert renamed["updated_at"].endswith("+00:00")
    _refused(client.patch(f"/team/themes/{theme['id']}", json={"name": " "}), 422)
    assert _ok(client.patch(f"/team/themes/{theme['id']}", json={}))["name"] == "Places people trust"  # nothing to change, no revision
    _refused(client.put(f"/team/themes/{theme['id']}/members", json={"connection_ids": [ia, outside]}), 422)
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
    assert _revisions(theme["id"]) == 3
    act_as(b)

    # One insight at a time, so two people editing cannot undo each other. A repeat changes nothing.
    members_url = f"/team/themes/{theme['id']}/members"
    assert _ok(client.post(members_url, json={"connection_id": ib}))["member_connection_ids"] == sorted([ia, ib])
    assert _ok(client.post(members_url, json={"connection_id": ib}))["member_connection_ids"] == sorted([ia, ib])
    assert _revisions(theme["id"]) == 4
    act_as(a)  # a teammate's change lands beside it, not over it
    after_remove = _ok(client.delete(f"{members_url}/{ia}"))
    assert (after_remove["member_connection_ids"], after_remove["updated_by"]["user_id"]) == ([ib], a)
    assert _ok(client.delete(f"{members_url}/{ia}"))["member_connection_ids"] == [ib]
    assert _revisions(theme["id"]) == 5
    act_as(b)
    _refused(client.post(members_url, json={"connection_id": outside}), 422)
    _refused(client.delete(f"{members_url}/{outside}"), 422)
    _refused(client.post(f"/team/themes/{MISSING}/members", json={"connection_id": ia}), 404)
    _refused(client.delete(f"/team/themes/{MISSING}/members/{ia}"), 404)
    assert _ok(client.get(f"/team/themes/{theme['id']}/history"))[0]["member_connection_ids"] == sorted([ia, ib])
    assert _revisions(theme["id"]) == 5

    by_hand = _ok(client.post(themes_url, json={"name": "By hand", "definition": "Made by a person.", "connection_ids": [ib]}))
    assert (by_hand["status"], by_hand["created_by"], by_hand["member_connection_ids"]) == ("accepted", you, [ib])
    _refused(client.post(themes_url, json={"name": "x", "definition": "d", "connection_ids": [outside]}), 422)
    _refused(client.post(f"/team/question_groups/{MISSING}/themes", json={"name": "x", "definition": "d"}), 404)

    assert _ok(client.post(f"/team/themes/{theme['id']}/accept"))["status"] == "accepted"
    detail = _ok(client.get(f"/team/question_groups/{group['id']}"))
    assert [(t["id"], t["status"]) for t in detail["themes"]] == [(theme["id"], "accepted"), (by_hand["id"], "accepted")]
    assert detail["group"]["theme_count"] == 2
    assert [row["research_question_id"] for row in detail["questions"]] == [qa, qb]
    assert [(i["connection_id"], i["author"]["user_id"]) for i in detail["insights"]] == [(ia, a), (ib, b), (later, b)]
    assert detail["insights"][0]["evidence"][0]["conversation_id"] == 9

    assert _ok(client.post(f"/team/themes/{by_hand['id']}/dismiss"))["status"] == "dismissed"
    assert [t["id"] for t in _ok(client.get(f"/team/question_groups/{group['id']}"))["themes"]] == [theme["id"]]
    with_dismissed = _ok(client.get(f"/team/question_groups/{group['id']}?include_dismissed=true"))
    assert [(t["id"], t["status"]) for t in with_dismissed["themes"]] == [
        (theme["id"], "accepted"), (one["id"], "dismissed"), (by_hand["id"], "dismissed")]
    for method, path in (("get", f"/team/themes/{MISSING}/history"), ("post", f"/team/themes/{MISSING}/accept"),
                         ("post", f"/team/themes/{MISSING}/dismiss"), ("get", f"/team/question_groups/{MISSING}")):
        _refused(getattr(client, method)(path), 404)

    # A filed insight that is later retracted leaves the group and the member lists. Its themes
    # stay, because it is on a conversation everyone here can read.
    _ok(client.post(f"/memo/connections/{ib}/retract"))
    after = _ok(client.get(f"/team/question_groups/{group['id']}?include_dismissed=true"))
    assert [i["connection_id"] for i in after["insights"]] == [ia, later]
    assert [(t["id"], t["member_connection_ids"]) for t in after["themes"]] == [(theme["id"], []), (one["id"], [later]), (by_hand["id"], [])]
    assert _ok(client.delete(f"{members_url}/{ib}"))["member_connection_ids"] == [] and _revisions(theme["id"]) == 6

    kinds = _kinds()
    assert kinds.count("team_themes_suggested") == 3 and kinds.count("team_theme_edited") == 4  # b's own; a's two edits are in a's log
    assert kinds.count("team_theme_accepted") == 2 and kinds.count("team_theme_dismissed") == 2
    assert {i["kind"] for i in client.get("/activity?filter=changed").json()["items"]} >= {"team_theme_edited"}
