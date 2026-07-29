from django.urls import include, path

urlpatterns = [
    path("api/automation/", include("automation.urls")),
]
