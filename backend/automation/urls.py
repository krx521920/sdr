from django.urls import path

from automation.views import (
    AutomationJobDetailView,
    AutomationJobListView,
    AutomationJobRetryView,
)

app_name = "api_automation"

urlpatterns = [
    path("jobs/", AutomationJobListView.as_view(), name="job_list"),
    path("jobs/<uuid:job_id>/", AutomationJobDetailView.as_view(), name="job_detail"),
    path(
        "jobs/<uuid:job_id>/retry/",
        AutomationJobRetryView.as_view(),
        name="job_retry",
    ),
]
