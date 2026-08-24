from django.urls import include, path

urlpatterns = [
    path("api/matching/", include("matching.urls")),
]
