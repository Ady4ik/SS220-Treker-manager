from __future__ import annotations

import httpx


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str):
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )

    async def close(self):
        await self.client.aclose()

    async def create_issue(self, repository: str, title: str, body: str, labels: list[str]) -> dict:
        response = await self.client.post(f"/repos/{repository}/issues", json={"title": title, "body": body, "labels": labels})
        if response.is_error:
            raise GitHubError(f"GitHub issue creation failed ({response.status_code}): {response.text}")
        return response.json()

    async def add_to_project(self, project_owner: str, project_number: int, issue_node_id: str) -> str:
        query = """mutation($project:ID!, $content:ID!) { addProjectV2ItemById(input:{projectId:$project,contentId:$content}) { item { id } } }"""
        project_query = """query($login:String!, $number:Int!) { user(login:$login) { projectV2(number:$number) { id } } organization(login:$login) { projectV2(number:$number) { id } } }"""
        p = await self.client.post("/graphql", json={"query": project_query, "variables": {"login": project_owner, "number": project_number}})
        if p.is_error or p.json().get("errors"): raise GitHubError(f"Project lookup failed: {p.text}")
        data = p.json()["data"]
        owner = data.get("user") or data.get("organization")\n        if not owner or not owner.get("projectV2"):\n            raise GitHubError(f"GitHub Project {project_owner}/{project_number} was not found")\n        project_id = owner["projectV2"]["id"]
        r = await self.client.post("/graphql", json={"query": query, "variables": {"project": project_id, "content": issue_node_id}})
        if r.is_error or r.json().get("errors"): raise GitHubError(f"Project item creation failed: {r.text}")
        return r.json()["data"]["addProjectV2ItemById"]["item"]["id"]

