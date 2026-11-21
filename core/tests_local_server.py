import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class LocalWaitressServerCommandTests(SimpleTestCase):
    @patch("waitress.serve")
    def test_serve_uses_safe_loopback_default_and_waitress(self, serve):
        output = io.StringIO()
        call_command("serve", stdout=output)
        self.assertIn("127.0.0.1:8000", output.getvalue())
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(serve.call_args.kwargs["port"], 8000)
        self.assertEqual(serve.call_args.kwargs["threads"], 8)
        self.assertEqual(serve.call_args.kwargs["ident"], "AutoZapaz")

    def test_serve_rejects_invalid_port_before_starting(self):
        with self.assertRaises(CommandError):
            call_command("serve", port=70000, stdout=io.StringIO())

    def test_serve_rejects_unreasonable_thread_count(self):
        with self.assertRaises(CommandError):
            call_command("serve", threads=256, stdout=io.StringIO())
