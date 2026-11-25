import unittest
from unittest.mock import patch

from app.github_actions_client import (
    GitHubActionsBridgeError,
    _trusted_oidc_request_host,
    _request_actions_oidc_token,
)


class GitHubActionsClientTests(unittest.TestCase):
    def test_accepts_github_regional_oidc_hosts(self):
        for hostname in (
            "pipelines.actions.githubusercontent.com",
            "pipelinesghubeus6.actions.githubusercontent.com",
            "pipelinesghuwestus3.actions.githubusercontent.com",
            "actions.githubusercontent.com",
        ):
            with self.subTest(hostname=hostname):
                self.assertTrue(_trusted_oidc_request_host(hostname))

    def test_rejects_untrusted_or_lookalike_oidc_hosts(self):
        for hostname in (
            None, "", "evil.example",
            "actions.githubusercontent.com.evil.example",
            "githubusercontent.com", "notactions.githubusercontent.com",
        ):
            with self.subTest(hostname=hostname):
                self.assertFalse(_trusted_oidc_request_host(hostname))

    def test_rejects_untrusted_request_url(self):
        with (
            patch.dict("os.environ", {
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://evil.example/token?secret=hidden",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "hidden-token",
            }),
            self.assertRaises(GitHubActionsBridgeError),
        ):
            _request_actions_oidc_token()


if __name__ == "__main__":
    unittest.main()
