from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health),
    path("products/", views.ProductListCreateView.as_view()),
    path("products/<str:sku>/", views.ProductDetailView.as_view()),
    path("inventory/<str:sku>/", views.InventoryUpdateView.as_view()),
]
