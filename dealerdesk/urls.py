from django.contrib import admin
from django.urls import include, path

# Unknown URLs and crashes answer in the JSON error shape (used when DEBUG is off)
handler404 = "common.views.not_found"
handler500 = "common.views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    # Add your apps' URLs here, e.g. path("api/", include("orders.urls")),
]
