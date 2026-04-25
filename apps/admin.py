from django.contrib import admin, messages
from django.db.models import Q
import csv

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse

from .forms import CardExportForm, CardImportForm
from .models import Card, Error, Transfer
from .utils import (
    build_card_export_rows,
    build_cards_xlsx_bytes,
    display_card,
    display_phone,
    normalize_card_row,
    parse_card_rows,
)


class HasPhoneFilter(admin.SimpleListFilter):
    title = "phone"
    parameter_name = "phone_state"

    def lookups(self, request, model_admin):
        return (("with_phone", "With phone"), ("without_phone", "Without phone"))

    def queryset(self, request, queryset):
        if self.value() == "with_phone":
            return queryset.exclude(Q(phone__isnull=True) | Q(phone=""))
        if self.value() == "without_phone":
            return queryset.filter(Q(phone__isnull=True) | Q(phone=""))
        return queryset


class BalanceFilter(admin.SimpleListFilter):
    title = "balance"
    parameter_name = "balance_range"

    def lookups(self, request, model_admin):
        return (
            ("zero", "0"),
            ("small", "0.01 - 1,000,000"),
            ("medium", "1,000,000 - 100,000,000"),
            ("large", "100,000,000+"),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "zero":
            return queryset.filter(balance=0)
        if value == "small":
            return queryset.filter(balance__gt=0, balance__lt=1_000_000)
        if value == "medium":
            return queryset.filter(balance__gte=1_000_000, balance__lt=100_000_000)
        if value == "large":
            return queryset.filter(balance__gte=100_000_000)
        return queryset


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    change_list_template = "admin/apps/card/change_list.html"
    list_display = ("formatted_card_number", "formatted_phone", "status", "expire", "balance")
    list_filter = ("status", "expire", HasPhoneFilter, BalanceFilter)
    search_fields = ("card_number", "phone")
    ordering = ("card_number",)
    list_per_page = 100

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-excel/",
                self.admin_site.admin_view(self.import_excel_view),
                name="apps_card_import_excel",
            ),
            path(
                "export/",
                self.admin_site.admin_view(self.export_cards_view),
                name="apps_card_export",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Card Number")
    def formatted_card_number(self, obj: Card) -> str:
        return display_card(obj.card_number)

    @admin.display(description="Phone")
    def formatted_phone(self, obj: Card) -> str:
        if not obj.phone:
            return "-"
        return display_phone(obj.phone)

    def import_excel_view(self, request: HttpRequest):
        if request.method == "POST":
            form = CardImportForm(request.POST, request.FILES)
            if form.is_valid():
                uploaded_file = form.cleaned_data["file"]
                try:
                    rows = parse_card_rows(uploaded_file.name, uploaded_file.read())
                except Exception as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                    rows = []
                created_count = 0
                updated_count = 0
                errors: list[str] = []
                for index, row in enumerate(rows, start=2):
                    try:
                        normalized = normalize_card_row(row)
                        _, created = Card.objects.update_or_create(
                            card_number=normalized["card_number"],
                            defaults=normalized,
                        )
                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                    except Exception as exc:
                        errors.append(f"Row {index}: {exc}")
                if created_count or updated_count:
                    self.message_user(
                        request,
                        f"Import finished. Created: {created_count}, Updated: {updated_count}.",
                        messages.SUCCESS,
                    )
                for error in errors[:20]:
                    self.message_user(request, error, messages.ERROR)
                if len(errors) > 20:
                    self.message_user(
                        request,
                        f"{len(errors) - 20} more invalid rows were skipped.",
                        messages.WARNING,
                    )
                return HttpResponseRedirect(reverse("admin:apps_card_changelist"))
        else:
            form = CardImportForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Import cards from Excel",
            "form": form,
        }
        return render(request, "admin/apps/card/import_cards.html", context)

    def export_cards_view(self, request: HttpRequest) -> HttpResponse:
        selected_status = request.GET.get("status__exact", "")
        base_queryset = self.get_queryset(request)
        if request.method == "POST":
            form = CardExportForm(request.POST)
            if form.is_valid():
                selected_status = form.cleaned_data["status"] or request.GET.get("status__exact", "")
                queryset = base_queryset
                if selected_status:
                    queryset = queryset.filter(status=selected_status)
                export_format = form.cleaned_data["export_format"]
                if export_format == "xlsx":
                    response = HttpResponse(
                        build_cards_xlsx_bytes(queryset, value_style="formatted"),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    response["Content-Disposition"] = 'attachment; filename="cards_export.xlsx"'
                    return response

                response = HttpResponse(content_type="text/csv")
                response["Content-Disposition"] = 'attachment; filename="cards_export.csv"'
                writer = csv.writer(response)
                for row in build_card_export_rows(queryset, value_style="formatted"):
                    writer.writerow(row)
                return response
        else:
            form = CardExportForm(initial={"status": selected_status})

        queryset = base_queryset
        if selected_status:
            queryset = queryset.filter(status=selected_status)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Export cards",
            "form": form,
            "cards_count": queryset.count(),
        }
        return render(request, "admin/apps/card/export_cards.html", context)


@admin.register(Error)
class ErrorAdmin(admin.ModelAdmin):
    list_display = ("code", "en", "ru", "uz")
    search_fields = ("code", "en", "ru", "uz")
    ordering = ("code",)


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = (
        "ext_id",
        "sender_card_number",
        "receiver_card_number",
        "sending_amount",
        "currency",
        "state",
        "created_at",
    )
    list_filter = ("state", "currency", "created_at")
    search_fields = ("ext_id", "sender_card_number", "receiver_card_number", "sender_phone", "receiver_phone")
