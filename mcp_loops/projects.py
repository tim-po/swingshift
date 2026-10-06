"""Projects — the canonical module name for the connected-git-repo model.

Round A of the platform arc renamed the noun **Product → Project**: a *Project*
is the canonical term for a connected git repo (+ its portable coordinates) that
loops and standalone runs TARGET. The pure model itself did not change, so this
module is a THIN re-export of :mod:`mcp_loops.products` under project-named
symbols. Keeping the implementation in ``products.py`` avoids the import churn a
file rename would cause across the stack (``server``, ``products_gather``,
``origin_proto.agent_core`` …) — exactly the zero-breakage constraint of the
rename: symbol clarity is welcome, but never at the cost of a working alias.

Import the canonical names from here::

    from mcp_loops.projects import validate_project, migrate_project

Both the ``*_project`` names below and the original ``*_product`` names in
:mod:`mcp_loops.products` refer to the SAME functions, so old and new callers
resolve identically.
"""

from __future__ import annotations

from mcp_loops import products as _products

# canonical constants
PROJECT_SCHEMA_VERSION = _products.PRODUCT_SCHEMA_VERSION
PRODUCT_SCHEMA_VERSION = _products.PRODUCT_SCHEMA_VERSION      # back-compat alias
PORTABLE_PATH_FIELDS = _products.PORTABLE_PATH_FIELDS
DEFAULT_BRANCH = _products.DEFAULT_BRANCH

# canonical function names (project-spelled) → the shared products.* impls
derive_project_from_source = _products.derive_product_from_source
validate_project = _products.validate_product
migrate_project = _products.migrate_product

# shared, noun-neutral helpers re-exported verbatim
slug = _products.slug
humanize = _products.humanize
looks_like_git_url = _products.looks_like_git_url
is_portable_relpath = _products.is_portable_relpath
resolve_paths = _products.resolve_paths
normalize_git_remote = _products.normalize_git_remote
merge_suggestions = _products.merge_suggestions

# original product-spelled names, re-exported so a single import site works for
# either spelling during the deprecation window.
derive_product_from_source = _products.derive_product_from_source
validate_product = _products.validate_product
migrate_product = _products.migrate_product

__all__ = [
    "PROJECT_SCHEMA_VERSION", "PRODUCT_SCHEMA_VERSION", "PORTABLE_PATH_FIELDS",
    "DEFAULT_BRANCH",
    "derive_project_from_source", "validate_project", "migrate_project",
    "derive_product_from_source", "validate_product", "migrate_product",
    "slug", "humanize", "looks_like_git_url", "is_portable_relpath",
    "resolve_paths", "normalize_git_remote", "merge_suggestions",
]
