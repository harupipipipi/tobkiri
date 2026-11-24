from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from core_runtime.host_contract import host_contract_value


GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:pull|issues)/(?P<number>\d+)(?:[/?#].*)?$"
)
GITHUB_REPO_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")
GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[^/\s:]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)


class GitHubClientError(RuntimeError):
    def __init__(self, message: str, code: str = "GITHUB_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitHubRef:
    owner: str
    repo: str
    number: int

    @property
    def repo_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(url: str) -> GitHubRef:
    match = GITHUB_URL_RE.match(str(url or "").strip())
    if not match:
        raise GitHubClientError("GitHub URL must look like https://github.com/owner/repo/pull/123 or /issues/123", "GITHUB_URL_INVALID")
    return GitHubRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        number=int(match.group("number")),
    )


def parse_github_repo(repo: str) -> str:
    value = str(repo or "").strip()
    if not GITHUB_REPO_RE.match(value):
        raise GitHubClientError("GitHub repository must look like owner/repo", "GITHUB_REPO_INVALID")
    return value.removesuffix(".git")


def github_repo_from_remote(remote_url: str) -> str:
    match = GITHUB_REMOTE_RE.search(str(remote_url or "").strip())
    if not match:
        raise GitHubClientError("Git remote must point at github.com/owner/repo", "GITHUB_REMOTE_INVALID")
    return parse_github_repo(f"{match.group('owner')}/{match.group('repo').removesuffix('.git')}")


