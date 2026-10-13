from django.urls import path

from core.views_forecast import ForecastView, ReorderSuggestionsView

urlpatterns = [
    path("forecast/<str:sku>/", ForecastView.as_view(), name="forecast-detail"),
    path("reorder-suggestions/", ReorderSuggestionsView.as_view(), name="reorder-suggestions"),
]
