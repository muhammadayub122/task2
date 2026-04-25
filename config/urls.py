from django.contrib import admin
from django.urls import include, path

from apps.rpc import jsonrpc_endpoint

urlpatterns = [
    path("admin/", admin.site.urls),
    path("rpc/", jsonrpc_endpoint, name="jsonrpc-endpoint"),
    path("cards/", include("apps.urls")),
]
