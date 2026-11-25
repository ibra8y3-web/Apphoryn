import unittest
from unittest.mock import Mock, patch

from app.github_actions_oidc import (
    ALLOWED_REPOSITORY,
    AUDIENCE,
    ISSUER,
    GitHubActionsOIDCError,
    validate_github_actions_claims,
    verify_github_actions_oidc,
)


def valid_claims():
    return {
        "repository": ALLOWED_REPOSITORY,
        "ref": "refs/heads/main",
        "event_name": "schedule",
        "workflow_ref": f"{ALLOWED_REPOSITORY}/.github/workflows/harvest.yml@refs/heads/main",
        "run_id": "123456",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"repo:{ALLOWED_REPOSITORY}:ref:refs/heads/main",
    }


class GitHubActionsOIDCTests(unittest.TestCase):
    def test_accepts_approved_main_branch_catalog_workflow(self):
        self.assertEqual(validate_github_actions_claims(valid_claims())["run_id"], "123456")

    def test_rejects_another_repository(self):
        claims = valid_claims()
        claims["repository"] = "someone-else/repository"
        with self.assertRaises(GitHubActionsOIDCError):
            validate_github_actions_claims(claims)

    def test_rejects_non_main_refs_and_pull_requests(self):
        for field, value in (("ref", "refs/heads/feature"), ("event_name", "pull_request")):
            claims = valid_claims()
            claims[field] = value
            with self.subTest(field=field), self.assertRaises(GitHubActionsOIDCError):
                validate_github_actions_claims(claims)

    def test_rejects_a_different_workflow(self):
        claims = valid_claims()
        claims["workflow_ref"] = f"{ALLOWED_REPOSITORY}/.github/workflows/other.yml@refs/heads/main"
        with self.assertRaises(GitHubActionsOIDCError):
            validate_github_actions_claims(claims)

    def test_verifies_signature_issuer_and_audience_before_claim_validation(self):
        key_client = Mock()
        key_client.get_signing_key_from_jwt.return_value.key = "verified-key"
        with (
            patch("app.github_actions_oidc._jwks_client", key_client),
            patch("app.github_actions_oidc.jwt.decode", return_value=valid_claims()) as decode,
        ):
            claims = verify_github_actions_oidc("signed-token")
        self.assertEqual(claims["repository"], ALLOWED_REPOSITORY)
        decode.assert_called_once()
        self.assertEqual(decode.call_args.kwargs["audience"], AUDIENCE)
        self.assertEqual(decode.call_args.kwargs["issuer"], ISSUER)

    def test_rejects_invalid_or_missing_tokens(self):
        with self.assertRaises(GitHubActionsOIDCError):
            verify_github_actions_oidc("")
        key_client = Mock()
        key_client.get_signing_key_from_jwt.side_effect = ValueError("bad token")
        with (
            patch("app.github_actions_oidc._jwks_client", key_client),
            self.assertRaises(GitHubActionsOIDCError),
        ):
            verify_github_actions_oidc("bad-token")


if __name__ == "__main__":
    unittest.main()
