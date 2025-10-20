# core/pagination.py
from rest_framework.pagination import PageNumberPagination

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25                 # default page size
    page_size_query_param = "page_size"  # allow client to change (optional)
    max_page_size = 200
    page_query_param = "page"      # ?page=2
