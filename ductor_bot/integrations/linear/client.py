"""Async GraphQL client for Linear."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import aiohttp

from ductor_bot.integrations.linear.config import LinearConfig
from ductor_bot.integrations.linear.models import LinearIssue, LinearIssueDetails, LinearTeam


def _expect_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"Linear response field '{field}' is not an object"
        raise TypeError(msg)
    return value


def _expect_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        msg = f"Linear response field '{field}' is not a string"
        raise TypeError(msg)
    return value


def _extract_state_name(state_obj: object) -> str:
    if not isinstance(state_obj, Mapping):
        return ""
    raw = state_obj.get("name")
    if isinstance(raw, str):
        return raw
    return ""


def _issue_from_node(node_obj: object) -> LinearIssue:
    node = _expect_mapping(node_obj, field="issue")
    return LinearIssue(
        id=_expect_str(node.get("id"), field="issue.id"),
        identifier=_expect_str(node.get("identifier"), field="issue.identifier"),
        title=_expect_str(node.get("title"), field="issue.title"),
        url=_expect_str(node.get("url"), field="issue.url"),
        state_name=_extract_state_name(node.get("state")),
    )


def _issue_details_from_node(node_obj: object) -> LinearIssueDetails:
    issue = _issue_from_node(node_obj)
    node = _expect_mapping(node_obj, field="issue")
    description = node.get("description")
    return LinearIssueDetails(
        **issue.model_dump(),
        description=description if isinstance(description, str) else "",
    )


class LinearClient:
    """Thin async wrapper around Linear GraphQL API."""

    _ENDPOINT = "https://api.linear.app/graphql"

    def __init__(self, config: LinearConfig) -> None:
        self._config = config
        timeout = aiohttp.ClientTimeout(total=20)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_token:
            headers["Authorization"] = config.api_token
        self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)

    async def close(self) -> None:
        """Close underlying HTTP session."""
        if not self._session.closed:
            await self._session.close()

    async def _graphql(
        self,
        query: str,
        *,
        variables: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if not self._config.api_token:
            msg = "Linear API token is not configured"
            raise RuntimeError(msg)

        payload: dict[str, object] = {
            "query": query,
            "variables": dict(variables or {}),
        }

        async with self._session.post(self._ENDPOINT, json=payload) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                msg = f"Linear HTTP {response.status}: {body}"
                raise RuntimeError(msg)

        if not isinstance(body, Mapping):
            msg = "Linear response is not a JSON object"
            raise TypeError(msg)

        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping) and isinstance(first.get("message"), str):
                msg = first["message"]
            else:
                msg = str(errors)
            raise RuntimeError(f"Linear GraphQL error: {msg}")

        data = body.get("data")
        if not isinstance(data, Mapping):
            msg = "Linear GraphQL response has no data object"
            raise TypeError(msg)

        return dict(data)

    async def list_teams(self) -> list[LinearTeam]:
        query = """
        query ListTeams {
          teams {
            nodes {
              id
              key
              name
            }
          }
        }
        """
        data = await self._graphql(query)
        teams_obj = _expect_mapping(data.get("teams"), field="teams")
        nodes = teams_obj.get("nodes")
        if not isinstance(nodes, list):
            return []

        teams: list[LinearTeam] = []
        for node_obj in nodes:
            node = _expect_mapping(node_obj, field="teams.nodes")
            teams.append(
                LinearTeam(
                    id=_expect_str(node.get("id"), field="team.id"),
                    key=_expect_str(node.get("key"), field="team.key"),
                    name=_expect_str(node.get("name"), field="team.name"),
                )
            )
        return teams

    async def create_issue(self, team_id: str, title: str, description: str) -> LinearIssue:
        mutation = """
        mutation CreateIssue($teamId: String!, $title: String!, $description: String!) {
          issueCreate(
            input: {
              teamId: $teamId
              title: $title
              description: $description
            }
          ) {
            issue {
              id
              identifier
              title
              url
              state {
                name
              }
            }
          }
        }
        """
        data = await self._graphql(
            mutation,
            variables={
                "teamId": team_id,
                "title": title,
                "description": description,
            },
        )
        payload = _expect_mapping(data.get("issueCreate"), field="issueCreate")
        return _issue_from_node(payload.get("issue"))

    async def list_recent_issues(self, team_id: str, limit: int = 10) -> list[LinearIssue]:
        query = """
        query ListRecentIssues($teamId: String!, $limit: Int!) {
          issues(
            filter: { team: { id: { eq: $teamId } } }
            first: $limit
            orderBy: updatedAt
          ) {
            nodes {
              id
              identifier
              title
              url
              state {
                name
              }
            }
          }
        }
        """
        data = await self._graphql(query, variables={"teamId": team_id, "limit": limit})
        issues_obj = _expect_mapping(data.get("issues"), field="issues")
        nodes = issues_obj.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [_issue_from_node(node_obj) for node_obj in nodes]

    async def get_issue(self, identifier: str) -> LinearIssueDetails | None:
        query = """
        query GetIssue($identifier: String!) {
          issue(identifier: $identifier) {
            id
            identifier
            title
            url
            description
            state {
              name
            }
          }
        }
        """
        data = await self._graphql(query, variables={"identifier": identifier})
        issue_obj = data.get("issue")
        if issue_obj is None:
            return None
        return _issue_details_from_node(issue_obj)

    async def append_issue_description(
        self,
        identifier: str,
        appendix: str,
    ) -> LinearIssueDetails:
        issue = await self.get_issue(identifier)
        if issue is None:
            msg = f"Linear issue not found: {identifier}"
            raise ValueError(msg)

        suffix = appendix.strip()
        if issue.description.strip():
            description = f"{issue.description.rstrip()}\n\n{suffix}"
        else:
            description = suffix
        return await self.update_issue_description(issue.id, description)

    async def add_comment(self, identifier: str, body: str) -> str:
        issue = await self.get_issue(identifier)
        if issue is None:
            msg = f"Linear issue not found: {identifier}"
            raise ValueError(msg)

        mutation = """
        mutation AddComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            comment {
              url
            }
          }
        }
        """
        data = await self._graphql(
            mutation,
            variables={"issueId": issue.id, "body": body},
        )
        payload = _expect_mapping(data.get("commentCreate"), field="commentCreate")
        comment_obj = payload.get("comment")
        if isinstance(comment_obj, Mapping):
            comment_url = comment_obj.get("url")
            if isinstance(comment_url, str) and comment_url:
                return comment_url
        return issue.url

    async def set_issue_state_by_name(
        self,
        identifier: str,
        state_names: Sequence[str],
    ) -> str:
        query = """
        query GetIssueForState($identifier: String!) {
          issue(identifier: $identifier) {
            id
            state {
              name
            }
            team {
              states {
                nodes {
                  id
                  name
                }
              }
            }
          }
        }
        """
        data = await self._graphql(query, variables={"identifier": identifier})
        issue_obj = data.get("issue")
        if issue_obj is None:
            msg = f"Linear issue not found: {identifier}"
            raise ValueError(msg)

        issue = _expect_mapping(issue_obj, field="issue")
        issue_id = _expect_str(issue.get("id"), field="issue.id")

        target_names = {name.casefold().strip() for name in state_names if name.strip()}
        if not target_names:
            msg = "state_names is empty"
            raise ValueError(msg)

        team_obj = _expect_mapping(issue.get("team"), field="issue.team")
        states_obj = _expect_mapping(team_obj.get("states"), field="issue.team.states")
        nodes = states_obj.get("nodes")
        if not isinstance(nodes, list):
            msg = "Linear issue team has no states"
            raise TypeError(msg)

        selected_state_id = ""
        selected_state_name = ""
        for node_obj in nodes:
            node = _expect_mapping(node_obj, field="issue.team.states.nodes")
            name = _expect_str(node.get("name"), field="issue.team.states.nodes.name")
            if name.casefold() in target_names:
                selected_state_id = _expect_str(node.get("id"), field="state.id")
                selected_state_name = name
                break

        if not selected_state_id:
            msg = f"No matching state found for {list(state_names)}"
            raise ValueError(msg)

        mutation = """
        mutation SetIssueState($issueId: String!, $stateId: String!) {
          issueUpdate(id: $issueId, input: { stateId: $stateId }) {
            issue {
              state {
                name
              }
            }
          }
        }
        """
        result = await self._graphql(
            mutation,
            variables={"issueId": issue_id, "stateId": selected_state_id},
        )
        payload = _expect_mapping(result.get("issueUpdate"), field="issueUpdate")
        issue_out = _expect_mapping(payload.get("issue"), field="issueUpdate.issue")
        state_name = _extract_state_name(issue_out.get("state"))
        return state_name or selected_state_name

    async def update_issue_description(self, issue_id: str, description: str) -> LinearIssueDetails:
        mutation = """
        mutation UpdateIssueDescription($issueId: String!, $description: String!) {
          issueUpdate(id: $issueId, input: { description: $description }) {
            issue {
              id
              identifier
              title
              url
              description
              state {
                name
              }
            }
          }
        }
        """
        data = await self._graphql(
            mutation,
            variables={"issueId": issue_id, "description": description},
        )
        payload = _expect_mapping(data.get("issueUpdate"), field="issueUpdate")
        return _issue_details_from_node(payload.get("issue"))
