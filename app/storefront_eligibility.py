"""Shared policy for publishing only installable Apphoryn store entries."""


def has_installable_path(
    app: dict,
    verified_artifacts: object,
    supported_targets: object,
    existing_platforms: object = (),
) -> bool:
    """Require a verified installer or an actual supported source-build path."""
    if verified_artifacts or existing_platforms:
        return True
    return (
        app.get("project_kind") in {"application", "developer_tool"}
        and bool(app.get("build_recommended"))
        and bool(supported_targets)
    )
