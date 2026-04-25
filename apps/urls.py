from django.urls import path
from .views import import_view, export_view

urlpatterns = [
    path("import/", import_view, name="import_cards"),
    path("export/", export_view, name="export_cards"),
]