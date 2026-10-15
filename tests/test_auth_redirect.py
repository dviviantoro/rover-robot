"""Where a successful sign-in sends you.

`next` is a query parameter and then a form field -- whatever anyone wrote into a link.
Used unchecked it made `/login?next=//evil.example` sign a person in and then hand them
to another site, on the one page whose job is to ask for a password.
"""
from __future__ import annotations

import pytest

from robot.ui.auth import LANDING, safe_next


class TestWhereSigningInSendsYou:
    @pytest.mark.parametrize("value", ["/memory", "/chat", "/knowledge?tab=docs", "/a/b/c"])
    def test_a_path_on_this_server_is_kept(self, value):
        assert safe_next(value) == value

    @pytest.mark.parametrize("value", [
        "https://evil.example",
        "http://evil.example/login",
        "//evil.example",                  # protocol-relative: same scheme, other host
        "///evil.example",
        "/\\evil.example",                 # browsers read the backslash as a slash
        "\\\\evil.example",
        "javascript:alert(1)",
        "evil.example",
        "/ok\r\nSet-Cookie: x=1",          # a header, if anything downstream forgets
        "/ok\x00",
    ])
    def test_anything_that_leaves_the_server_goes_to_the_dashboard(self, value):
        assert safe_next(value) == LANDING

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_nothing_goes_to_the_dashboard(self, value):
        assert safe_next(value) == LANDING

    def test_the_public_landing_page_is_not_where_you_land_after_signing_in(self):
        """Signing in is a request to get in. `/` is public, so it would be a no-op."""
        assert safe_next("/") == LANDING

    @pytest.mark.parametrize("value", ["/login", "/login?error=1", "/logout", "/login/"])
    def test_it_never_sends_you_back_to_the_form_or_straight_out_again(self, value):
        assert safe_next(value) == LANDING

    def test_the_landing_target_is_a_page_that_exists(self):
        assert LANDING == "/dashboard"
