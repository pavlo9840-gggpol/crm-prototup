import tempfile
from pathlib import Path

from django.test import TestCase, override_settings


class LocalMediaServingTests(TestCase):
    def test_uploaded_media_is_available_when_debug_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            (media_root / "products").mkdir()
            (media_root / "products" / "fixture.txt").write_text("product-photo-placeholder")
            with override_settings(DEBUG=False, MEDIA_ROOT=media_root):
                response = self.client.get("/media/products/fixture.txt")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"product-photo-placeholder")
