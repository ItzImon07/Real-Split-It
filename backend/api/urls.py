from django.urls import path
from .views import ProcessReceiptView

urlpatterns = [
    path('process-receipt/', ProcessReceiptView.as_view(), name='process-receipt'),
]
