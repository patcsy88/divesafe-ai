"""FastAPI application factory: the Dive Assessment API and the human decision interface.

Error mapping is deliberately narrow. Only specific, expected failures become 4xx responses with
fixed messages; anything else is a logged 500 with an opaque body, so internal bugs are never
reported as client errors and internals are never echoed.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from divesafe import __version__
from divesafe.api.auth import Principal, require_principal
from divesafe.api.limits import BodySizeLimitMiddleware
from divesafe.api.schemas import (
    ActualConditionsRequest,
    AssessmentView,
    CreateAssessmentRequest,
    DecisionRequest,
    EvidenceView,
    SiteView,
)
from divesafe.api.state import AppState, build_default_state
from divesafe.config import configure_logging, get_settings
from divesafe.domain import ActualConditions, AssessmentRecord, DivePlan
from divesafe.orchestration import (
    AlreadyDecidedError,
    DecisionRequiredError,
    InvalidPlanError,
    UnsafeConfigurationError,
    assess_dive,
    decide,
    report_actual_conditions,
)
from divesafe.services import ConflictError, InMemoryAssessmentRepository, RecordNotFoundError

logger = logging.getLogger(__name__)

AssessmentId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


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

    hidden = settings.environment == "production"
    app = FastAPI(
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

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the submitted input: it can be huge, hostile or sensitive.
        errors = [
            {"loc": [str(part) for part in e.get("loc", ())], "msg": str(e.get("msg", ""))[:200]}
            for e in exc.errors()[:20]
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal error"}, status_code=500)

    def _load(assessment_id: str) -> AssessmentRecord:
        record = app_state.repository.get(assessment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="assessment not found")
        return record

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": "durable" if durable else "in-memory (not durable)",
        }

    @app.post("/v1/assessments", status_code=201, tags=["assessments"])
    async def create_assessment(
        body: CreateAssessmentRequest, principal: Annotated[Principal, Depends(require_principal)]
    ) -> AssessmentView:
        if app_state.engine is None:
            raise HTTPException(
                status_code=503, detail="the evidence policy is not configured on this server"
            )
        site = app_state.sites.get(body.site_id)
        if site is None:
            raise HTTPException(status_code=404, detail="unknown site")
        try:
            plan = DivePlan(**body.model_dump())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_messages(exc)) from exc
        try:
            record = await assess_dive(
                assessment_id=uuid.uuid4().hex,
                plan=plan,
                site=site,
                connectors=app_state.connectors,
                engine=app_state.engine,
                now=app_state.clock(),
                provider=app_state.provider,
            )
        except InvalidPlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UnsafeConfigurationError as exc:
            logger.error("unsafe configuration", extra={"detail": str(exc)})
            raise HTTPException(status_code=503, detail="server configuration is unsafe") from exc
        app_state.repository.create(record)
        logger.info(
            "assessment stored", extra={"assessment_id": record.id, "actor": principal.actor}
        )
        return AssessmentView.of(record)

    @app.get("/v1/assessments/{assessment_id}", tags=["assessments"])
    def get_assessment(
        assessment_id: AssessmentId,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AssessmentView:
        return AssessmentView.of(_load(assessment_id))

    @app.get("/v1/assessments/{assessment_id}/evidence", tags=["assessments"])
    def get_evidence(
        assessment_id: AssessmentId,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> EvidenceView:
        return EvidenceView.of(_load(assessment_id))

    @app.get("/v1/sites/{site_id}", tags=["sites"])
    def get_site(
        site_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,100}$")],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> SiteView:
        site = app_state.sites.get(site_id)
        if site is None:
            raise HTTPException(status_code=404, detail="unknown site")
        return SiteView.of(site)

    @app.post("/v1/assessments/{assessment_id}/decision", tags=["human gate"])
    def record_decision(
        assessment_id: AssessmentId,
        body: DecisionRequest,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AssessmentView:
        record = _load(assessment_id)
        if record.human_decision is not None:
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
        except AlreadyDecidedError as exc:
            raise HTTPException(
                status_code=409, detail="this assessment already has a decision"
            ) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="assessment changed; reload it") from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail="assessment not found") from exc
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
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AssessmentView:
        record = _load(assessment_id)
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
        logger.info(
            "actual conditions recorded",
            extra={"assessment_id": assessment_id, "actor": principal.actor},
        )
        return AssessmentView.of(updated)

    return app
