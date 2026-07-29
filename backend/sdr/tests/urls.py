from django.urls import include, path

urlpatterns = [
    path("api/sdr/", include("sdr.urls")),
]
