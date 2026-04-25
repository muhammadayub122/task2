from django.shortcuts import render
from .forms import CardImportForm, CardExportForm
from .services import import_cards, export_cards


def import_view(request):
    if request.method == "POST":
        form = CardImportForm(request.POST, request.FILES)
        if form.is_valid():
            result = import_cards(request.FILES["file"])
            return render(request, "result.html", {"result": result})
    else:
        form = CardImportForm()

    return render(request, "import.html", {"form": form})


def export_view(request):
    form = CardExportForm(request.GET)

    if form.is_valid():
        return export_cards(
            form.cleaned_data["export_format"],
            form.cleaned_data["status"],
            "formatted",
        )

    return render(request, "export.html", {"form": form})
