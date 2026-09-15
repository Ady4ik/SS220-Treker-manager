from __future__ import annotations

import httpx


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token, transport=None):
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30, transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.is_error:
            raise GitHubError(f"GitHub HTTP {response.status_code}")
        return response.json()

    async def graphql(self, query, variables):
        data = await self.request("POST", "/graphql", json={"query": query, "variables": variables})
        if data.get("errors"):
            raise GitHubError("GitHub GraphQL: проверьте права, владельца и поля Project")
        return data["data"]

    async def ensure_labels(self, repository, labels):
        existing, page = set(), 1
        while True:
            batch = await self.request("GET", f"/repos/{repository}/labels", params={"per_page": 100, "page": page})
            existing.update(label["name"].casefold() for label in batch)
            if len(batch) < 100:
                break
            page += 1
        for label in labels:
            if label.casefold() not in existing:
                await self.request("POST", f"/repos/{repository}/labels",
                                   json={"name": label, "color": "d73a4a" if label == "bug" else "a2eeef"})

    async def find_issue(self, repository, marker):
        # REST pagination avoids the lag of GitHub search indexing.
        page = 1
        while True:
            batch = await self.request("GET", f"/repos/{repository}/issues",
                                       params={"state": "all", "per_page": 100, "page": page})
            for issue in batch:
                if "pull_request" not in issue and marker in (issue.get("body") or ""):
                    return issue
            if len(batch) < 100:
                return None
            page += 1

    async def create_issue(self, repository, title, body, labels):
        return await self.request("POST", f"/repos/{repository}/issues",
                                  json={"title": title, "body": body, "labels": labels})

    async def update_issue_body(self, repository, number, body):
        return await self.request("PATCH", f"/repos/{repository}/issues/{number}", json={"body": body})

    async def project(self, route):
        owner_type = route.get("project_owner_type", "organization")
        if owner_type not in {"user", "organization"}:
            raise ValueError("project_owner_type должен быть user или organization")
        query = """query($login:String!, $number:Int!, $after:String) {
          OWNER(login:$login) { projectV2(number:$number) {
            id fields(first:100, after:$after) {
              nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }""".replace("OWNER", owner_type)
        fields, after = [], None
        while True:
            data = await self.graphql(query, {"login": route["project_owner"],
                                               "number": route["project_number"], "after": after})
            project = (data.get(owner_type) or {}).get("projectV2")
            if not project:
                raise GitHubError("GitHub Project не найден")
            fields.extend(project["fields"]["nodes"])
            page = project["fields"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
        result = {"id": project["id"]}
        if route.get("status"):
            field = next((f for f in fields if f and f.get("name") == route.get("status_field", "Status")), None)
            option = next((o for o in (field or {}).get("options", []) if o["name"] == route["status"]), None)
            if not option:
                raise GitHubError("Поле или вариант статуса Project не найден")
            result.update(field=field["id"], option=option["id"])
        return result

    async def add_to_project(self, project, issue_node_id):
        data = await self.graphql("""mutation($project:ID!, $content:ID!) {
          addProjectV2ItemById(input:{projectId:$project,contentId:$content}) { item { id } }
        }""", {"project": project["id"], "content": issue_node_id})
        item = data["addProjectV2ItemById"]["item"]["id"]
        if "field" in project:
            await self.graphql("""mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
              updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,
                value:{singleSelectOptionId:$option}}) { projectV2Item { id } }
            }""", {"project": project["id"], "item": item, "field": project["field"], "option": project["option"]})
        return item
