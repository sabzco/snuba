import base64
from datetime import UTC, datetime, timedelta

import pytest
from sentry_protos.snuba.v1.endpoint_create_subscription_pb2 import (
    CreateSubscriptionRequest as CreateSubscriptionRequestProto,
)
from sentry_protos.snuba.v1.endpoint_create_subscription_pb2 import (
    CreateSubscriptionResponse,
)
from sentry_protos.snuba.v1.endpoint_time_series_pb2 import TimeSeriesRequest
from sentry_protos.snuba.v1.error_pb2 import Error
from sentry_protos.snuba.v1.request_common_pb2 import RequestMeta
from sentry_protos.snuba.v1.trace_item_attribute_pb2 import (
    AttributeAggregation,
    AttributeKey,
    ExtrapolationMode,
    Function,
)

from snuba.datasets.entities.entity_key import EntityKey
from snuba.redis import RedisClientKey, get_redis_client
from snuba.subscriptions.data import PartitionId, RPCSubscriptionData
from snuba.subscriptions.store import RedisSubscriptionDataStore
from tests.base import BaseApiTest
from tests.web.rpc.v1.test_endpoint_time_series.test_endpoint_time_series import (
    DummyMetric,
    store_timeseries,
)

END_TIME = datetime.utcnow().replace(second=0, microsecond=0, tzinfo=UTC)
START_TIME = END_TIME - timedelta(hours=1)


TESTS_INVALID_RPC_SUBSCRIPTIONS = [
    pytest.param(
        CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                ],
            ),
            time_window_secs=172800,
            resolution_secs=60,
        ),
        "Time window must be less than or equal to 24 hours",
        id="Invalid subscription: time window",
    ),
    pytest.param(
        CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1, 2, 3],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                ],
            ),
            time_window_secs=300,
            resolution_secs=60,
        ),
        "Multiple project IDs not supported",
        id="Invalid subscription: multiple project ids",
    ),
    pytest.param(
        CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                ],
            ),
            time_window_secs=300,
            resolution_secs=60,
        ),
        "Exactly one aggregation required",
        id="Invalid subscription: multiple aggregations",
    ),
    pytest.param(
        CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                group_by=[
                    AttributeKey(type=AttributeKey.TYPE_STRING, name="device.class")
                ],
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                ],
            ),
            time_window_secs=300,
            resolution_secs=60,
        ),
        "Group bys not supported",
        id="Invalid subscription: group by",
    ),
    pytest.param(
        CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_NONE,
                    ),
                ],
            ),
            time_window_secs=300,
            resolution_secs=60,
        ),
        "Invalid extrapolation mode",
        id="Invalid subscription: extrapolation mode",
    ),
]


@pytest.mark.clickhouse_db
@pytest.mark.redis_db
class TestCreateSubscriptionApi(BaseApiTest):
    def test_create_valid_subscription(self) -> None:
        store_timeseries(
            START_TIME,
            1,
            3600,
            metrics=[DummyMetric("test_metric", get_value=lambda x: 1)],
        )

        message = CreateSubscriptionRequestProto(
            time_series_request=TimeSeriesRequest(
                meta=RequestMeta(
                    project_ids=[1],
                    organization_id=1,
                    cogs_category="something",
                    referrer="something",
                ),
                aggregations=[
                    AttributeAggregation(
                        aggregate=Function.FUNCTION_SUM,
                        key=AttributeKey(
                            type=AttributeKey.TYPE_FLOAT, name="test_metric"
                        ),
                        label="sum",
                        extrapolation_mode=ExtrapolationMode.EXTRAPOLATION_MODE_SAMPLE_WEIGHTED,
                    ),
                ],
                granularity_secs=300,
            ),
            time_window_secs=300,
            resolution_secs=60,
        )
        response = self.app.post(
            "/rpc/CreateSubscriptionRequest/v1", data=message.SerializeToString()
        )
        assert response.status_code == 200
        response_class = CreateSubscriptionResponse()
        response_class.ParseFromString(response.data)
        assert response_class.subscription_id
        partition = int(response_class.subscription_id.split("/", 1)[0])

        rpc_subscription_data = list(
            RedisSubscriptionDataStore(
                get_redis_client(RedisClientKey.SUBSCRIPTION_STORE),
                EntityKey("eap_spans"),
                PartitionId(partition),
            ).all()
        )[0][1]

        assert isinstance(rpc_subscription_data, RPCSubscriptionData)

        request_class = TimeSeriesRequest()
        request_class.ParseFromString(
            base64.b64decode(rpc_subscription_data.time_series_request)
        )
        assert rpc_subscription_data.time_window_sec == 300
        assert rpc_subscription_data.resolution_sec == 60
        assert rpc_subscription_data.request_name == "TimeSeriesRequest"
        assert rpc_subscription_data.request_version == "v1"

    @pytest.mark.parametrize(
        "create_subscription, error_message", TESTS_INVALID_RPC_SUBSCRIPTIONS
    )
    def test_create_invalid_subscription(
        self, create_subscription: CreateSubscriptionRequestProto, error_message: str
    ) -> None:
        store_timeseries(
            START_TIME,
            1,
            3600,
            metrics=[DummyMetric("test_metric", get_value=lambda x: 1)],
        )

        response = self.app.post(
            "/rpc/CreateSubscriptionRequest/v1",
            data=create_subscription.SerializeToString(),
        )
        assert response.status_code == 500
        error = Error()
        error.ParseFromString(response.data)
        assert error_message in error.message
