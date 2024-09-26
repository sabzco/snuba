from __future__ import annotations

import time
from random import random
from typing import Any, Mapping, Optional, Union

import sentry_sdk
from sentry_kafka_schemas.schema_types import snuba_queries_v1
from sentry_sdk import Hub
from usageaccountant import UsageUnit

from snuba import environment, settings, state
from snuba.cogs.accountant import record_cogs
from snuba.datasets.storage import StorageNotAvailable
from snuba.query.exceptions import QueryPlanException
from snuba.querylog.query_metadata import QueryStatus, SnubaQueryMetadata, Status
from snuba.request import Request
from snuba.utils.metrics.timer import Timer
from snuba.utils.metrics.wrapper import MetricsWrapper
from snuba.web import QueryException, QueryResult

metrics = MetricsWrapper(environment.metrics, "api")
from snuba.querylog.query_metadata import get_request_status


def _record_timer_metrics(
    request: Request,
    timer: Timer,
    query_metadata: SnubaQueryMetadata,
    result: Union[QueryResult, QueryException, QueryPlanException],
) -> None:
    final = str(request.query.get_final())
    referrer = request.referrer or "none"
    app_id = request.attribution_info.app_id.key or "none"
    parent_api = request.attribution_info.parent_api or "none"
    tags = {
        "status": query_metadata.status.value,
        "request_status": query_metadata.request_status.value,
        "slo": query_metadata.slo.value,
        "referrer": referrer,
        "parent_api": parent_api,
        "final": final,
        "dataset": query_metadata.dataset,
        "app_id": app_id,
    }
    mark_tags = {
        "final": final,
        "referrer": referrer,
        "parent_api": parent_api,
        "dataset": query_metadata.dataset,
    }
    if isinstance(result, StorageNotAvailable):
        # The QueryPlanException is raised outside the query execution flow.
        # As a result, its status and SLO values are not based on its query_list
        status = get_request_status(result)
        tags = {
            "status": QueryStatus.ERROR.value,
            "request_status": status.status.value,
            "slo": status.slo.value,
            "referrer": referrer,
            "parent_api": parent_api,
            "final": final,
            "dataset": query_metadata.dataset,
            "app_id": app_id,
        }

    timer.send_metrics_to(
        metrics,
        tags=tags,
        mark_tags=mark_tags,
    )


def _record_bytes_scanned_metrics(
    query_metadata: SnubaQueryMetadata,
    result: Union[QueryResult, QueryException, QueryPlanException],
) -> None:
    """
    Experimental metrics - trying to understand whether or not
    profile.bytes is correct or we should be using progress.bytes
    for calculating bytes scanned per Query.

    Should be removed once we have gathered data and made a decision.
    """
    if not isinstance(result, QueryResult):
        return
    profile = result.result["profile"]
    if not profile or "progress_bytes" not in profile or "bytes" not in profile:
        return

    tags = {"dataset": query_metadata.dataset}

    profile_bytes = profile["bytes"]
    metrics.increment("profile_bytes", profile_bytes, tags)

    progress_bytes = profile["progress_bytes"]
    metrics.increment("progress_bytes", progress_bytes, tags)


def _record_cogs(
    request: Request,
    query_metadata: SnubaQueryMetadata,
    result: Union[QueryResult, QueryException, QueryPlanException],
) -> None:
    """
    Record bytes scanned for the clickhouse compute of resource of a query.
    """

    if not isinstance(result, QueryResult):
        return

    profile = result.result.get("profile")
    if not profile or (bytes_scanned := profile.get("progress_bytes")) is None:
        return

    # The dataset is usually a good proxy for app_feature
    # However, this is not always the case. We can
    # check the entity as well as a fallback option
    # if the dataset is incorrect in the querylog.

    app_feature = query_metadata.dataset.replace("_", "")

    if (
        query_metadata.dataset == "generic_metrics"
        or query_metadata.entity.startswith("generic_metrics")
    ) and (
        (use_case_id := request.attribution_info.tenant_ids.get("use_case_id"))
        is not None
    ):
        app_feature = f"genericmetrics_{use_case_id}"

    elif query_metadata.dataset == "events":
        app_feature = "errors"

    cluster_name = query_metadata.query_list[0].stats.get("cluster_name", "")

    if not cluster_name.startswith("snuba-gen-metrics"):
        return  # Only track shared clusters

    # Sanitize the cluster name to line up with the resource_id naming convention
    cluster_name = (
        cluster_name.replace("-", "_")
        .replace("snuba_gen_metrics", "generic_metrics_clickhouse")
        .replace("_0", "")
    )

    if random() < (state.get_config("snuba_api_cogs_probability") or 0):
        record_cogs(
            resource_id=f"{cluster_name}",
            app_feature=app_feature,
            amount=bytes_scanned,
            usage_type=UsageUnit.BYTES,
        )

        # TODO: Record the time spent in the API compared to time spent running the
        # Clickhouse query, so we can track usage of the API pods themselves.


