#!/usr/bin/env python3
"""Assemble and run an ARC-Bench competition submission locally."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
DEFAULT_DATA_ROOT = REPOSITORY_ROOT / "data" / "competition"
DEFAULT_RUNS_ROOT = SCRIPT_DIR / "runs"
DEFAULT_REQUIREMENTS_DIR = SCRIPT_DIR / "public-exercise" / "requirements"
DEFAULT_IMAGE = "arcbench-local-submit:latest"
DEFAULT_METER_BASE_URL = "https://meter.arc-bench.com"
METER_USAGE_WINDOW_SECONDS = 30 * 24 * 60 * 60
METER_TIMEOUT_SECONDS = 10.0
METER_SETTLE_TIMEOUT_SECONDS = 15.0
EXPECTED_COST_PER_PASS_RATE_POINT = Decimal("1.2")
REWARD_EXPONENT = Decimal("0.1")
PENALTY_EXPONENT = Decimal("0.2")
EXCLUDED_BASELINE_PARTS = {".arc", ".git", "requirements", "node_modules", ".cache", "dist", "build"}
PASSTHROUGH_ENVIRONMENT = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "MODEL",
    "VISUAL_API_KEY",
    "VISUAL_BASE_URL",
    "VISUAL_MODEL",
    "ARC_GIT_USER_EMAIL",
    "ARC_GIT_USER_NAME",
    "ARCBENCH_PIP_INDEX_URL",
    "ARCBENCH_PIP_TRUSTED_HOST",
    "ARCBENCH_PIP_EXTRA_INDEX_URL",
    "NPM_CONFIG_REGISTRY",
)


class LocalSubmitError(RuntimeError):
    pass


class MeterUsageError(RuntimeError):
    pass


@dataclass(frozen=True)
class MeterUsageSnapshot:
    access_key_id: str
    total_tokens: int
    total_cost: Decimal
    currency: str
    query_start: int


@dataclass(frozen=True)
class MeterUsageDelta:
    token_count: int
    cost: Decimal
    currency: str


def read_environment_value(env_file: str | None, name: str) -> str:
    """Resolve a value the same way the Docker invocation does.

    Explicit process environment variables override values from --env-file.
    """
    if name in os.environ:
        return os.environ[name].strip()
    if not env_file:
        return ""
    path = Path(env_file).resolve()
    if not path.is_file():
        return ""
    value = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() != name:
            continue
        candidate = candidate.strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"'}:
            candidate = candidate[1:-1]
        value = candidate
    return value


class MeterUsageClient:
    """Capture token and billed-cost deltas from ARC Bench Meter."""

    def __init__(self, base_url: str, api_key: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MeterUsageError("meter base URL must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()

    def _request_json(
        self,
        opener,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=body,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with opener.open(request, timeout=METER_TIMEOUT_SECONDS) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise MeterUsageError(f"meter request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeterUsageError(f"meter request failed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MeterUsageError("meter response was not a JSON object")
        return parsed

    def capture(
        self,
        *,
        wait_for_settlement: bool = False,
        query_start: int | None = None,
    ) -> MeterUsageSnapshot:
        if not self.api_key:
            raise MeterUsageError("meter API key is unavailable")
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        login = self._request_json(
            opener,
            "POST",
            "/api/user/login",
            payload={"access_key": self.api_key},
        )
        account = login.get("account")
        access_key_id = str(account.get("access_key_id") or "").strip() if isinstance(account, dict) else ""
        if not access_key_id:
            raise MeterUsageError("meter login response did not include an access-key identity")

        try:
            if wait_for_settlement:
                deadline = time.monotonic() + METER_SETTLE_TIMEOUT_SECONDS
                while True:
                    freshness = self._request_json(opener, "GET", "/api/user/freshness")
                    pending = freshness.get("pending_billing_events")
                    if isinstance(pending, int) and pending <= 0:
                        break
                    if time.monotonic() >= deadline:
                        raise MeterUsageError("meter billing did not settle before the timeout")
                    time.sleep(0.5)

            captured_at = int(time.time())
            end = captured_at + 1
            start = max(1, int(query_start or (end - METER_USAGE_WINDOW_SECONDS)))
            if start >= end:
                raise MeterUsageError("meter usage window start must be before its end")
            usage = self._request_json(
                opener,
                "GET",
                "/api/user/usage",
                query={"start": start, "end": end, "timezone": "UTC", "bucket": "week"},
            )
        finally:
            try:
                self._request_json(opener, "POST", "/api/user/logout")
            except MeterUsageError:
                pass

        if usage.get("ok") is not True or not isinstance(usage.get("rows"), list):
            raise MeterUsageError("meter usage response was not successful")
        total_tokens = 0
        total_cost = Decimal("0")
        try:
            for row in usage["rows"]:
                measures = row.get("measures") if isinstance(row, dict) else None
                if not isinstance(measures, dict):
                    continue
                total_tokens += int(measures.get("quantity") or 0)
                total_cost += Decimal(str(measures.get("amount") or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MeterUsageError("meter usage response contained invalid totals") from exc
        return MeterUsageSnapshot(
            access_key_id=access_key_id,
            total_tokens=max(0, total_tokens),
            total_cost=max(Decimal("0"), total_cost),
            currency=str(usage.get("currency") or "").strip().upper() or "UNKNOWN",
            query_start=start,
        )

    @staticmethod
    def delta(start: MeterUsageSnapshot, end: MeterUsageSnapshot) -> MeterUsageDelta:
        if start.access_key_id != end.access_key_id:
            raise MeterUsageError("meter access-key identity changed during the run")
        if start.currency != end.currency:
            raise MeterUsageError("meter billing currency changed during the run")
        return MeterUsageDelta(
            token_count=max(0, end.total_tokens - start.total_tokens),
            cost=max(Decimal("0"), end.total_cost - start.total_cost),
            currency=end.currency,
        )


def calculate_submission_score(pass_rate: float, token_cost: float | None) -> float | None:
    """Apply the competition score formula from 参赛须知-new(1).pdf."""
    p = Decimal(str(pass_rate))
    if p <= 0:
        return 0.0
    if token_cost is None:
        return None
    cost = Decimal(str(token_cost))
    if cost <= 0:
        # A model call is mandatory, so a positive pass rate with zero billed
        # cost is not a valid competition score (and the formula is singular).
        return None
    expected_cost = EXPECTED_COST_PER_PASS_RATE_POINT * p
    exponent = REWARD_EXPONENT if cost <= expected_cost else PENALTY_EXPONENT
    return float(p / ((cost / expected_cost) ** exponent))


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise LocalSubmitError(f"ZIP contains an unsafe path: {member.filename}")
            if not relative.parts:
                continue
            target = (destination / Path(*relative.parts)).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise LocalSubmitError(f"ZIP path escapes its destination: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def flatten_single_directory(directory: Path) -> None:
    children = list(directory.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        return
    nested_root = children[0]
    for child in list(nested_root.iterdir()):
        shutil.move(str(child), directory / child.name)
    nested_root.rmdir()


def copy_source(source: Path, destination: Path, *, flatten_zip: bool = False) -> None:
    source = source.resolve()
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=False)
        return
    if source.is_file() and source.suffix.lower() == ".zip":
        safe_extract_zip(source, destination)
        if flatten_zip:
            flatten_single_directory(destination)
        return
    raise LocalSubmitError(f"Expected a directory or .zip file: {source}")


def copy_baseline(source: Path, destination: Path) -> None:
    source = source.resolve()
    if source.is_file() and source.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="arcbench-local-baseline-") as temporary_directory:
            extracted = Path(temporary_directory)
            safe_extract_zip(source, extracted)
            copy_baseline(extracted, destination)
        return
    if not source.is_dir():
        raise LocalSubmitError(f"Expected a baseline application directory or .zip file: {source}")
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source)
        if set(relative.parts) & EXCLUDED_BASELINE_PARTS:
            continue
        if relative.name in {".env", ".env.local", ".env.production"} or relative.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def discover_tasks(data_root: Path, competition: str) -> dict[str, tuple[Path, Path]]:
    competition_root = (data_root / competition).resolve()
    if not competition_root.is_dir():
        raise LocalSubmitError(f"Competition does not exist: {competition_root}")

    tasks: dict[str, tuple[Path, Path]] = {}
    for requirements_yaml in sorted(competition_root.rglob("requirements.yaml")):
        parent = requirements_yaml.parent
        if parent.name == "requirements":
            task_root = parent.parent
            requirements_root = parent
        else:
            task_root = parent
            requirements_root = parent
        tests_root = task_root / "tests"
        if not tests_root.is_dir():
            continue
        relative_task = task_root.relative_to(competition_root).as_posix()
        slug = task_root.name
        key = slug if slug not in tasks else relative_task
        tasks[key] = (requirements_root, tests_root)
    return tasks


def resolve_task(
    data_root: Path,
    competition: str,
    task: str,
) -> tuple[str, Path, Path]:
    tasks = discover_tasks(data_root, competition)
    if task in tasks:
        requirements_root, tests_root = tasks[task]
        return task, requirements_root, tests_root

    matches = [
        (name, paths)
        for name, paths in tasks.items()
        if name.rsplit("/", 1)[-1] == task
    ]
    if len(matches) == 1:
        name, (requirements_root, tests_root) = matches[0]
        return name, requirements_root, tests_root
    available = ", ".join(sorted(tasks)) or "<none>"
    raise LocalSubmitError(
        f"Task '{task}' was not found uniquely in competition '{competition}'. "
        f"Available tasks: {available}"
    )


def copy_requirements(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in {"tests", "template", ".arc"}:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True, symlinks=False)
        elif child.is_file():
            shutil.copy2(child, target)
    if not (destination / "requirements.yaml").is_file():
        raise LocalSubmitError(f"Task is missing requirements.yaml: {source}")
    prerequisites = destination / "prerequisites.md"
    if not prerequisites.exists():
        prerequisites.write_text("", encoding="utf-8")


def validate_agent_entrypoint(submission_dir: Path) -> None:
    missing = [name for name in ("main.py", "requirements.txt") if not (submission_dir / name).is_file()]
    if missing:
        raise LocalSubmitError(
            f"Agent ZIP must contain {', '.join(missing)} at its archive root: {submission_dir}"
        )


def assemble_workspace(args: argparse.Namespace) -> tuple[Path, str]:
    data_root = Path(args.data_root).resolve()
    if args.requirements_dir:
        task_name = args.task
        requirements_root = Path(args.requirements_dir).resolve()
        if not requirements_root.is_dir():
            raise LocalSubmitError(f"Requirements directory does not exist: {requirements_root}")
        tests_root = Path(args.tests_dir).resolve() if args.tests_dir else None
        if tests_root is not None and not tests_root.is_dir():
            raise LocalSubmitError(f"Tests directory does not exist: {tests_root}")
    else:
        task_name, requirements_root, tests_root = resolve_task(
            data_root,
            args.competition,
            args.task,
        )

    if args.workspace:
        workspace = Path(args.workspace).resolve()
        if workspace.exists() and any(workspace.iterdir()):
            raise LocalSubmitError(
                f"Workspace is not empty: {workspace}. Choose a new path to preserve previous results."
            )
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_task = "".join(character if character.isalnum() or character in "-_" else "-" for character in task_name)
        workspace = (DEFAULT_RUNS_ROOT / f"{timestamp}-{safe_task}-{uuid.uuid4().hex[:8]}").resolve()

    submission_dir = workspace / "submission"
    template_dir = workspace / "template"
    tests_dir = workspace / "tests"
    arc_dir = template_dir / ".arc"
    requirements_dir = template_dir / "requirements"
    for directory in (submission_dir, template_dir, tests_dir, arc_dir):
        directory.mkdir(parents=True, exist_ok=True)

    copy_source(Path(args.agent), submission_dir, flatten_zip=True)
    validate_agent_entrypoint(submission_dir)

    if args.template:
        copy_baseline(Path(args.template), template_dir)

    copy_requirements(requirements_root, requirements_dir)
    evaluation_enabled = tests_root is not None
    if tests_root is not None:
        shutil.copytree(tests_root, tests_dir, dirs_exist_ok=True, symlinks=False)

    requirement_id = f"{args.competition}--{task_name.rsplit('/', 1)[-1]}"
    runner_spec = {
        "agent_source": "uploaded_agent",
        "runtime": "python",
        "submission_dir": "/workspace/submission",
        "template_dir": "/workspace/template",
        "project_dir": "/workspace/template",
        "tests_dir": "/workspace/tests",
        "arc_dir": ".arc",
        "requirement_dir": "requirements",
        "output_dir": "/workspace/template",
        "runner_events_path": ".arc/runner-events.jsonl",
        "traceability_dir": ".arc/traceability",
        "evaluation_enabled": evaluation_enabled,
        "task": {
            "category": "web",
            "requirement_id": requirement_id,
            "test_runner": "playwright",
        },
    }
    (workspace / "runner-spec.json").write_text(
        json.dumps(runner_spec, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (workspace / "execution.debug.log").write_text(
        "Local ARC-Bench workspace assembled successfully.\n",
        encoding="utf-8",
    )
    metadata = {
        "competition": args.competition,
        "task": task_name,
        "requirement_id": requirement_id,
        "runtime": "python",
        "agent": str(Path(args.agent).resolve()),
        "template": str(Path(args.template).resolve()) if args.template else None,
        "requirements_dir": str(requirements_root),
        "tests_dir": str(tests_root) if tests_root is not None else None,
        "evaluation_enabled": evaluation_enabled,
        "image": args.image,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (workspace / "local-submission.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return workspace, requirement_id


def docker_environment_arguments(env_file: str | None) -> list[str]:
    arguments: list[str] = []
    if env_file:
        path = Path(env_file).resolve()
        if not path.is_file():
            raise LocalSubmitError(f"Environment file does not exist: {path}")
        arguments.extend(["--env-file", str(path)])
    for name in PASSTHROUGH_ENVIRONMENT:
        if name in os.environ:
            arguments.extend(["--env", name])
    arguments.extend(
        [
            "--env",
            "ARC_DEBUG=1",
            "--env",
            "HOME=/tmp/arcbench-home",
            "--env",
            "PIP_TARGET=/tmp/arcbench-agent-deps",
            "--env",
            "PYTHONPATH=/tmp/arcbench-agent-deps",
            "--env",
            "npm_config_cache=/tmp/arcbench-npm-cache",
        ]
    )
    return arguments


def run_container(args: argparse.Namespace, workspace: Path) -> int:
    if shutil.which("docker") is None:
        raise LocalSubmitError("docker is not installed or is not available on PATH")

    container_name = f"arcbench-local-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "--mount",
        f"type=bind,source={workspace},target=/workspace",
    ]
    if os.name == "posix" and not args.run_as_root:
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    if args.memory:
        command.extend(["--memory", args.memory])
    if args.cpus:
        command.extend(["--cpus", args.cpus])
    command.extend(docker_environment_arguments(args.env_file))
    command.append(args.image)

    meter_start: MeterUsageSnapshot | None = None
    meter_delta: MeterUsageDelta | None = None
    meter_error: str | None = None
    meter_api_key = read_environment_value(args.env_file, "OPENAI_API_KEY")
    configured_meter_url = (
        args.meter_base_url
        or read_environment_value(args.env_file, "ARCBENCH_METER_BASE_URL")
        or DEFAULT_METER_BASE_URL
    )
    meter_client: MeterUsageClient | None = None
    if meter_api_key:
        try:
            meter_client = MeterUsageClient(configured_meter_url, meter_api_key)
            print("Capturing ARC Bench Meter baseline...", flush=True)
            meter_start = meter_client.capture()
        except MeterUsageError as exc:
            meter_error = str(exc)
            print(f"Meter baseline unavailable: {meter_error}", file=sys.stderr, flush=True)

    print(f"Workspace: {workspace}", flush=True)
    print(f"Container: {container_name}", flush=True)
    print("Starting the same run_submission.py used by the platform...", flush=True)
    completed = subprocess.run(command, check=False)
    if meter_client is not None and meter_start is not None:
        try:
            print("Waiting for ARC Bench Meter billing...", flush=True)
            meter_end = meter_client.capture(
                wait_for_settlement=True,
                query_start=meter_start.query_start,
            )
            meter_delta = meter_client.delta(meter_start, meter_end)
        except MeterUsageError as exc:
            meter_error = str(exc)
            print(f"Final Meter usage unavailable: {meter_error}", file=sys.stderr, flush=True)
    result_metadata = {
        "container_exit_code": completed.returncode,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "token_count": meter_delta.token_count if meter_delta else None,
        "token_cost": float(meter_delta.cost) if meter_delta else None,
        "token_cost_usd": float(meter_delta.cost) if meter_delta else None,
        "token_cost_currency": meter_delta.currency if meter_delta else None,
        "meter_error": meter_error,
    }
    (workspace / "local-run.json").write_text(
        json.dumps(result_metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return completed.returncode


def walk_playwright_report(report: dict) -> list[dict[str, object]]:
    tests: list[dict[str, object]] = []

    def visit_suite(suite: dict, inherited_file: str | None = None) -> None:
        suite_file = str(suite.get("file") or inherited_file or "")
        for spec in suite.get("specs", []):
            title = str(spec.get("title") or "Unnamed test")
            for test in spec.get("tests", []):
                results = test.get("results", [])
                statuses = [str(item.get("status") or "") for item in results]
                if any(status in {"failed", "timedOut", "interrupted"} for status in statuses):
                    passed = False
                elif "passed" in statuses:
                    passed = True
                elif "skipped" in statuses:
                    passed = False
                else:
                    passed = str(test.get("status") or "") == "expected"
                tests.append(
                    {
                        "file": suite_file,
                        "title": title,
                        "passed": passed,
                        "statuses": statuses,
                        "duration_ms": sum(int(item.get("duration") or 0) for item in results),
                    }
                )
        for child in suite.get("suites", []):
            visit_suite(child, suite_file)

    for suite in report.get("suites", []):
        visit_suite(suite)
    return tests


def read_result(workspace: Path, *, show_tests: bool = False) -> int:
    workspace = workspace.resolve()
    report_path = workspace / "template" / ".arc" / "playwright-report.json"
    run_path = workspace / "local-run.json"
    execution_path = workspace / "template" / ".arc" / "agent-execution.json"

    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
    execution = json.loads(execution_path.read_text(encoding="utf-8")) if execution_path.is_file() else {}
    submission_path = workspace / "local-submission.json"
    submission = json.loads(submission_path.read_text(encoding="utf-8")) if submission_path.is_file() else {}
    if submission.get("evaluation_enabled") is False:
        result = {
            "workspace": str(workspace),
            "container_exit_code": run.get("container_exit_code"),
            "agent_duration_seconds": execution.get("duration_seconds"),
            "evaluation_status": "skipped",
            "score": None,
            "token_count": run.get("token_count"),
            "token_cost": run.get("token_cost"),
            "token_cost_usd": run.get("token_cost_usd", run.get("token_cost")),
            "token_cost_currency": run.get("token_cost_currency"),
            "meter_error": run.get("meter_error"),
            "playwright_report": None,
            "stdout_log": str(workspace / "template" / ".arc" / "stdout.log"),
            "debug_log": str(workspace / "execution.debug.log"),
        }
        (workspace / "local-result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Playwright evaluation was skipped because no --tests-dir was provided.")
        return 0 if run.get("container_exit_code") == 0 else 1
    if not report_path.is_file():
        result = {
            "workspace": str(workspace),
            "container_exit_code": run.get("container_exit_code"),
            "agent_duration_seconds": execution.get("duration_seconds"),
            "evaluation_status": "failed",
            "passed": 0,
            "failed": 0,
            "total": 0,
            "score": 0.0,
            "test_pass_rate": 0.0,
            "token_count": run.get("token_count"),
            "token_cost": run.get("token_cost"),
            "token_cost_usd": run.get("token_cost_usd", run.get("token_cost")),
            "token_cost_currency": run.get("token_cost_currency"),
            "meter_error": run.get("meter_error"),
            "playwright_report": None,
            "stdout_log": str(workspace / "template" / ".arc" / "stdout.log"),
            "debug_log": str(workspace / "execution.debug.log"),
            "failure_reason": "No Playwright report was produced",
        }
        (workspace / "local-result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2

    report = json.loads(report_path.read_text(encoding="utf-8"))
    tests = walk_playwright_report(report)
    passed = sum(1 for test in tests if test["passed"])
    failed = len(tests) - passed
    exact_pass_rate = (passed / len(tests)) * 100 if tests else 0.0
    pass_rate = round(exact_pass_rate, 1)
    feature_outcomes: dict[str, bool] = {}
    for index, test in enumerate(tests):
        feature = str(test.get("file") or "").strip() or f"__unknown_feature_{index}"
        feature_outcomes[feature] = feature_outcomes.get(feature, True) and bool(test["passed"])
    feature_total = len(feature_outcomes)
    feature_implemented = sum(1 for implemented in feature_outcomes.values() if implemented)
    feature_rate = round((feature_implemented / feature_total) * 100, 1) if feature_total else 0.0
    submission_score = calculate_submission_score(exact_pass_rate, run.get("token_cost"))
    result = {
        "workspace": str(workspace),
        "container_exit_code": run.get("container_exit_code"),
        "agent_duration_seconds": execution.get("duration_seconds"),
        "evaluation_status": "completed",
        "passed": passed,
        "failed": failed,
        "total": len(tests),
        "score": submission_score,
        "test_pass_rate": pass_rate,
        "token_count": run.get("token_count"),
        "token_cost": run.get("token_cost"),
        "token_cost_usd": run.get("token_cost_usd", run.get("token_cost")),
        "token_cost_currency": run.get("token_cost_currency"),
        "meter_error": run.get("meter_error"),
        "feature_implemented_count": feature_implemented,
        "feature_total_count": feature_total,
        "feature_implementation_rate": feature_rate,
        "playwright_report": str(report_path),
        "stdout_log": str(workspace / "template" / ".arc" / "stdout.log"),
        "debug_log": str(workspace / "execution.debug.log"),
    }
    (workspace / "local-result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if show_tests:
        for test in tests:
            marker = "PASS" if test["passed"] else "FAIL"
            print(f"[{marker}] {test['file']} :: {test['title']} ({test['duration_ms']} ms)")
    return 0 if failed == 0 and bool(tests) else 1


def list_tasks(args: argparse.Namespace) -> int:
    tasks = discover_tasks(Path(args.data_root).resolve(), args.competition)
    if not tasks:
        print(f"No runnable tasks found for competition '{args.competition}'.")
        return 1
    for name in sorted(tasks):
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an ARC-Bench agent and competition test suite in the local runner Docker image."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tasks found in one competition")
    list_parser.add_argument("--competition", required=True)
    list_parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))

    run_parser = subparsers.add_parser("run", help="Assemble a workspace and run one local submission")
    run_parser.add_argument("--competition", default="public-practice", help="Result label used in requirement_id")
    run_parser.add_argument("--task", default="counter", help="Result label used in requirement_id")
    run_parser.add_argument("--agent", required=True, help="Agent ZIP or extracted agent directory")
    run_parser.add_argument("--template", help="Optional baseline application directory or ZIP, for Evolution simulation")
    run_parser.add_argument(
        "--output-dir",
        "--workspace",
        dest="workspace",
        help="Empty host output directory; defaults to runs/<unique-id>",
    )
    run_parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    run_parser.add_argument(
        "--requirements-dir",
        default=str(DEFAULT_REQUIREMENTS_DIR),
        help="Public requirements directory containing requirements.yaml and optional reference/",
    )
    run_parser.add_argument(
        "--tests-dir",
        help="Optional public Playwright tests directory; omit it to skip evaluation",
    )
    run_parser.add_argument("--image", default=os.environ.get("ARCBENCH_LOCAL_IMAGE", DEFAULT_IMAGE))
    run_parser.add_argument("--env-file", help="Docker env file containing model settings; do not commit it")
    run_parser.add_argument(
        "--meter-base-url",
        help=f"ARC Bench Meter portal URL (default: ARCBENCH_METER_BASE_URL or {DEFAULT_METER_BASE_URL})",
    )
    run_parser.add_argument("--memory", default="2g")
    run_parser.add_argument("--cpus", default="1")
    run_parser.add_argument("--run-as-root", action="store_true", help="Run as root inside Docker")
    run_parser.add_argument("--show-tests", action="store_true")
    run_parser.add_argument("--prepare-only", action="store_true", help="Assemble the workspace without starting Docker")

    result_parser = subparsers.add_parser("result", help="Read a completed local workspace")
    result_parser.add_argument("--workspace", required=True)
    result_parser.add_argument("--show-tests", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "list":
            return list_tasks(args)
        if args.command == "result":
            return read_result(Path(args.workspace), show_tests=args.show_tests)
        workspace, _requirement_id = assemble_workspace(args)
        if args.prepare_only:
            print(f"Workspace prepared: {workspace}")
            return 0
        exit_code = run_container(args, workspace)
        result_exit_code = read_result(workspace, show_tests=args.show_tests)
        if exit_code != 0:
            return exit_code
        return result_exit_code
    except (LocalSubmitError, OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"local-submit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
