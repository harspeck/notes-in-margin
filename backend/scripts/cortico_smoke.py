"""Live check of the Cortico pipes through this app's API, with a real Cortico account.

Run it yourself; the password is read with getpass and goes only to the API you point at:

    .venv/bin/python scripts/cortico_smoke.py --api https://<deployment>.vercel.app --org 353
    .venv/bin/python scripts/cortico_smoke.py --api http://localhost:8000 --org 353 --write-highlight

Steps: sign in, list organizations and projects, save a workspace from the first project
(or --projects), list its conversations, load one transcript and its guide. With
--write-highlight it also creates a one-word highlight, confirms it reached Cortico, then
deletes it again (from this app and from Cortico).
"""
from __future__ import annotations

import argparse
import getpass
import sys

import httpx


def step(name: str, response: httpx.Response) -> dict | list:
    ok = response.status_code < 400
    print(f"{'ok ' if ok else 'ERR'} {response.status_code} {name}")
    if not ok:
        print(f"    {response.text[:300]}")
        sys.exit(1)
    return response.json()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--org", type=int, required=True, help="Cortico organization id, e.g. 353")
    p.add_argument("--projects", type=int, nargs="*", help="project ids for the workspace (default: first one)")
    p.add_argument("--email")
    p.add_argument("--write-highlight", action="store_true")
    a = p.parse_args()

    email = a.email or input("Cortico email: ")
    password = getpass.getpass("Cortico password: ")
    with httpx.Client(base_url=a.api.rstrip("/"), timeout=60) as c:
        me = step("sign in", c.post("/auth/login", json={"email": email, "password": password}))
        print(f"    signed in as Cortico user {me['user']['id']}")
        orgs = step("organizations", c.get("/organizations"))
        for o in orgs:
            print(f"    {o['id']}: {o['name']} ({o['project_count']} projects)")
        projects = step(f"projects in {a.org}", c.get(f"/organizations/{a.org}/projects"))
        for proj in projects[:15]:
            print(f"    {proj['id']}: {proj['name']}")
        if not projects:
            sys.exit("No projects in that organization are open to this account.")
        chosen = a.projects or [projects[0]["id"]]
        ws = step("save workspace", c.post("/workspaces", json={"name": "Smoke test", "organization_id": a.org, "project_ids": chosen}))
        convs = step("conversations", c.get("/conversations"))
        print(f"    {len(convs)} conversations in workspace {ws['id']}")
        if not convs:
            sys.exit("The workspace has no conversations to read.")
        conv = next((x for x in convs if x.get("duration_seconds")), convs[0])
        detail = step(f"transcript of {conv['id']}", c.get(f"/conversations/{conv['id']}"))
        print(f"    {len(detail['snippets'])} snippets, {len(detail['guide_questions'])} guide questions")

        if a.write_highlight:
            snip = next((s for s in detail["snippets"] if len(s["words"]) >= 3), None)
            if snip is None:
                sys.exit("No snippet with words to highlight.")
            h = step("create highlight", c.post(
                f"/conversations/{conv['id']}/highlights",
                json={"title": "Notes in the Margin smoke test", "notes": "Created and deleted by cortico_smoke.py",
                      "snippets": [{"snippet_id": snip["snippet_id"], "start_index": 0, "end_index": 1}]},
            ))
            print(f"    cortico_sync: {h['cortico_sync']}")
            d = step("delete highlight", c.delete(f"/highlights/{h['id']}"))
            print(f"    cortico_sync: {d['cortico_sync']}")

        step("archive smoke-test workspace", c.patch(f"/workspaces/{ws['id']}", json={"archived": True}))
        step("sign out", c.post("/auth/logout"))


if __name__ == "__main__":
    main()
