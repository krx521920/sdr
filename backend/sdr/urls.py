from django.urls import path

from sdr.api.views import WebsiteLeadIntakeView

app_name = "api_sdr"

urlpatterns = [
    path("intake/website/", WebsiteLeadIntakeView.as_view(), name="website_intake"),
]
