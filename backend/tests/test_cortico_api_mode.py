"""cortico_api mode end to end, against a fake Cortico v2 server (httpx.MockTransport).

Nothing here touches the network. The fake mirrors the v2 shapes in CorticoSkill's
OpenAPI snapshot: /auth/* at the root, page_info.cursor pagination (`after` for projects,
`cursor` for conversations), transcript snippets with integer id plus uuid, and
inclusive word_indices on POST /v2/highlights.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.config import get_config
from app.cortico.source import SnippetRef
from app.cortico.write_back import NotRepresentable, build_highlight_snippets
from app.main import app

WORDS_1 = ["the", "library", "knows", "my", "name"]
WORDS_2 = ["before", "I", "walk", "in"]


def _words(ws):
    return [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "confidence": 0.9} for i, w in enumerate(ws)]


class FakeCortico:
    def __init__(self):
        # pw-c: the desk test's own user. pw-d, pw-e: the questions-screen tests' own users.
        self.users = {"pw-a": 9001, "pw-b": 9002, "pw-c": 9003, "pw-d": 9004, "pw-e": 9005, "pw-f": 9006}
        self.access = {}  # access token -> user id
        self.refresh = {}  # refresh token -> user id
        self.counter = 0
        self.highlights_created: list[dict] = []
        self.highlights_deleted: list[int] = []
        self.highlights_listed: list[dict] = []  # what GET /v2/highlights returns
        self.reject_source_filter = False
        self.transcript_failures = 0  # serve this many 503s before succeeding
        self.calls: list[str] = []

    def _issue(self, user_id):
        self.counter += 1
        a, r = f"acc{self.counter}", f"ref{self.counter}"
        self.access[a] = user_id
        self.refresh[r] = user_id
        return {"access_token": a, "refresh_token": r}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        self.calls.append(f"{request.method} {path}")
        if path == "/auth/login":
            body = json.loads(request.content)
            uid = self.users.get(body["password"])
            return httpx.Response(200, json=self._issue(uid)) if uid else httpx.Response(401, json={"detail": "bad"})
        if path == "/auth/refresh-tokens":
            uid = self.refresh.pop(json.loads(request.content)["refresh_token"], None)  # rotation
            return httpx.Response(200, json=self._issue(uid)) if uid else httpx.Response(401, json={"detail": "revoked"})

        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        uid = self.access.get(token)
        if uid is None:
            return httpx.Response(401, json={"detail": "expired"})
        if path == "/auth/logout":
            return httpx.Response(204)
        if path == "/v2/me":
            return httpx.Response(200, json={
                "id": uid, "email": f"u{uid}@example.org", "first_name": "Test", "last_name": str(uid),
                "organizations": [{"id": 353, "name": "American Conversation Project"}],
                "roles": [{"id": 1, "role_type": "host", "project_id": 501}, {"id": 2, "role_type": "host", "project_id": 502}],
            })
        if path == "/v2/projects":
            pages = {None: ([501, 502], 2), "2": ([503], None)}
            ids, cursor = pages[q.get("after")]
            return httpx.Response(200, json={
                "projects": [{"id": i, "name": f"Project {i}", "description": "", "organization_id": 353} for i in ids],
                "page_info": {"count": len(ids), "cursor": cursor, "total": 3},
            })
        if path == "/v2/conversations":
            # Like real Cortico: the last page still carries a cursor (the last id), and
            # this route rejects that cursor.
            if "cursor" in q:
                return httpx.Response(400, json={"detail": {"message": f'Invalid GUID "{q["cursor"]}"'}})
            conv = {"501": 7001, "502": 7002}[q["project_id"]]
            return httpx.Response(200, json={"conversations": [self._conv(conv)], "page_info": {"count": 1, "cursor": conv, "total": 1}})
        if path.startswith("/v2/conversations/"):
            parts = path.split("/")
            cid = int(parts[3])
            if cid not in (7001, 7002):
                return httpx.Response(404, json={"detail": "not found"})
            if path.endswith("/transcript"):
                if self.transcript_failures:
                    self.transcript_failures -= 1
                    return httpx.Response(503, headers={"Retry-After": "1"}, json={"detail": "busy"})
                base = 0 if cid == 7001 else 10  # snippet ids are global in Cortico
                return httpx.Response(200, json={"snippets": [
                    {"id": base + 1, "uuid": "u1", "content": " ".join(WORDS_1), "words": _words(WORDS_1), "index_in_conversation": 0,
                     "speaker_name": "Dale", "is_facilitator": False, "audio_start_offset": 0.0, "audio_end_offset": 2.0},
                    {"id": base + 2, "uuid": "u2", "content": " ".join(WORDS_2), "words": _words(WORDS_2), "index_in_conversation": 1,
                     "speaker_name": "Ruth", "is_facilitator": False, "audio_start_offset": 2.0, "audio_end_offset": 4.0},
                    {"id": base + 3, "uuid": "u3", "content": "and that matters", "words": _words(["and", "that", "matters"]),
                     "index_in_conversation": 2, "speaker_name": "Dale", "is_facilitator": False},
                ]})
            return httpx.Response(200, json=self._conv(cid))
        if path == "/v2/conversation-guides":
            return httpx.Response(200, json={"items": [
                {"id": 1, "version": {"version": 1, "sections": [{"title": "Old", "order": 0, "prompts": [{"id": 299, "content": "old", "order": 0}]}]}},
                {"id": 2, "version": {"version": 3, "sections": [
                    {"title": "Belonging", "order": 1, "prompts": [{"id": 302, "content": "Second", "order": 0}]},
                    {"title": "Intro", "order": 0, "prompts": [{"id": 301, "content": "First", "order": 0}]},
                ]}},
            ], "page_info": {"count": 2, "cursor": None, "total": 2}})
        if path == "/v2/highlights" and request.method == "GET":
            if self.reject_source_filter and "source" in q:
                return httpx.Response(400, json={"detail": {"message": "Only one filter may be provided at once."}})
            assert q["conversation_ids"] == "7001"
            rows = self.highlights_listed
            return httpx.Response(200, json={"highlights": rows, "page_info": {"count": len(rows), "cursor": None, "total": len(rows)}})
        if path == "/v2/highlights" and request.method == "POST":
            body = json.loads(request.content)
            if body.get("project_id") is not None and body.get("forum_id") is not None:
                return httpx.Response(400, json={"detail": {"message": "Only one filter may be provided at once."}})
            self.highlights_created.append(body)
            return httpx.Response(200, json={"id": 88000 + len(self.highlights_created), "title": body["title"], "is_active": True})
        if path.startswith("/v2/highlights/") and request.method == "DELETE":
            self.highlights_deleted.append(int(path.rsplit("/", 1)[1]))
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"detail": f"fake has no route {request.method} {path}"})

    @staticmethod
    def _conv(cid):
        return {"id": cid, "title": f"Conversation {cid}", "forum_id": 11, "duration": 600.0, "audio_url": None,
                "start_time": "2026-10-01T00:00:00Z", "num_participants": 4}


@pytest.fixture
def fake(monkeypatch):
    config = get_config()
    fake = FakeCortico()
    monkeypatch.setattr(config, "data_source", "cortico_api")
    monkeypatch.setattr(config, "session_secret", "test-secret")
    monkeypatch.setattr(auth_module, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def _login(password="pw-a") -> TestClient:
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": "someone@example.org", "password": password})
    assert r.status_code == 200, r.text
    assert "session_token" not in r.json()
    return client


def _workspace(client: TestClient, project_ids=(501, 502)) -> dict:
    r = client.post("/workspaces", json={"name": "Fall listening", "organization_id": 353, "project_ids": list(project_ids)})
    assert r.status_code == 200, r.text
    return r.json()


def test_bad_password_is_a_clear_401(fake):
    r = TestClient(app).post("/auth/login", json={"email": "x@example.org", "password": "nope"})
    assert r.status_code == 401
    assert "email and password" in r.json()["detail"]


def test_routes_require_sign_in(fake):
    assert TestClient(app).get("/conversations").status_code == 401
    assert TestClient(app).get("/memo").status_code == 401


def test_projects_are_filtered_to_roles_and_paged(fake):
    client = _login()
    orgs = client.get("/organizations").json()
    assert orgs == [{"id": 353, "name": "American Conversation Project", "project_count": 2}]
    projects = client.get("/organizations/353/projects").json()
    assert [p["id"] for p in projects] == [501, 502]  # 503 (page 2, no role) is filtered out
    assert sum(c == "GET /v2/projects" for c in fake.calls) == 4  # two pages, twice


def test_workspace_rejects_projects_outside_the_users_roles(fake):
    client = _login()
    r = client.post("/workspaces", json={"name": "x", "organization_id": 353, "project_ids": [501, 503]})
    assert r.status_code == 403
    assert client.get("/conversations").status_code == 409  # no workspace yet


def test_conversations_transcript_and_guide_come_from_the_api(fake):
    client = _login()
    ws = _workspace(client)
    assert client.get("/auth/me").json()["active_workspace"]["id"] == ws["id"]
    convs = client.get("/conversations").json()
    assert {c["id"] for c in convs} == {7001, 7002}
    assert {c["project_id"] for c in convs} == {501, 502}

    detail = client.get("/conversations/7001").json()
    assert detail["project_id"] == 501
    assert [s["snippet_id"] for s in detail["snippets"]] == [1, 2, 3]
    assert detail["snippets"][0]["words"][1]["word"] == "library"
    # Latest guide version, sections in order.
    assert [q["guide_question_id"] for q in detail["guide_questions"]] == [301, 302]

    # Second read within the TTL comes from the sidecar cache.
    before = fake.calls.count("GET /v2/conversations/7001/transcript")
    client.get("/conversations/7001")
    assert fake.calls.count("GET /v2/conversations/7001/transcript") == before

    assert client.get("/conversations/7999").status_code == 404


def test_highlight_is_written_back_with_inclusive_word_indices(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    body = {"title": None, "notes": "known by name", "snippets": [
        {"snippet_id": 1, "start_index": 3, "end_index": 5},
        {"snippet_id": 2, "start_index": 0, "end_index": 2},
    ]}
    h = client.post("/conversations/7001/highlights", json=body).json()
    assert h["cortico_sync"] == {"status": "synced", "cortico_id": 88001, "error": None}
    sent = fake.highlights_created[0]
    assert sent["snippets"] == [
        {"uuid": "u1", "word_indices": {"start_index": 3, "end_index": 4}},
        {"uuid": "u2", "word_indices": {"start_index": 0, "end_index": 1}},
    ]
    assert (sent["conversation_id"], sent["project_id"]) == (7001, 501)
    assert "forum_id" not in sent  # Cortico rejects project_id and forum_id together
    assert sent["title"] == "my name" and sent["notes"] == "known by name"

    # Retrying an already-synced highlight never creates a second Cortico copy.
    again = client.post(f"/highlights/{h['id']}/cortico_sync").json()
    assert again["cortico_sync"]["cortico_id"] == 88001
    assert len(fake.highlights_created) == 1

    assert client.get("/conversations/7001/highlights").json()[0]["cortico_sync"]["status"] == "synced"

    deleted = client.delete(f"/highlights/{h['id']}").json()
    assert deleted["cortico_sync"]["status"] == "deleted"
    assert fake.highlights_deleted == [88001]


def test_cortico_highlights_are_placed_on_words_and_leave_out_your_own_copies(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    mine = client.post("/conversations/7001/highlights", json={"snippets": [{"snippet_id": 1, "start_index": 0, "end_index": 2}]}).json()
    # Words sit 0.5 s apart from 0.0 in every snippet of the fake, so only the first two
    # snippets (0-2 s and 2-4 s) can be told apart by time; the third has no offsets.
    fake.highlights_listed = [
        {"id": mine["cortico_sync"]["cortico_id"], "audio_start_offset": 0.0, "audio_end_offset": 1.0, "is_active": True, "user_id": 9001},
        {"id": 501, "title": "Known by name", "description": "why it matters", "user_name": "Ruth H", "user_id": 77,
         "tool_id": "llm-auto-highlighter", "audio_start_offset": 0.5, "audio_end_offset": 1.5, "is_active": True},
        {"id": 502, "audio_start_offset": 1.0, "audio_end_offset": 1.4, "is_active": False},
        {"id": 503, "audio_start_offset": None, "audio_end_offset": None, "is_active": True},
    ]
    before = len(fake.calls)
    rows = client.get("/conversations/7001/cortico_highlights").json()
    assert [r["cortico_id"] for r in rows] == [501]
    assert rows[0]["snippets"] == [{"snippet_id": 1, "start_index": 1, "end_index": 3}]
    assert (rows[0]["author_name"], rows[0]["mine"], rows[0]["tool_id"]) == ("Ruth H", False, "llm-auto-highlighter")
    assert _v2_calls(fake, before).count("GET /v2/highlights") == 1  # one listing, not one call per highlight

    # Another user sees the copy the first user's highlight made: to them it is Cortico's.
    other = _login("pw-b")
    _workspace(other)
    other.get("/conversations")
    assert [r["cortico_id"] for r in other.get("/conversations/7001/cortico_highlights").json()] == [mine["cortico_sync"]["cortico_id"], 501]
    assert other.get("/conversations/7999/cortico_highlights").status_code == 404

    # Cortico refusing the `source` filter falls back to the conversation filter alone.
    fake.reject_source_filter = True
    assert len(client.get("/conversations/7001/cortico_highlights").json()) == 1


def test_non_contiguous_highlight_saves_locally_and_is_skipped(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    body = {"snippets": [{"snippet_id": 1, "start_index": 1, "end_index": 3}, {"snippet_id": 2, "start_index": 0, "end_index": 2}]}
    h = client.post("/conversations/7001/highlights", json=body).json()
    assert h["id"]
    assert h["cortico_sync"]["status"] == "skipped"
    assert fake.highlights_created == []
    assert client.delete(f"/highlights/{h['id']}").json()["cortico_sync"]["status"] == "not_in_cortico"


def test_expired_access_token_refreshes_once_and_rotates(fake):
    client = _login()
    _workspace(client)
    fake.access.clear()  # Cortico now rejects the stored access token
    r = client.get("/conversations")
    assert r.status_code == 200, r.text
    assert "POST /auth/refresh-tokens" in fake.calls
    assert "ref1" not in fake.refresh  # the old refresh token was consumed

    fake.access.clear()
    fake.refresh.clear()  # refresh revoked too: the user must sign in again
    r = client.get("/conversations")
    assert r.status_code == 401


def test_cortico_outage_retries_then_surfaces_503(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    fake.transcript_failures = 1  # one 503, then success: retried transparently
    assert client.get("/conversations/7002").status_code == 200

    fake.transcript_failures = 10
    from app.models_prototype import RLCorticoSnippet
    from app.db import PrototypeSession

    with PrototypeSession() as db:  # force a cache miss
        db.query(RLCorticoSnippet).filter(RLCorticoSnippet.conversation_id == 7001).delete()
        db.commit()
    r = client.get("/conversations/7001")
    assert r.status_code == 503
    assert r.json()["source"] == "cortico"


def test_users_cannot_touch_each_others_highlights(fake):
    a = _login("pw-a")
    _workspace(a)
    a.get("/conversations")
    h = a.post("/conversations/7001/highlights", json={"snippets": [{"snippet_id": 3, "start_index": 0, "end_index": 3}]}).json()

    b = _login("pw-b")
    _workspace(b)
    assert b.patch(f"/highlights/{h['id']}", json={"starred": True}).status_code == 404
    assert b.delete(f"/highlights/{h['id']}").status_code == 404
    assert b.get("/conversations/7001/highlights").json() == []


def test_voice_memo_upload_is_a_clear_501_when_hosted(fake):
    client = _login()
    r = client.post("/conversations/7001/voice_memos", data={"duration": "5"}, files={"audio": ("a.webm", b"x", "audio/webm")})
    assert r.status_code == 501
    assert "hosted backend" in r.json()["detail"]


def test_typed_reflection_works_when_hosted_and_can_be_filed(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    r = client.post("/conversations/7001/reflections", json={"text": "The library knows people by name."})
    assert r.status_code == 200, r.text
    vm = r.json()
    assert vm["typed"] is True and vm["audio_path"] is None and vm["status"] == "ready_for_review"
    assert client.post("/conversations/7999/reflections", json={"text": "nothing there"}).status_code == 404

    assert client.get(f"/voice_memos/{vm['id']}/review").json() == {"connections": [], "starred_unassigned": []}
    rq = client.post("/research_questions", json={"name": "hosted typed rq"}).json()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": rq["id"],
                           "insight_text": "known by name", "evidence": [{"snippet_id": 1, "start_index": 1, "end_index": 5}]}]}
    assert client.post(f"/voice_memos/{vm['id']}/file", json=body).status_code == 200
    entry = next(s for s in client.get("/memo").json()["sections"] if s["research_question_id"] == rq["id"])["entries"][0]
    assert entry["voice_memo_typed"] is True and entry["voice_memo_audio_path"] is None
    assert entry["conversation_title"] == "Conversation 7001"
    assert entry["evidence"][0]["text"] == "library knows my name"

    other = _login("pw-b")
    assert other.get(f"/voice_memos/{vm['id']}").status_code == 404
    assert other.get("/activity").json()["items"] == []
    assert "reflection_typed" in [a["kind"] for a in client.get("/activity").json()["items"]]


def test_desk_and_reading_state_are_scoped_to_the_user_and_workspace(fake):
    client = _login("pw-c")
    assert client.get("/desk").json() == {"keep_reading": [], "finish": [], "last_conversation": None}  # no workspace yet
    ws = _workspace(client)
    client.get("/conversations")

    assert client.put("/conversations/7999/reading_position").status_code == 404
    client.put("/conversations/7002/reading_position")
    opened = client.put("/conversations/7001/reading_position", json={"position_seconds": 30.5, "snippet_id": 2}).json()
    assert opened["position_snippet_id"] == 2

    desk = client.get("/desk").json()
    assert [c["conversation_id"] for c in desk["keep_reading"]] == [7001, 7002]
    first = desk["keep_reading"][0]
    assert (first["title"], first["project_id"], first["project_name"]) == ("Conversation 7001", 501, None)
    assert (first["start_time"], first["duration_seconds"]) == ("2026-10-01T00:00:00Z", 600.0)
    assert desk["last_conversation"]["conversation_id"] == 7001

    vm = client.post("/conversations/7002/reflections", json={"text": "typed"}).json()
    desk = client.get("/desk").json()
    assert [c["conversation_id"] for c in desk["keep_reading"]] == [7001]
    assert [(f["voice_memo_id"], f["state"]) for f in desk["finish"]] == [(vm["id"], "not_reviewed")]

    # Narrowing the workspace to project 502 hides conversation 7001 (project 501).
    assert client.patch(f"/workspaces/{ws['id']}", json={"project_ids": [502]}).status_code == 200
    desk = client.get("/desk").json()
    assert desk["keep_reading"] == [] and desk["last_conversation"]["conversation_id"] == 7002
    assert [f["conversation_id"] for f in desk["finish"]] == [7002]

    other = _login("pw-b")
    _workspace(other)
    assert other.get("/desk").json() == {"keep_reading": [], "finish": [], "last_conversation": None}


def test_logout_ends_the_session(fake):
    client = _login()
    assert client.post("/auth/logout").json() == {"signed_out": True}
    assert "POST /auth/logout" in fake.calls
    assert client.get("/workspaces").status_code == 401


def _snip(i, n, uuid=True):
    return SnippetRef(id=i, conversation_id=1, order_index=i, text="x", words=[{"word": "w"}] * n, uuid=f"u{i}" if uuid else None)


def test_payload_rules_match_cortico():
    snippets = [_snip(1, 5), _snip(2, 0), _snip(3, 4)]
    # Single word: half-open [2, 3) becomes inclusive 2..2.
    assert build_highlight_snippets([(1, 2, 3)], snippets) == [{"uuid": "u1", "word_indices": {"start_index": 2, "end_index": 2}}]
    # An empty snippet between passages is not a gap.
    assert len(build_highlight_snippets([(1, 4, 5), (3, 0, 2)], snippets)) == 2
    with pytest.raises(NotRepresentable):
        build_highlight_snippets([(1, 1, 3), (3, 0, 2)], snippets)  # first passage stops early
    with pytest.raises(NotRepresentable):
        build_highlight_snippets([(1, 0, 6)], snippets)  # past the last word
    with pytest.raises(NotRepresentable):
        build_highlight_snippets([(9, 0, 1)], snippets)  # unknown snippet
    with pytest.raises(NotRepresentable):
        build_highlight_snippets([(1, 0, 1)], [_snip(1, 5, uuid=False)])


def _v2_calls(fake, since: int) -> list[str]:
    return [c for c in fake.calls[since:] if " /v2/" in c]


def test_guide_ideas_when_hosted_read_the_cached_listing_and_fetch_each_projects_guide_once(fake):
    from app.db import PrototypeSession
    from app.models_prototype import RLCorticoGuide

    client = _login("pw-d")
    assert client.get("/research_questions/guide_ideas").json() == {"ideas": [], "projects_not_loaded": []}  # no workspace
    _workspace(client)
    client.get("/conversations")
    with PrototypeSession() as db:  # other tests may have cached a guide already
        db.query(RLCorticoGuide).delete()
        db.commit()

    before = len(fake.calls)
    body = client.get("/research_questions/guide_ideas").json()
    # One guide call per workspace project, and nothing per conversation: the conversations
    # come from the listing the sidecar already cached.
    assert _v2_calls(fake, before) == ["GET /v2/conversation-guides", "GET /v2/conversation-guides"]
    assert body["projects_not_loaded"] == []
    assert [(i["text"], i["shorthand_code"]) for i in body["ideas"]] == [("First", "Intro"), ("Second", "Belonging")]
    assert all(i["conversation_count"] == 2 and i["project_ids"] == [501, 502] for i in body["ideas"])

    # The guides are cached now: neither a second listing nor opening a conversation refetches them.
    before = len(fake.calls)
    client.post("/research_questions", json={"name": "first"})
    assert [i["text"] for i in client.get("/research_questions/guide_ideas").json()["ideas"]] == ["Second"]
    assert _v2_calls(fake, before) == []
    assert [q["guide_question_id"] for q in client.get("/conversations/7001").json()["guide_questions"]] == [301, 302]
    assert "GET /v2/conversation-guides" not in fake.calls[before:]


def test_guide_ideas_when_hosted_leave_projects_past_the_fetch_cap_unloaded(fake, monkeypatch):
    from app.db import PrototypeSession
    from app.models_prototype import RLCorticoGuide
    from app.services import questions

    client = _login("pw-d")
    _workspace(client)
    client.get("/conversations")
    with PrototypeSession() as db:
        db.query(RLCorticoGuide).delete()
        db.commit()
    monkeypatch.setattr(questions, "GUIDE_IDEAS_MAX_PROJECT_FETCHES", 1)
    before = len(fake.calls)
    body = client.get("/research_questions/guide_ideas").json()
    assert _v2_calls(fake, before) == ["GET /v2/conversation-guides"]
    assert body["projects_not_loaded"] == [502] and all(i["project_ids"] == [501] for i in body["ideas"])


def test_questions_screen_when_hosted_is_scoped_to_the_workspace_and_writes_back(fake, monkeypatch):
    from app.db import PrototypeSession
    from app.llm import StructuredResult
    from app.models_prototype import PassageSuggestion
    from app.services import find_passages as passages_svc
    from app.services import question_gaps as gaps_svc

    client = _login("pw-e")
    empty = client.get("/research_questions/breadth").json()  # no workspace yet
    assert empty["projects_in_scope"] == [] and empty["opened_conversation_ids"] == []
    _workspace(client)
    client.get("/conversations")
    rq = client.post("/research_questions", json={"name": "hosted question"}).json()["id"]
    client.get("/conversations/7001")
    client.put("/conversations/7001/reading_position")

    # Find more passages: the model's answer is checked against the transcript from the API.
    payloads = []

    def model(**kwargs):
        payloads.append(json.loads(kwargs["user"]))
        return StructuredResult(data={"passages": [
            {"snippet_id": 1, "start_index": 1, "end_index": 5, "reason": "known by name"},
            {"snippet_id": 11, "start_index": 0, "end_index": 2, "reason": "another conversation's snippet"},
            {"snippet_id": 2, "start_index": 2, "end_index": 9, "reason": "past the last word"},
        ]}, provider="anthropic", model="fake-model", reasoning_effort="low")

    monkeypatch.setattr(passages_svc, "structured_json", model)
    found = client.post(f"/research_questions/{rq}/find_passages").json()
    assert found["searched_conversation_ids"] == [7001]
    assert [s["snippet_id"] for s in payloads[0]["transcript_snippets"]] == [1, 2, 3]
    (suggestion,) = found["suggestions"]
    assert (suggestion["text"], suggestion["speaker_name"], suggestion["time_seconds"]) == ("library knows my name", "Dale", 0.5)
    assert suggestion["conversation_title"] == "Conversation 7001"

    # Accepting goes through the reader's highlight code: project id from the workspace
    # listing, and the same write-back to Cortico.
    created_before = len(fake.highlights_created)
    accepted = client.post(f"/passage_suggestions/{suggestion['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    highlight = accepted.json()["highlight"]
    assert highlight["research_question_ids"] == [rq] and highlight["cortico_sync"]["status"] == "synced"
    (sent,) = fake.highlights_created[created_before:]
    assert (sent["conversation_id"], sent["project_id"]) == (7001, 501) and "forum_id" not in sent
    assert sent["snippets"] == [{"uuid": "u1", "word_indices": {"start_index": 1, "end_index": 4}}]
    assert client.post(f"/passage_suggestions/{suggestion['id']}/accept").status_code == 409
    assert len(fake.highlights_created) == created_before + 1
    with PrototypeSession() as db:
        assert db.get(PassageSuggestion, suggestion["id"]).highlight_id == highlight["id"]

    # Breadth and the loose-highlights list read the sidecar only.
    before = len(fake.calls)
    breadth = client.get("/research_questions/breadth").json()
    loose = client.get(f"/research_questions/{rq}/highlights").json()["highlights"]
    assert _v2_calls(fake, before) == []
    assert breadth["projects_in_scope"] == [501, 502] and breadth["opened_conversation_ids"] == [7001]
    (q,) = breadth["questions"]
    assert q["highlight_only_conversation_ids"] == [7001] and q["project_ids_heard"] == [501]
    assert (q["insight_count"], q["highlight_count"]) == (0, 1)
    assert [(h["highlight_id"], h["text"], h["conversation_title"]) for h in loose] == [
        (highlight["id"], "library knows my name", "Conversation 7001")]

    # What am I missing: unopened conversations come from the cached listing, with no gists.
    def gaps_model(**kwargs):
        payloads.append(json.loads(kwargs["user"]))
        return StructuredResult(data={"observations": [
            {"kind": "perspective", "observation": "One project is not heard from yet.", "conversation_ids": [7002, 7001, 7999]},
        ]}, provider="anthropic", model="fake-model", reasoning_effort="low")

    monkeypatch.setattr(gaps_svc, "structured_json", gaps_model)
    before = len(fake.calls)
    gaps = client.post(f"/research_questions/{rq}/whats_missing").json()["gaps"]
    assert _v2_calls(fake, before) == []
    assert payloads[-1]["unopened_conversations"] == [{"conversation_id": 7002, "title": "Conversation 7002", "project_id": 502}]
    assert [c["conversation_id"] for c in payloads[-1]["conversations_heard"]] == [7001]
    assert payloads[-1]["highlight_quotes"] == [{"conversation_id": 7001, "text": "library knows my name", "note": None}]
    assert [c["conversation_id"] for c in gaps[0]["conversations"]] == [7002]  # 7001 is opened, 7999 does not exist
    (step,) = client.post(f"/question_gaps/{gaps[0]['id']}/listen_next").json()["next_steps"]
    assert (step["recommended_conversation_id"], step["recommended_conversation_title"], step["created"]) == (
        7002, "Conversation 7002", True)

    # Narrowing the workspace to project 502 takes conversation 7001 out of scope.
    ws = client.get("/workspaces").json()[0]
    assert client.patch(f"/workspaces/{ws['id']}", json={"project_ids": [502]}).status_code == 200
    narrowed = client.get("/research_questions/breadth").json()
    assert narrowed["projects_in_scope"] == [502] and narrowed["opened_conversation_ids"] == []
    assert narrowed["questions"][0]["conversation_ids"] == [] and narrowed["questions"][0]["highlight_count"] == 1

    other = _login("pw-b")
    _workspace(other)
    assert other.get(f"/research_questions/{rq}/highlights").status_code == 404
    assert other.post(f"/question_gaps/{gaps[0]['id']}/listen_next").status_code == 404


def test_recommendations_never_draw_on_conversations_outside_the_users_workspace(fake, monkeypatch):
    """Gists and the conversation cache are shared by every user, so the workspace is the
    only thing keeping one user's summaries out of another user's prompt."""
    from app.db import PrototypeSession
    from app.llm import StructuredResult
    from app.models_prototype import ConversationGist
    from app.services import next_steps

    wide = _login()  # another user lists both projects, which puts 7002 in the shared cache
    _workspace(wide)
    wide.get("/conversations")
    client = _login("pw-f")
    _workspace(client, project_ids=(501,))  # conversation 7001 only
    client.get("/conversations")
    sent = []

    def model(*, system, user, schema, name, effort=None, model=None):
        sent.append(json.loads(user))
        return StructuredResult(provider="anthropic", model="m", reasoning_effort="low", data={"suggestions": [
            {"research_question_id": None, "suggested_direction": None, "recommended_conversation_id": 7002, "rationale": "leak"},
            {"research_question_id": None, "suggested_direction": None, "recommended_conversation_id": 7001, "rationale": "fine"},
        ]})

    monkeypatch.setattr(next_steps, "structured_json", model)
    with PrototypeSession() as db:
        for cid, pid in ((7001, 501), (7002, 502)):
            db.merge(ConversationGist(conversation_id=cid, project_id=pid, summary_text=f"summary of {cid}", keywords_json="[]"))
        db.commit()
    try:
        out = client.post("/next_steps/recommend").json()
        assert [g["conversation_id"] for g in sent[0]["conversation_gists"]] == [7001]
        assert out["recommendation"]["recommended_conversation_id"] == 7001
        assert {s["recommended_conversation_id"] for s in client.get("/next_steps").json()} == {7001}
    finally:
        with PrototypeSession() as db:
            db.query(ConversationGist).filter(ConversationGist.conversation_id.in_([7001, 7002])).delete(synchronize_session=False)
            db.commit()


def test_a_snippet_from_another_conversation_cannot_be_cited_or_highlighted(fake):
    """Snippet text comes from a cache shared by every user, so a snippet id the user
    supplies must belong to the conversation it is filed on."""
    wide = _login()
    _workspace(wide)
    assert wide.get("/conversations/7002").status_code == 200  # caches conversation 7002's snippets
    narrow = _login("pw-b")
    _workspace(narrow, (501,))
    narrow.get("/conversations")
    vm = narrow.post("/conversations/7001/reflections", json={"text": "x"}).json()
    body = {"decisions": [{"connection_id": None, "action": "accept", "research_question_id": None, "insight_text": "x",
                           "evidence": [{"snippet_id": 11, "start_index": 0, "end_index": 100000}]}]}
    r = narrow.post(f"/voice_memos/{vm['id']}/file", json=body)
    assert r.status_code == 400 and "11" in r.json()["detail"]
    assert narrow.get(f"/voice_memos/{vm['id']}/review").json()["connections"] == []
    r = narrow.post("/conversations/7001/highlights", json={"snippets": [{"snippet_id": 12, "start_index": 0, "end_index": 9}]})
    assert r.status_code == 400
    assert narrow.get("/conversations/7001/highlights").json() == []


def test_a_request_that_waited_for_a_refresh_reuses_it(fake):
    """Cortico revokes a refresh token once used. A request that loaded the session before
    another request refreshed it must read the new tokens, not send the old one."""
    from datetime import datetime, timedelta, timezone

    from app import auth as auth_module
    from app.db import PrototypeSession
    from app.models_prototype import RLSession

    client = _login()
    token_hash = auth_module.hash_token(client.cookies.get(get_config().session.cookie_name))
    with PrototypeSession() as db:
        db.get(RLSession, token_hash).access_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    waiting = PrototypeSession()
    try:
        row = waiting.get(RLSession, token_hash)
        assert row.refresh_token_enc  # loaded, as current_user does, before the other request refreshes
        with PrototypeSession() as first:
            auth_module.refresh_session(first, token_hash)
        auth_module.refresh_session(waiting, token_hash)
    finally:
        waiting.close()
    assert fake.calls.count("POST /auth/refresh-tokens") == 1
    assert client.get("/auth/me").status_code == 200
