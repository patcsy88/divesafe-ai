"""FastAPI application factory: the Dive Assessment API and the human decision interface.

Error mapping is deliberately narrow. Only specific, expected failures become 4xx responses with
fixed messages; anything else is a logged 500 with an opaque body, so internal bugs are never
reported as client errors and internals are never echoed.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from divesafe import __version__
from divesafe.agents.runner import stage_timeout
from divesafe.api.auth import Principal, Role, require_any_role, require_role
from divesafe.api.limits import BodySizeLimitMiddleware
from divesafe.api.runner import RunManager
from divesafe.api.schemas import (
    ActualConditionsRequest,
    AssessmentView,
    CreateAssessmentRequest,
    DecisionRequest,
    EvidenceView,
    RunStatusView,
    SiteListView,
    SiteView,
)
from divesafe.api.state import AppState, build_default_state
from divesafe.config import configure_logging, get_settings
from divesafe.config.settings import CONNECTOR_BUDGET_SECONDS, RUN_MARGIN_SECONDS
from divesafe.data.http import shutdown_http_pool
from divesafe.domain import ActualConditions, AssessmentRecord, DivePlan
from divesafe.orchestration import (
    AlreadyDecidedError,
    DecisionRequiredError,
    NotAssignedDeciderError,
    assess_dive,
    check_assigned,
    decide,
    report_actual_conditions,
)
from divesafe.services import (
    ConflictError,
    InMemoryAssessmentRepository,
    InvalidSuccessorError,
    RecordNotFoundError,
    RepositoryIntegrityError,
    RepositoryUnavailableError,
    UnstorableRecordError,
)
from divesafe.services.runs import (
    CapacityError,
    FailureCode,
    NewRun,
    PendingRun,
    RunMismatchError,
    RunState,
)

logger = logging.getLogger(__name__)

_NOT_UPDATABLE = "the assessment could not be updated; reload it"
_NOT_ASSIGNED = (
    "nothing was recorded; this assessment is assigned to another decider and is still pending"
)

AssessmentId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
# How long after creation a create may be repeated under the same key. Operational, not a safety
# limit: past it the stored result is old evidence, so the caller must start a new assessment.
REPLAY_WINDOW = timedelta(minutes=10)


def _idempotent_id(actor: str, key: str) -> str:
    """The assessment id for this caller's retry key. The same actor and key always give the same
    id, so the repository's unique id is what stops a second record; no extra storage."""
    return hashlib.sha256(f"divesafe-idempotency-v1\0{actor}\0{key}".encode()).hexdigest()[:32]


READ = require_role(Role.VIEWER, limit_key="read")
# A key without the viewer role may still poll the runs it started (ADR 0019).
READ_OWN = require_any_role((Role.VIEWER, Role.ASSESSOR), limit_key="read")
CREATE = require_role(Role.ASSESSOR, limit_key="create")
DECIDE = require_role(Role.DECIDER, limit_key="decide")


def _messages(exc: ValidationError) -> list[str]:
    return [str(e["msg"]) for e in exc.errors()]


