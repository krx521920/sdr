from django.urls import include, path

from integrations.api.website_views import WebsiteLeadIntakeView

urlpatterns = [
    path("api/integrations/", include("integrations.urls")),
    path("api/sdr/intake/website/", WebsiteLeadIntakeView.as_view()),
]
