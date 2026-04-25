from django import forms
from .models import CardStatus


class CardImportForm(forms.Form):
    file = forms.FileField(
        help_text="CSV, Excel (.xlsx) yoki JSON yuklang. Status ustuni bo'lmasa avtomatik active olinadi."
    )

    def clean_file(self):
        file = self.cleaned_data["file"]
        if file.size > 5 * 1024 * 1024:
            raise forms.ValidationError("File juda katta (max 5MB)")
        return file


class CardExportForm(forms.Form):
    EXPORT_FORMAT_CHOICES = (
        ("csv", "CSV"),
        ("xlsx", "Excel (.xlsx)"),
        ("json", "JSON"),
    )

    export_format = forms.ChoiceField(
        choices=EXPORT_FORMAT_CHOICES,
        initial="xlsx"
    )

    status = forms.ChoiceField(
        choices=[("", "All statuses"), *CardStatus.choices],
        required=False
    )
