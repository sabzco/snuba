from snuba.admin.clickhouse.common import InvalidCustomQuery, get_ro_cluster_connection
from snuba.clickhouse.native import ClickhouseResult


def validate_trace_query(sql_query: str) -> None:
    """
    Simple validation to ensure query only attempts read queries.

    Raises InvalidCustomQuery if query is invalid or not allowed.
    """
    sql_query = " ".join(sql_query.split())
    lowered = sql_query.lower().strip()

    if not lowered.startswith("select"):
        raise InvalidCustomQuery("Only SELECT queries are allowed")

    disallowed_keywords = ["insert", ";"]
    for kw in disallowed_keywords:
        if kw in lowered:
            raise InvalidCustomQuery(f"{kw} is not allowed in the query")


def run_query_and_get_trace(storage_name: str, query: str) -> ClickhouseResult:
    validate_trace_query(query)
    connection = get_ro_cluster_connection(storage_name)
    query_result = connection.execute(query=query, capture_trace=True)
    return query_result
