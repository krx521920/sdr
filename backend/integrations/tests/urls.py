from django.urls import include, path

from integrations.api.website_views import WebsiteLeadIntakeView

urlpatterns = [
    path("api/cases/", include("cases.urls")),
    path("api/integrations/", include("integrations.urls")),
    path("api/sdr/intake/website/", WebsiteLeadIntakeView.as_view()),
    path("api/sdr/", include("sdr.urls")),
]
