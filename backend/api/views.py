"""
views.py

Exposes the receipt-processing pipeline as a REST endpoint.

The heavy lifting (reading the image, extracting items, categorizing them,
detecting tax/service/tip) now happens in one Gemini vision call via
ai_service.extract_receipt() — see that module's docstring for why the
old OpenCV/EasyOCR/regex pipeline was replaced.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser
from rest_framework import status

from .ai_service import extract_receipt


class ProcessReceiptView(APIView):
    """
    POST /api/process-receipt/

    Accepts a multipart/form-data upload with a single file field
    named "receipt_image". Returns parsed menu items (with AI category
    tags) plus any detected tax/service/tip charges.

    Example success response:
        {
            "success": true,
            "items": [
                {"name": "Chicken Biryani", "price": 250.0, "tag": "staples"}
            ],
            "item_count": 1,
            "charges": {"tax": 24.5, "service": 0.0, "tip": 0.0}
        }
    """
    parser_classes = [MultiPartParser]

    def post(self, request, *args, **kwargs):
        uploaded_file = request.FILES.get('receipt_image')

        if uploaded_file is None:
            return Response(
                {"success": False, "error": "No file provided. Expected form-data key 'receipt_image'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not uploaded_file.content_type.startswith('image/'):
            return Response(
                {"success": False, "error": "Uploaded file is not an image."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            image_bytes = uploaded_file.read()
            result = extract_receipt(image_bytes, mime_type=uploaded_file.content_type)
        except RuntimeError as e:
            return Response(
                {"success": False, "error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception as e:
            return Response(
                {"success": False, "error": f"Processing failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        items = result.get("items", [])
        charges = result.get("charges", {"tax": 0.0, "service": 0.0, "tip": 0.0})

        return Response(
            {
                "success": True,
                "items": items,
                "item_count": len(items),
                "charges": charges,
            },
            status=status.HTTP_200_OK,
        )