def record_query(
    request: Request,
    timer: Timer,
    query_metadata: SnubaQueryMetadata,
    result: Union[QueryResult, QueryException, QueryPlanException],
) -> None:
    """
    Records a request after it has been parsed and validated, whether
    we actually ran a query or not.
    """

    extra_data: Mapping[str, Any] = {}
    if not isinstance(result, QueryPlanException):
        extra_data = result.extra
    if settings.RECORD_QUERIES:
        # Send to redis
        # We convert this to a dict before passing it to state in order to avoid a
        # circular dependency, where state would depend on the higher level
        # QueryMetadata class
        state.record_query(query_metadata.to_dict())
        _record_timer_metrics(request, timer, query_metadata, result)
        _record_bytes_scanned_metrics(query_metadata, result)
        _record_cogs(request, query_metadata, result)
        _add_tags(timer, extra_data.get("experiments"), query_metadata)


def _add_tags(
    timer: Timer,
    experiments: Optional[Mapping[str, Any]] = None,
    metadata: Optional[SnubaQueryMetadata] = None,
) -> None:
    if Hub.current.scope.span:
        duration_group = timer.get_duration_group()
        sentry_sdk.set_tag("duration_group", duration_group)
        if duration_group == ">30s":
            sentry_sdk.set_tag("timeout", "too_long")
        if experiments is not None:
            for name, value in experiments.items():
                sentry_sdk.set_tag(name, str(value))
        if metadata is not None:
            for query_data in metadata.query_list:
                max_threads = query_data.stats.get("max_threads")
                if max_threads is not None:
                    sentry_sdk.set_tag("max_threads", max_threads)
                    break


def _build_failed_request_dict(
    request_id: str,
    body: dict[str, Any],
    dataset: str,
    organization: int,
    request_status: Status,
    referrer: Optional[str],
    exception_name: str | None = None,
) -> snuba_queries_v1.Querylog:
    return {
        "request": {
            "id": request_id,
            "body": body,
            "referrer": str(referrer),
            "team": None,
            "app_id": "none",
            "feature": None,
        },
        "dataset": dataset,
        "entity": "error",
        "start_timestamp": None,
        "end_timestamp": None,
        "status": request_status.status.value,
        "request_status": request_status.status.value,
        "slo": request_status.slo.value,
        "projects": [],
        "query_list": [],
        "timing": {"timestamp": int(time.time()), "duration_ms": 0, "tags": {}},
        "snql_anonymized": "",
        "organization": organization,
    }


def record_invalid_request(
    request_id: str,
    body: dict[str, Any],
    dataset: str,
    organization: int,
    timer: Timer,
    request_status: Status,
    referrer: Optional[str],
    exception_name: str | None = None,
) -> None:
    """
    Records a failed request before the request object is created, so
    it records failures during parsing/validation.
    This is for client errors.
    """
    _record_failure_metric_with_status(
        QueryStatus.INVALID_REQUEST, request_status, timer, referrer, exception_name
    )
    state.record_query(
        _build_failed_request_dict(
            request_id,
            body,
            dataset,
            organization,
            request_status,
            referrer,
            exception_name,
        )
    )


def record_error_building_request(
    request_id: str,
    body: dict[str, Any],
    dataset: str,
    organization: int,
    timer: Timer,
    request_status: Status,
    referrer: Optional[str],
    exception_name: str | None = None,
) -> None:
    """
    Records a failed request before the request object is created, so
    it records failures during parsing/validation.
    This is for system errors during parsing/validation.
    """
    _record_failure_metric_with_status(
        QueryStatus.ERROR, request_status, timer, referrer, exception_name
    )
    state.record_query(
        _build_failed_request_dict(
            request_id,
            body,
            dataset,
            organization,
            request_status,
            referrer,
            exception_name,
        )
    )


def _record_failure_metric_with_status(
    status: QueryStatus,
    request_status: Status,
    timer: Timer,
    referrer: Optional[str],
    exception_name: str | None = None,
) -> None:
    # TODO: Revisit if recording some data for these queries in the querylog
    # table would be useful.
    if settings.RECORD_QUERIES:
        timer.send_metrics_to(
            metrics,
            tags={
                "status": status.value,
                "referrer": referrer or "none",
                "request_status": request_status.status.value,
                "slo": request_status.slo.value,
                "exception": exception_name or "none",
            },
        )
        _add_tags(timer)