def create_app(state: AppState | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app_state = state if state is not None else build_default_state(settings)
    durable = not isinstance(app_state.repository, InMemoryAssessmentRepository)

    if not durable:
        if settings.environment == "production":
            raise RuntimeError("refusing to start in production with non-durable in-memory storage")
        logger.warning(
            "assessments are stored in memory only and are lost on restart; "
            "run a single worker and do not use this for real decisions"
        )
    if app_state.decision_max_age is None:
        logger.warning("DIVESAFE_DECISION_MAX_AGE_MINUTES is unset: decisions have no age limit")

    if app_state.provider is not None:
        needed = stage_timeout(app_state.provider) + CONNECTOR_BUDGET_SECONDS + RUN_MARGIN_SECONDS
        if app_state.runs.deadline.total_seconds() < needed:
            raise RuntimeError(
                "the run deadline is shorter than the model provider's stage timeout plus the "
                "connector budget and margin; every run would be cut off mid-stage"
            )

    runner = RunManager(
        app_state.repository, deadline=app_state.runs.deadline, grace=app_state.runs.shutdown_grace
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runner.start_cleanup(app_state.runs.failed_retention)
        try:
            yield
        finally:
            try:
                await runner.shutdown()  # before the provider closes, so cancelled runs end first
            except Exception:
                logger.error("running assessments did not stop cleanly")
            shutdown_http_pool()
            close = getattr(app_state.provider, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # shutdown must go on; the error text may hold request data
                    logger.error("the model provider did not close cleanly")

    hidden = settings.environment == "production"
    app = FastAPI(
        lifespan=lifespan,
        title="DiveSafe AI",
        version=__version__,
        description=(
            "Decision support only. The final decision always belongs to the diver, "
            "dive master or dive leader."
        ),
        docs_url=None if hidden else "/docs",
        redoc_url=None if hidden else "/redoc",
        openapi_url=None if hidden else "/openapi.json",
    )
    app.add_middleware(BodySizeLimitMiddleware)

    @app.middleware("http")
    async def no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.state.divesafe = app_state
    app.state.runner = runner

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the submitted input: it can be huge, hostile or sensitive.
        errors = [
            {"loc": [str(part) for part in e.get("loc", ())], "msg": str(e.get("msg", ""))[:200]}
            for e in exc.errors()[:20]
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(RepositoryUnavailableError)
    async def store_unavailable(request: Request, exc: RepositoryUnavailableError) -> JSONResponse:
        logger.error("storage unavailable", extra={"path": request.url.path})
        return JSONResponse(
            {"detail": "storage is unavailable; nothing was recorded"}, status_code=503
        )

    @app.exception_handler(RepositoryIntegrityError)
    async def store_integrity(request: Request, exc: RepositoryIntegrityError) -> JSONResponse:
        logger.error("storage integrity failure", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal error"}, status_code=500)

    @app.exception_handler(UnstorableRecordError)
    async def unstorable(request: Request, exc: UnstorableRecordError) -> JSONResponse:
        logger.error("record could not be stored", extra={"path": request.url.path})
        return JSONResponse(
            {"detail": "the content cannot be stored; nothing was recorded"}, status_code=422
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal error"}, status_code=500)

    def _may_see(run: PendingRun, principal: Principal) -> bool:
        return (
            Role.VIEWER in principal.roles
            or run.created_by == principal.actor
            or run.assigned_decider == principal.actor
        )

    def _load(assessment_id: str, principal: Principal | None = None) -> AssessmentRecord:
        record = app_state.repository.get(assessment_id)
        if record is None:
            run = app_state.repository.get_run(assessment_id)
            if run is None:
                record = app_state.repository.get(assessment_id)  # finished between the reads
                if record is not None:
                    return record
            if run is not None and principal is not None and _may_see(run, principal):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "nothing was recorded; this assessment has no result yet"
                        if run.state is RunState.RUNNING
                        else "nothing was recorded; this assessment did not finish"
                    ),
                )
            raise HTTPException(status_code=404, detail="assessment not found")
        return record

    def _require_assigned(record: AssessmentRecord, principal: Principal) -> None:
        try:
            check_assigned(record, principal.actor)
        except NotAssignedDeciderError as exc:
            logger.warning(
                "refused: not the assigned decider",
                extra={"assessment_id": record.id, "actor": principal.actor},
            )
            raise HTTPException(status_code=403, detail=_NOT_ASSIGNED) from exc

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": "durable" if durable else "in-memory (not durable)",
        }

    @app.post("/v1/assessments", status_code=202, tags=["assessments"])
    async def create_assessment(
        body: CreateAssessmentRequest,
        principal: Annotated[Principal, Depends(CREATE)],
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> AssessmentView | RunStatusView:
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key must be 16 to 128 letters, digits, '.', '_' or '-'",
            )
        if runner.closing:
            raise HTTPException(
                status_code=503, detail="the server is shutting down; nothing was recorded"
            )
        if app_state.engine is None:
            raise HTTPException(
                status_code=503, detail="the evidence policy is not configured on this server"
            )
        site = app_state.sites.get(body.site_id)
        if site is None:
            raise HTTPException(status_code=404, detail="unknown site")
        try:
            plan = DivePlan(**body.model_dump(exclude={"decider"}))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_messages(exc)) from exc
        if body.decider is not None:
            requested = body.decider
        else:
            requested = principal.actor if Role.DECIDER in principal.roles else ""
        decider = (
            app_state.authenticator.canonical_decider(requested) if requested.strip() else None
        )
        if decider is None:
            logger.warning("decider assignment refused", extra={"actor": principal.actor})
            raise HTTPException(
                status_code=422,
                detail="name a known key holder with the decider role to record the decision",
            )
        assessment_id = (
            _idempotent_id(principal.actor, idempotency_key)
            if idempotency_key is not None
            else uuid.uuid4().hex
        )

        def replay(existing: AssessmentRecord) -> AssessmentView:
            # The same key must mean the same request; anything else is refused, not reinterpreted.
            if (
                existing.plan != plan
                or existing.assigned_decider != decider
                or existing.created_by != principal.actor
            ):
                raise HTTPException(
                    status_code=422,
                    detail="this Idempotency-Key was already used for a different request",
                )
            if app_state.clock() - existing.created_at > REPLAY_WINDOW:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "nothing was recorded; the earlier result for this Idempotency-Key is too "
                        "old to repeat. Run a new assessment"
                    ),
                )
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
            logger.info(
                "assessment create replayed",
                extra={"assessment_id": existing.id, "actor": principal.actor},
            )
            if Role.VIEWER in principal.roles:
                return AssessmentView.of(existing)
            # A create-only key must not use a repeat to read what happened since: it gets the
            # record as it was when it created it.
            return AssessmentView.withheld(existing)

        engine = app_state.engine
        if not engine.policy.required_categories:
            raise HTTPException(status_code=503, detail="server configuration is unsafe")
        if plan.planned_start < app_state.clock():
            raise HTTPException(
                status_code=422, detail="planned_start is in the past; assess a future dive window"
            )
        if idempotency_key is not None:
            earlier = await run_in_threadpool(app_state.repository.get, assessment_id)
            if earlier is not None:
                return replay(earlier)
        try:
            admission = await run_in_threadpool(
                lambda: app_state.repository.admit(
                    NewRun(
                        id=assessment_id,
                        created_by=principal.actor,
                        assigned_decider=decider,
                        plan=plan,
                    ),
                    max_in_flight=app_state.runs.max_in_flight,
                    max_per_actor=app_state.runs.max_per_actor,
                    lease=app_state.runs.lease,
                )
            )
        except RunMismatchError as exc:
            raise HTTPException(
                status_code=422,
                detail="this Idempotency-Key was already used for a different request",
            ) from exc
        except CapacityError as exc:
            logger.warning(
                "run refused: at capacity", extra={"actor": principal.actor, "scope": exc.scope}
            )
            raise HTTPException(
                status_code=429,
                detail="nothing was recorded; too many assessments are running, try again later",
                headers={"Retry-After": str(app_state.runs.retry_after_seconds)},
            ) from exc
        if admission.kind == "completed":
            stored = await run_in_threadpool(app_state.repository.get, assessment_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="assessment not found")
            return replay(stored)
        assert admission.run is not None
        if admission.kind == "failed":
            raise HTTPException(
                status_code=409,
                detail="nothing was recorded; this run did not finish. Use a new Idempotency-Key",
            )
        if admission.kind == "started":
            assert admission.run_token is not None
            clock = app_state.clock

            async def work() -> AssessmentRecord:
                return await assess_dive(
                    created_by=principal.actor,
                    assigned_decider=decider,
                    assessment_id=assessment_id,
                    plan=plan,
                    site=site,
                    connectors=app_state.connectors,
                    engine=engine,
                    now=clock(),
                    provider=app_state.provider,
                    clock=clock,
                )

            try:
                runner.start(assessment_id, admission.run_token, principal.actor, work)
            except RuntimeError as exc:
                await run_in_threadpool(
                    app_state.repository.fail,
                    assessment_id,
                    admission.run_token,
                    FailureCode.SHUTDOWN,
                )
                raise HTTPException(
                    status_code=503, detail="the server is shutting down; nothing was recorded"
                ) from exc
        response.headers["Location"] = f"/v1/assessments/{assessment_id}"
        return RunStatusView.of(admission.run)

    @app.get("/v1/assessments/{assessment_id}", tags=["assessments"])
    def get_assessment(
        assessment_id: AssessmentId,
        principal: Annotated[Principal, Depends(READ_OWN)],
    ) -> AssessmentView | RunStatusView:
        viewer = Role.VIEWER in principal.roles
        record = app_state.repository.get(assessment_id)
        run = None if record is not None else app_state.repository.get_run(assessment_id)
        if record is None and run is None:
            # A run can finish between the two reads above: look for the record once more.
            record = app_state.repository.get(assessment_id)
        if record is not None:
            if viewer:
                return AssessmentView.of(record)
            if record.created_by != principal.actor:
                raise HTTPException(status_code=404, detail="assessment not found")
            # A create-only key sees the record as it was created, as a replay does.
            return AssessmentView.withheld(record)
        if run is None or not (viewer or run.created_by == principal.actor):
            raise HTTPException(status_code=404, detail="assessment not found")
        return RunStatusView.of(run)

    @app.get("/v1/assessments/{assessment_id}/evidence", tags=["assessments"])
    def get_evidence(
        assessment_id: AssessmentId,
        principal: Annotated[Principal, Depends(READ)],
    ) -> EvidenceView:
        return EvidenceView.of(_load(assessment_id, principal))

    @app.get("/v1/sites", tags=["sites"])
    def list_sites(principal: Annotated[Principal, Depends(READ)]) -> SiteListView:
        return SiteListView(sites=tuple(SiteView.of(s) for s in app_state.sites.values()))

    @app.get("/v1/sites/{site_id}", tags=["sites"])
    def get_site(
        site_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,100}$")],
        principal: Annotated[Principal, Depends(READ)],
    ) -> SiteView:
        site = app_state.sites.get(site_id)
        if site is None:
            raise HTTPException(status_code=404, detail="unknown site")
        return SiteView.of(site)

    @app.post("/v1/assessments/{assessment_id}/decision", tags=["human gate"])
    def record_decision(
        assessment_id: AssessmentId,
        body: DecisionRequest,
        principal: Annotated[Principal, Depends(DECIDE)],
        response: Response,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> AssessmentView:
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key must be 16 to 128 letters, digits, '.', '_' or '-'",
            )

        def same_decision(existing: AssessmentRecord) -> bool:
            made = existing.human_decision
            return (
                idempotency_key is not None
                and made is not None
                and made.decided_by == principal.actor
                and made.decision == body.decision
                and (made.override_rationale or "") == (body.rationale or "")
            )

        def replay(existing: AssessmentRecord) -> AssessmentView:
            response.headers["Idempotent-Replay"] = "true"
            logger.info(
                "decision replayed",
                extra={"assessment_id": assessment_id, "actor": principal.actor},
            )
            return AssessmentView.of(existing)

        record = _load(assessment_id, principal)
        _require_assigned(record, principal)
        if record.human_decision is not None:
            # A retry of the very same decision (same person, outcome and reason) after an
            # unknown outcome is reported back, unchanged. Anything else stays a 409.
            if same_decision(record):
                return replay(record)
            raise HTTPException(status_code=409, detail="this assessment already has a decision")
        now = app_state.clock()
        max_age = app_state.decision_max_age
        if max_age is not None and now - record.created_at > max_age:
            raise HTTPException(
                status_code=409, detail="assessment is too old to decide on; run a new assessment"
            )
        if body.decision != record.final_recommendation and not (body.rationale or "").strip():
            raise HTTPException(status_code=422, detail="an override requires a rationale")
        try:
            updated = decide(
                record,
                decided_by=principal.actor,
                decision=body.decision,
                decided_at=now,
                rationale=body.rationale,
            )
            app_state.repository.replace(record, updated)
        except NotAssignedDeciderError as exc:
            logger.warning(
                "decision refused: not the assigned decider",
                extra={"assessment_id": assessment_id, "actor": principal.actor},
            )
            raise HTTPException(status_code=403, detail=_NOT_ASSIGNED) from exc
        except (AlreadyDecidedError, ConflictError) as exc:
            # Two identical submissions can race; the loser is a replay if the winner matches it.
            try:
                latest = app_state.repository.get(assessment_id)
            except RepositoryUnavailableError as unavailable:
                raise HTTPException(
                    status_code=503,
                    detail="the outcome is unknown; reload to see whether it was recorded",
                ) from unavailable
            if latest is not None and same_decision(latest):
                return replay(latest)
            if isinstance(exc, ConflictError):
                raise HTTPException(
                    status_code=409, detail="assessment changed; reload it"
                ) from exc
            raise HTTPException(
                status_code=409, detail="this assessment already has a decision"
            ) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail="assessment not found") from exc
        except InvalidSuccessorError as exc:
            logger.error(
                "decision refused: invalid successor", extra={"assessment_id": assessment_id}
            )
            raise HTTPException(status_code=409, detail=_NOT_UPDATABLE) from exc
        logger.info(
            "human decision recorded",
            extra={
                "assessment_id": assessment_id,
                "actor": principal.actor,
                "override": body.decision != record.final_recommendation,
            },
        )
        return AssessmentView.of(updated)

    @app.post("/v1/assessments/{assessment_id}/actual-conditions", tags=["human gate"])
    def record_actual_conditions(
        assessment_id: AssessmentId,
        body: ActualConditionsRequest,
        principal: Annotated[Principal, Depends(DECIDE)],
    ) -> AssessmentView:
        record = _load(assessment_id, principal)
        _require_assigned(record, principal)
        now = app_state.clock()
        if record.human_decision is not None and now < record.plan.planned_start:
            raise HTTPException(
                status_code=409, detail="the planned dive has not started; report conditions later"
            )
        try:
            actual = ActualConditions(
                reported_at=now,
                reported_by=principal.actor,
                observations=body.observations,
            )
            updated = report_actual_conditions(record, actual=actual)
            app_state.repository.replace(record, updated)
        except NotAssignedDeciderError as exc:
            logger.warning(
                "actual conditions refused: not the assigned decider",
                extra={"assessment_id": assessment_id, "actor": principal.actor},
            )
            raise HTTPException(status_code=403, detail=_NOT_ASSIGNED) from exc
        except DecisionRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AlreadyDecidedError as exc:
            raise HTTPException(
                status_code=409, detail="actual conditions were already reported"
            ) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="assessment changed; reload it") from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail="assessment not found") from exc
        except InvalidSuccessorError as exc:
            logger.error(
                "actual conditions refused: invalid successor",
                extra={"assessment_id": assessment_id},
            )
            raise HTTPException(status_code=409, detail=_NOT_UPDATABLE) from exc
        logger.info(
            "actual conditions recorded",
            extra={"assessment_id": assessment_id, "actor": principal.actor},
        )
        return AssessmentView.of(updated)

    return app