class GitHubReadClient:
    """Small read-only GitHub client with token-first REST and gh CLI fallback."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or host_contract_value("github_token", provider_id="github")

    def pr(self, url: str) -> dict[str, Any]:
        ref = parse_github_url(url)
        pr = self._api(f"repos/{ref.repo_full_name}/pulls/{ref.number}")
        files = self._api(f"repos/{ref.repo_full_name}/pulls/{ref.number}/files?per_page=100")
        review_comments = self._api(f"repos/{ref.repo_full_name}/pulls/{ref.number}/comments?per_page=100")
        issue_comments = self._api(f"repos/{ref.repo_full_name}/issues/{ref.number}/comments?per_page=100")
        checks = self.checks_for_pr_ref(ref, pr)
        return {
            "url": url,
            "repo": ref.repo_full_name,
            "number": ref.number,
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "author": (pr.get("user") or {}).get("login"),
            "base": (pr.get("base") or {}).get("ref"),
            "head": (pr.get("head") or {}).get("ref"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "metadata": pr,
            "files": files if isinstance(files, list) else [],
            "review_comments": review_comments if isinstance(review_comments, list) else [],
            "comments": issue_comments if isinstance(issue_comments, list) else [],
            "checks": checks,
        }

    def issue(self, url: str) -> dict[str, Any]:
        ref = parse_github_url(url)
        issue = self._api(f"repos/{ref.repo_full_name}/issues/{ref.number}")
        comments = self._api(f"repos/{ref.repo_full_name}/issues/{ref.number}/comments?per_page=100")
        return {
            "url": url,
            "repo": ref.repo_full_name,
            "number": ref.number,
            "title": issue.get("title", ""),
            "state": issue.get("state", ""),
            "author": (issue.get("user") or {}).get("login"),
            "metadata": issue,
            "comments": comments if isinstance(comments, list) else [],
        }

    def ci_status(self, url: str) -> dict[str, Any]:
        ref = parse_github_url(url)
        pr = self._api(f"repos/{ref.repo_full_name}/pulls/{ref.number}")
        checks = self.checks_for_pr_ref(ref, pr)
        return {
            "url": url,
            "repo": ref.repo_full_name,
            "number": ref.number,
            "head_sha": (pr.get("head") or {}).get("sha"),
            "checks": checks,
        }

    def checks_for_pr_ref(self, ref: GitHubRef, pr: dict[str, Any]) -> dict[str, Any]:
        sha = str((pr.get("head") or {}).get("sha") or "").strip()
        if not sha:
            return {"check_runs": [], "statuses": [], "state": "unknown"}
        check_runs = self._api(f"repos/{ref.repo_full_name}/commits/{sha}/check-runs?per_page=100")
        statuses = self._api(f"repos/{ref.repo_full_name}/commits/{sha}/status")
        runs = check_runs.get("check_runs", []) if isinstance(check_runs, dict) else []
        combined_state = statuses.get("state", "unknown") if isinstance(statuses, dict) else "unknown"
        return {
            "head_sha": sha,
            "state": combined_state,
            "total_count": check_runs.get("total_count", len(runs)) if isinstance(check_runs, dict) else len(runs),
            "check_runs": runs,
            "statuses": statuses.get("statuses", []) if isinstance(statuses, dict) else [],
        }

    def _api(self, path: str) -> Any:
        if self.token:
            return self._api_with_token(path)
        return self._api_with_gh(path)

    def _api_with_token(self, path: str) -> Any:
        url = "https://api.github.com/" + path.lstrip("/")
        pages = []
        try:
            while url:
                request = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {self.token}",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "RumiAI-defaultspack",
                    },
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    pages.append(json.loads(response.read().decode("utf-8")))
                    url = _next_link(response.headers.get("Link", ""))
            return _merge_paginated_payloads(pages)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubClientError(f"GitHub API failed with HTTP {exc.code}: {detail}", "GITHUB_API_ERROR") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"GitHub API network error: {exc}", "GITHUB_NETWORK_ERROR") from exc

    def _api_with_gh(self, path: str) -> Any:
        gh = shutil.which("gh")
        if not gh:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN, or install/authenticate gh CLI for GitHub read workflow.", "GITHUB_TOKEN_REQUIRED")
        try:
            auth = subprocess.run([gh, "auth", "status"], text=True, capture_output=True, timeout=15)
        except Exception as exc:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN; gh CLI auth could not be checked.", "GITHUB_TOKEN_REQUIRED") from exc
        if auth.returncode != 0:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN, or run gh auth login for GitHub read workflow.", "GITHUB_TOKEN_REQUIRED")
        completed = subprocess.run(
            [gh, "api", "--paginate", "--slurp", path.lstrip("/")],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise GitHubClientError(completed.stderr.strip() or "gh api failed", "GITHUB_API_ERROR")
        try:
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubClientError("gh api returned invalid JSON", "GITHUB_API_ERROR") from exc
        if isinstance(payload, list):
            return _merge_paginated_payloads(payload)
        return payload


class GitHubWriteClient:
    """Small GitHub write client for approval-gated coding workflows."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or host_contract_value("github_token", provider_id="github")

    def resolve_pull_request_args(self, arguments: dict[str, Any], *, cwd: str | None = None) -> dict[str, Any]:
        repo = str(arguments.get("repo") or arguments.get("repository") or "").strip()
        head = str(arguments.get("head") or arguments.get("head_ref") or arguments.get("branch") or "").strip()
        base = str(arguments.get("base") or arguments.get("base_ref") or "").strip()
        title = str(arguments.get("title") or "").strip()
        body = str(arguments.get("body") or arguments.get("description") or "").strip()
        draft = arguments.get("draft", True)

        if not title:
            raise GitHubClientError("'title' is required", "INVALID_INPUT")
        if not repo and cwd:
            repo = github_repo_from_remote(_git(cwd, "config", "--get", "remote.origin.url"))
        if not head and cwd:
            head = _git(cwd, "branch", "--show-current")
        if not base and cwd:
            base = _git_default_branch(cwd) or "main"
        if not base:
            base = "main"
        if not repo:
            raise GitHubClientError("'repo' is required when no GitHub workspace remote is available", "INVALID_INPUT")
        if not head:
            raise GitHubClientError("'head' or 'branch' is required when no workspace branch is available", "INVALID_INPUT")

        return {
            "repo": parse_github_repo(repo),
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": bool(draft),
        }

    def create_pull_request(
        self,
        *,
        repo: str,
        title: str,
        body: str = "",
        head: str,
        base: str = "main",
        draft: bool = True,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        repo = parse_github_repo(repo)
        if not title.strip():
            raise GitHubClientError("'title' is required", "INVALID_INPUT")
        if not head.strip():
            raise GitHubClientError("'head' is required", "INVALID_INPUT")
        if not base.strip():
            raise GitHubClientError("'base' is required", "INVALID_INPUT")
        if self.token:
            return self._create_with_token(repo=repo, title=title, body=body, head=head, base=base, draft=draft)
        return self._create_with_gh(repo=repo, title=title, body=body, head=head, base=base, draft=draft, cwd=cwd)

    def _create_with_token(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
    ) -> dict[str, Any]:
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        try:
            created = self._api_json("POST", f"repos/{repo}/pulls", payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubClientError(f"GitHub API failed with HTTP {exc.code}: {detail}", "GITHUB_API_ERROR") from exc
        except urllib.error.URLError as exc:
            raise GitHubClientError(f"GitHub API network error: {exc}", "GITHUB_NETWORK_ERROR") from exc
        return _pr_create_result(created, repo=repo, head=head, base=base, draft=draft)

    def _create_with_gh(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
        cwd: str | None,
    ) -> dict[str, Any]:
        gh = shutil.which("gh")
        if not gh:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN, or install/authenticate gh CLI for GitHub write workflow.", "GITHUB_TOKEN_REQUIRED")
        try:
            auth = subprocess.run([gh, "auth", "status"], text=True, capture_output=True, timeout=15)
        except Exception as exc:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN; gh CLI auth could not be checked.", "GITHUB_TOKEN_REQUIRED") from exc
        if auth.returncode != 0:
            raise GitHubClientError("Set GITHUB_TOKEN or GH_TOKEN, or run gh auth login for GitHub write workflow.", "GITHUB_TOKEN_REQUIRED")
        command = [
            gh,
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ]
        if draft:
            command.append("--draft")
        completed = subprocess.run(command, text=True, capture_output=True, timeout=120, cwd=cwd or None)
        if completed.returncode != 0:
            raise GitHubClientError(completed.stderr.strip() or completed.stdout.strip() or "gh pr create failed", "GITHUB_API_ERROR")
        url = _first_github_pr_url(completed.stdout)
        number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url else None
        return {
            "url": url or completed.stdout.strip(),
            "repo": repo,
            "number": number,
            "title": title,
            "head": head,
            "base": base,
            "draft": draft,
        }

    def _api_json(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            "https://api.github.com/" + path.lstrip("/"),
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "RumiAI-defaultspack",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


def _git(cwd: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise GitHubClientError(completed.stderr.strip() or "git command failed", "GIT_ERROR")
    return completed.stdout.strip()


def _git_default_branch(cwd: str) -> str:
    try:
        symbolic = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except GitHubClientError:
        return ""
    if "/" in symbolic:
        return symbolic.split("/", 1)[1]
    return symbolic


def _pr_create_result(payload: Any, *, repo: str, head: str, base: str, draft: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GitHubClientError("GitHub API returned invalid pull request payload", "GITHUB_API_ERROR")
    return {
        "url": payload.get("html_url") or payload.get("url") or "",
        "repo": repo,
        "number": payload.get("number"),
        "title": payload.get("title") or "",
        "state": payload.get("state") or "",
        "head": ((payload.get("head") or {}).get("ref") if isinstance(payload.get("head"), dict) else None) or head,
        "base": ((payload.get("base") or {}).get("ref") if isinstance(payload.get("base"), dict) else None) or base,
        "draft": bool(payload.get("draft", draft)),
        "metadata": payload,
    }


def _first_github_pr_url(output: str) -> str:
    match = re.search(r"https://github\.com/[^\s]+/[^\s]+/pull/\d+", str(output or ""))
    return match.group(0) if match else ""


def _next_link(link_header: str) -> str:
    for part in str(link_header or "").split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        match = re.match(r"<([^>]+)>", section)
        if match:
            return match.group(1)
    return ""


def _merge_paginated_payloads(pages: list[Any]) -> Any:
    pages = [page for page in pages if page is not None]
    if not pages:
        return {}
    first = pages[0]
    if len(pages) == 1:
        return first
    if all(isinstance(page, list) for page in pages):
        merged: list[Any] = []
        for page in pages:
            merged.extend(page)
        return merged
    if all(isinstance(page, dict) for page in pages):
        list_key = _paginated_dict_list_key(pages)
        if list_key:
            merged_dict = dict(first)
            merged_items: list[Any] = []
            for page in pages:
                merged_items.extend(page.get(list_key, []))
            merged_dict[list_key] = merged_items
            if "total_count" in merged_dict:
                merged_dict["total_count"] = max(int(merged_dict.get("total_count") or 0), len(merged_items))
            return merged_dict
    return first


def _paginated_dict_list_key(pages: list[dict[str, Any]]) -> str:
    candidate_keys = ("check_runs", "statuses", "jobs", "workflow_runs")
    for key in candidate_keys:
        if all(isinstance(page.get(key), list) for page in pages):
            return key
    return ""
