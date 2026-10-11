from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import DuplicateError, NotFoundError
from core.models import Product
from core.serializers import (
    InventoryUpdateSerializer,
    ProductCreateSerializer,
    ProductSerializer,
    ProductUpdateSerializer,
)


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


class ProductListCreateView(generics.ListCreateAPIView):
    """GET /api/products/ (paginated, filtered) and POST /api/products/."""

    def get_queryset(self):
        qs = Product.objects.all()
        search = self.request.query_params.get("search")
        category = self.request.query_params.get("category")
        vehicle = self.request.query_params.get("vehicle")
        if search:
            qs = qs.filter(
                Q(sku__icontains=search)
                | Q(name__icontains=search)
                | Q(brand__icontains=search)
            )
        if category:
            qs = qs.filter(category=category)
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ProductCreateSerializer
        return ProductSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sku = serializer.validated_data["sku"]
        if Product.objects.filter(pk=sku).exists():
            raise DuplicateError(f"Product with SKU '{sku}' already exists.")
        try:
            with transaction.atomic():
                product = serializer.save()
        except IntegrityError:
            raise DuplicateError(f"Product with SKU '{sku}' already exists.")
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)


class ProductDetailView(generics.RetrieveUpdateAPIView):
    """GET /api/products/{sku}/ and PATCH /api/products/{sku}/. PUT is not in the contract."""

    http_method_names = ["get", "patch", "head", "options"]
    queryset = Product.objects.all()
    lookup_field = "sku"
    lookup_url_kwarg = "sku"

    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return ProductUpdateSerializer
        return ProductSerializer

    def get_object(self):
        sku = self.kwargs["sku"]
        try:
            return Product.objects.get(pk=sku)
        except Product.DoesNotExist:
            raise NotFoundError(f"Product '{sku}' not found.")

    def update(self, request, *args, **kwargs):
        product = self.get_object()
        serializer = self.get_serializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        product = serializer.save()
        return Response(ProductSerializer(product).data)


class InventoryUpdateView(APIView):
    """PUT /api/inventory/{sku}/ — set available quantity exactly (contract §5)."""

    def put(self, request, sku):
        try:
            product = Product.objects.get(pk=sku)
        except Product.DoesNotExist:
            raise NotFoundError(f"Product '{sku}' not found.")
        serializer = InventoryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product.stock = serializer.validated_data["quantity"]
        product.save(update_fields=["stock"])
        return Response({"sku": product.sku, "quantity": product.stock})
