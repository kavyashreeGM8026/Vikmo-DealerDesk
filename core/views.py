from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import APIError, DuplicateError, NotFoundError
from core.models import Dealer, Order, Product
from core.serializers import (
    MAX_ID,
    DealerSerializer,
    InventoryUpdateSerializer,
    OrderCreateSerializer,
    OrderOutputSerializer,
    ProductCreateSerializer,
    ProductSerializer,
    ProductUpdateSerializer,
)
from core.services.orders import create_draft_order, get_order, order_queryset, present_order


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
        # The row is locked (SELECT ... FOR UPDATE) until the transaction ends, so a manual
        # stock update can never interleave with an order confirmation touching the same
        # product. Behaviour and response are unchanged: 404 first, then 400, then 200.
        with transaction.atomic():
            try:
                product = Product.objects.select_for_update().get(pk=sku)
            except Product.DoesNotExist:
                raise NotFoundError(f"Product '{sku}' not found.")
            serializer = InventoryUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            product.stock = serializer.validated_data["quantity"]
            product.save(update_fields=["stock"])
        return Response({"sku": product.sku, "quantity": product.stock})


class DealerListCreateView(generics.ListCreateAPIView):
    """GET /api/dealers/ (paginated) and POST /api/dealers/ (contract section 6)."""

    queryset = Dealer.objects.all()  # Dealer.Meta.ordering = ["id"] keeps pages stable
    serializer_class = DealerSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["dealer_code"]
        if Dealer.objects.filter(dealer_code=code).exists():
            raise DuplicateError(f"Dealer with code '{code}' already exists.")
        try:
            with transaction.atomic():
                dealer = serializer.save()
        except IntegrityError:
            # Two identical requests raced past the exists() check; the DB unique index wins.
            raise DuplicateError(f"Dealer with code '{code}' already exists.")
        return Response(DealerSerializer(dealer).data, status=status.HTTP_201_CREATED)


class DealerDetailView(generics.RetrieveAPIView):
    """GET /api/dealers/{id}/. Only GET is in the contract."""

    http_method_names = ["get", "head", "options"]
    queryset = Dealer.objects.all()
    serializer_class = DealerSerializer

    def get_object(self):
        raw_id = self.kwargs["pk"]
        # Anything that is not a plain positive integer that fits the id column cannot exist.
        if not (raw_id.isascii() and raw_id.isdigit()) or int(raw_id) > 2**63 - 1:
            raise NotFoundError(f"Dealer '{raw_id}' not found.")
        try:
            return Dealer.objects.get(pk=int(raw_id))
        except Dealer.DoesNotExist:
            raise NotFoundError(f"Dealer '{raw_id}' not found.")


def _filter_error(field, message):
    return APIError(message, [{"field": field, "message": message}])


class OrderListCreateView(generics.GenericAPIView):
    """POST /api/orders/ (create a DRAFT) and GET /api/orders/ (paginated, filterable)."""

    def get_queryset(self):
        qs = order_queryset()  # ordered newest first (Order.Meta.ordering), items prefetched
        params = self.request.query_params

        dealer_id = params.get("dealer_id")
        if dealer_id:
            if not (dealer_id.isascii() and dealer_id.isdigit()) or int(dealer_id) > MAX_ID:
                raise _filter_error("dealer_id", "dealer_id must be a positive integer.")
            qs = qs.filter(dealer_id=int(dealer_id))

        status_filter = params.get("status")
        if status_filter:
            if status_filter not in Order.Status.values:
                allowed = ", ".join(Order.Status.values)
                raise _filter_error("status", f"status must be one of: {allowed}.")
            qs = qs.filter(status=status_filter)
        return qs

    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        data = OrderOutputSerializer([present_order(order) for order in page], many=True).data
        return self.get_paginated_response(data)

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = create_draft_order(
            serializer.validated_data["dealer"], serializer.validated_data["lines"]
        )
        return Response(OrderOutputSerializer(present_order(order)).data, status=status.HTTP_201_CREATED)


class OrderDetailView(APIView):
    """GET /api/orders/{order_number}/. (PUT arrives in Stage 2d-2.)"""

    def get(self, request, order_number):
        order = get_order(order_number)
        return Response(OrderOutputSerializer(present_order(order)).data)
