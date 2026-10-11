from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """`?page=N` (1-based) and `?page_size=N` (default 20, capped at 100).

    Responses look like {"count", "next", "previous", "results"}, as the contract requires.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
