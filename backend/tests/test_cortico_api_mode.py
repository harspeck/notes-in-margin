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
        self.users = {"pw-a": 9001, "pw-b": 9002}
        self.access = {}  # access token -> user id
        self.refresh = {}  # refresh token -> user id
        self.counter = 0
        self.highlights_created: list[dict] = []
        self.highlights_deleted: list[int] = []
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
            conv = {"501": 7001, "502": 7002}[q["project_id"]]
            return httpx.Response(200, json={"conversations": [self._conv(conv)], "page_info": {"count": 1, "cursor": None, "total": 1}})
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
        if path == "/v2/highlights" and request.method == "POST":
            body = json.loads(request.content)
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
    assert (sent["conversation_id"], sent["project_id"], sent["forum_id"]) == (7001, 501, 11)
    assert sent["title"] == "my name" and sent["notes"] == "known by name"
    assert sent["is_auto_highlight"] is False

    # Retrying an already-synced highlight never creates a second Cortico copy.
    again = client.post(f"/highlights/{h['id']}/cortico_sync").json()
    assert again["cortico_sync"]["cortico_id"] == 88001
    assert len(fake.highlights_created) == 1

    assert client.get("/conversations/7001/highlights").json()[0]["cortico_sync"]["status"] == "synced"

    deleted = client.delete(f"/highlights/{h['id']}").json()
    assert deleted["cortico_sync"]["status"] == "deleted"
    assert fake.highlights_deleted == [88001]


def test_non_contiguous_highlight_saves_locally_and_is_skipped(fake):
    client = _login()
    _workspace(client)
    client.get("/conversations")
    body = {"snippets": [{"snippet_id": 1, "start_index": 1, "end_index": 3}, {"snippet_id": 2, "start_index": 0, "end_index": 2}]}
    h = client.post("/conversations/7001/highlights", json=body).json()
    assert h["id"]
    assert h["cortico_sync"]["status"] == "skipped"
    assert fake.highlights_created == []


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
