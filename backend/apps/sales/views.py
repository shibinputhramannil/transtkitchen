from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.menu.models import MenuItem, MenuItemAlias, SaleSource
from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from .csv_import import TEMPLATE, ImportFileError, parse_sales_csv, read_upload
from .models import ImportStatus, SaleLine, SalesImport
from .serializers import ApplySerializer, MappingSerializer, SaleLineSerializer, SalesImportSerializer
from .services import ApplyRefused, apply_import, build_preview, daily_sales, default_report_range, resolve_lines

MAX_REPORT_DAYS = 92


class SalesScopedMixin(OrganizationScopedMixin):
    """Sales of the requester's organization; staff read, managers import."""

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.MANAGER

    def imports(self):
        return SalesImport.objects.filter(organization=self.organization)

    def preview_response(self, sales_import, status_code=status.HTTP_200_OK, **extra):
        """Read-only: describes the import as stored. Re-matching is POST .../rematch/."""
        return Response(
            {**SalesImportSerializer(sales_import).data, **build_preview(sales_import), **extra},
            status=status_code,
        )


class SalesImportListCreateView(SalesScopedMixin, generics.ListAPIView):
    """
    Lists imports, newest first. POST uploads a CSV file (form field "file")
    and returns the draft's preview. The same file twice returns the existing
    draft, or 409 if it was already applied.
    """

    serializer_class = SalesImportSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        return self.imports().select_related("created_by", "applied_by")

    def post(self, request, *args, **kwargs):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": ["Choose a CSV file to upload."]})
        try:
            data = read_upload(upload)
            parsed = parse_sales_csv(
                data, ZoneInfo(self.organization.time_zone), self.organization.local_today()
            )
        except ImportFileError as error:
            raise ValidationError({"file": [str(error)]}) from None

        existing = self.existing_import(parsed.sha256)
        if existing is not None:
            return self.duplicate_response(existing)

        valid_dates = [row.business_date for row in parsed.rows if not row.errors]
        try:
            with transaction.atomic():
                sales_import = SalesImport.objects.create(
                    organization=self.organization,
                    source=SaleSource.CSV,
                    source_reference=parsed.sha256,
                    file_name=upload.name[:255],
                    row_count=len(parsed.rows),
                    first_date=min(valid_dates, default=None),
                    last_date=max(valid_dates, default=None),
                    created_by=request.user,
                )
                SaleLine.objects.bulk_create(
                    SaleLine(
                        sales_import=sales_import,
                        row_number=row.row_number,
                        business_date=row.business_date if not row.errors else None,
                        sold_at=row.sold_at if not row.errors else None,
                        item_name=row.item_name,
                        external_id=row.external_id,
                        quantity=row.quantity if not row.errors else None,
                        error=row.error,
                    )
                    for row in parsed.rows
                )
                # Match the new rows once, as part of creating the draft.
                resolve_lines(sales_import)
        except IntegrityError:
            # The same file was uploaded at the same moment.
            return self.duplicate_response(self.existing_import(parsed.sha256))
        return self.preview_response(sales_import, status.HTTP_201_CREATED)

    def existing_import(self, sha256):
        return (
            self.imports()
            .filter(source=SaleSource.CSV, source_reference=sha256)
            .exclude(status=ImportStatus.DISCARDED)
            .first()
        )

    def duplicate_response(self, existing):
        if existing.status == ImportStatus.APPLIED:
            return Response(
                {
                    "detail": f"This file was already imported and applied on {existing.applied_at:%Y-%m-%d}.",
                    "import": str(existing.pk),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return self.preview_response(existing, duplicate=True)


class SalesImportDetailView(SalesScopedMixin, APIView):
    """The import with its preview: what applying it would do (or did)."""

    def get(self, request, *args, **kwargs):
        return self.preview_response(get_object_or_404(self.imports(), pk=kwargs["pk"]))


class SalesImportRowsView(SalesScopedMixin, generics.ListAPIView):
    """The import's rows. ?status=error|unmatched|ignored|matched filters them."""

    serializer_class = SaleLineSerializer
    FILTERS = {
        "error": lambda rows: rows.exclude(error=""),
        "unmatched": lambda rows: rows.filter(error="", ignored=False, menu_item__isnull=True),
        "ignored": lambda rows: rows.filter(ignored=True),
        "matched": lambda rows: rows.filter(error="", ignored=False, menu_item__isnull=False),
    }

    def get_queryset(self):
        sales_import = get_object_or_404(self.imports(), pk=self.kwargs["pk"])
        rows = sales_import.lines.select_related("menu_item")
        wanted = self.request.query_params.get("status")
        if wanted:
            if wanted not in self.FILTERS:
                raise ValidationError({"status": [f"Use one of: {', '.join(self.FILTERS)}."]})
            rows = self.FILTERS[wanted](rows)
        return rows


class MappingView(SalesScopedMixin, generics.GenericAPIView):
    """
    Maps every row with an item name (ignoring case) to a menu item and
    remembers it for future imports, or ignores those rows in this import.
    """

    serializer_class = MappingSerializer

    def post(self, request, *args, **kwargs):
        get_object_or_404(self.imports(), pk=kwargs["pk"])
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            sales_import = get_object_or_404(self.imports().select_for_update(), pk=kwargs["pk"])
            if sales_import.status != ImportStatus.DRAFT:
                return Response({"detail": "Only draft imports can change."}, status=status.HTTP_409_CONFLICT)
            rows = sales_import.lines.filter(error="", item_name__iexact=data["item_name"])
            if not rows.exists():
                raise ValidationError({"item_name": ["No rows in this import have that item name."]})

            if data["ignore"]:
                rows.update(ignored=True, menu_item=None)
            else:
                menu_item = MenuItem.objects.filter(
                    organization=self.organization, pk=data["menu_item"]
                ).first()
                if menu_item is None:
                    raise ValidationError({"menu_item": ["No such menu item."]})
                rows.update(ignored=False)
                MenuItemAlias.objects.update_or_create(
                    organization=self.organization,
                    source=sales_import.source,
                    external_name__iexact=data["item_name"],
                    defaults={"menu_item": menu_item},
                    create_defaults={"menu_item": menu_item, "external_name": data["item_name"]},
                )
                # Re-match only the rows this mapping is about.
                resolve_lines(sales_import, item_name=data["item_name"])
        return self.preview_response(sales_import)


class RematchView(SalesScopedMixin, APIView):
    """
    Matches a draft's rows again against the current menu items and remembered
    names (managers and owners). Ignored rows stay ignored. Safe to repeat: a
    second call finds nothing more to change. Returns the preview and how many
    rows changed ("rematched").
    """

    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            sales_import = get_object_or_404(self.imports().select_for_update(), pk=kwargs["pk"])
            if sales_import.status != ImportStatus.DRAFT:
                return Response({"detail": "Only draft imports can change."}, status=status.HTTP_409_CONFLICT)
            changed = resolve_lines(sales_import)
        return self.preview_response(sales_import, rematched=len(changed))


class ApplyView(SalesScopedMixin, generics.GenericAPIView):
    """
    Applies a draft import: records its sales and deducts their ingredients
    from stock, earliest expiry first, all in one transaction. Each warning in
    the preview must be listed in "acknowledged". Applying again returns the
    result instead of deducting twice.
    """

    serializer_class = ApplySerializer

    def post(self, request, *args, **kwargs):
        get_object_or_404(self.imports(), pk=kwargs["pk"])
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sales_import, applied_now = apply_import(
                self.organization, kwargs["pk"], request.user, set(serializer.validated_data["acknowledged"])
            )
        except ApplyRefused as refusal:
            body = {"detail": refusal.detail}
            if refusal.missing:
                body["missing_acknowledgements"] = refusal.missing
            if refusal.preview is not None:
                body["preview"] = refusal.preview
            return Response(body, status=refusal.status)
        return self.preview_response(sales_import, applied_now=applied_now)


class DiscardView(SalesScopedMixin, APIView):
    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            sales_import = get_object_or_404(self.imports().select_for_update(), pk=kwargs["pk"])
            if sales_import.status == ImportStatus.APPLIED:
                return Response(
                    {"detail": "Applied imports cannot be discarded."}, status=status.HTTP_409_CONFLICT
                )
            if sales_import.status == ImportStatus.DRAFT:
                sales_import.status = ImportStatus.DISCARDED
                sales_import.save(update_fields=["status", "updated_at"])
        return Response(SalesImportSerializer(sales_import).data)


class TemplateView(SalesScopedMixin, APIView):
    def get(self, request, *args, **kwargs):
        response = HttpResponse(TEMPLATE, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="sales-template.csv"'
        return response


class DailySalesView(SalesScopedMixin, APIView):
    """
    Quantity sold per menu item per business date (kitchen-local), from
    applied imports. ?from= and ?to= (YYYY-MM-DD) default to the last 7 days.
    """

    def get(self, request, *args, **kwargs):
        start, end = default_report_range(self.organization)
        try:
            if "from" in request.query_params:
                start = date.fromisoformat(request.query_params["from"])
            if "to" in request.query_params:
                end = date.fromisoformat(request.query_params["to"])
        except ValueError:
            raise ValidationError({"detail": "Dates must be YYYY-MM-DD."}) from None
        if end < start or end - start >= timedelta(days=MAX_REPORT_DAYS):
            raise ValidationError({"detail": f"Choose a range of 1 to {MAX_REPORT_DAYS} days."})
        rows = daily_sales(self.organization, start, end)
        return Response(
            {
                "from": start,
                "to": end,
                "results": [
                    {
                        "business_date": row["business_date"],
                        "menu_item": {"id": str(row["menu_item"]), "name": row["menu_item__name"]},
                        "quantity": f"{row['quantity']:.3f}",
                    }
                    for row in rows
                ],
            }
        )
