"""Auto-discovery and deployment of provider-specific rule files."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class RulesSelector:
    """Selects and deploys rule files based on CLI authentication status.

    Template naming in _home_defaults/:
    - RULES.md (static rules for all providers)
    - RULES-claude-only.md (Claude-specific variant)
    - RULES-codex-only.md (Codex-specific variant)
    - RULES-gemini-only.md (Gemini-specific variant)
    - RULES-all-clis.md (multi-provider variant)

    Deployed naming in ~/.ductor/:
    - CLAUDE.md (created if Claude authenticated)
    - AGENTS.md (created if Codex and/or Grok authenticated — both harnesses
      load project rules from AGENTS.md)
    - GEMINI.md (created if Gemini authenticated)
    - All synchronized via sync_rule_files() when multiple exist

    Usage:
        selector = RulesSelector(paths)
        selector.deploy_rules()
    """

    def __init__(self, paths: DuctorPaths) -> None:
        from ductor_bot.cli.auth import AuthStatus, check_all_auth

        self._paths = paths
        # Cache auth status to avoid multiple checks
        auth = check_all_auth()
        claude_result = auth.get("claude")
        codex_result = auth.get("codex")
        gemini_result = auth.get("gemini")
        grok_result = auth.get("grok")

        self._claude_authenticated = (
            claude_result.status == AuthStatus.AUTHENTICATED if claude_result else False
        )
        self._codex_authenticated = (
            codex_result.status == AuthStatus.AUTHENTICATED if codex_result else False
        )
        self._gemini_authenticated = (
            gemini_result.status == AuthStatus.AUTHENTICATED if gemini_result else False
        )
        self._grok_authenticated = (
            grok_result.status == AuthStatus.AUTHENTICATED if grok_result else False
        )

    @property
    def _agents_md_needed(self) -> bool:
        """AGENTS.md is shared by Codex and Grok Build."""
        return self._codex_authenticated or self._grok_authenticated

    @property
    def _authenticated_count(self) -> int:
        """Number of authenticated providers."""
        return sum(
            (
                self._claude_authenticated,
                self._codex_authenticated,
                self._gemini_authenticated,
                self._grok_authenticated,
            )
        )

    def get_variant_suffix(self) -> str:
        """Determine template variant based on CLI authentication status.

        Returns:
            "all-clis" if 2+ providers authenticated
            "claude-only" if only Claude
            "codex-only" if only Codex (or only Grok — same AGENTS.md rules)
            "gemini-only" if only Gemini
            "claude-only" as fallback (no providers authenticated)
        """
        if self._authenticated_count >= 2:
            return "all-clis"
        if self._codex_authenticated or self._grok_authenticated:
            return "codex-only"
        if self._gemini_authenticated:
            return "gemini-only"
        return "claude-only"

    def discover_template_directories(self) -> list[Path]:
        """Find all directories in _home_defaults/ containing RULES templates.

        Returns:
            List of directories containing at least one RULES*.md file.
        """
        seen: set[Path] = set()
        candidates: list[Path] = []

        for path in self._paths.home_defaults.rglob("RULES*.md"):
            parent = path.parent
            if parent not in seen:
                seen.add(parent)
                candidates.append(parent)

        return sorted(candidates)

    def get_best_template(self, directory: Path) -> Path | None:
        """Select best RULES template for directory based on auth status.

        Priority:
        1. Variant-specific template (RULES-{variant}.md)
        2. Static fallback template (RULES.md)

        Args:
            directory: Directory to check for templates

        Returns:
            Path to selected template, or None if no templates found
        """
        variant = self.get_variant_suffix()

        # Priority 1: Variant-specific template
        variant_template = directory / f"RULES-{variant}.md"
        if variant_template.exists():
            logger.debug("Selected variant template: %s", variant_template.name)
            return variant_template

        # Priority 2: Static fallback
        static_template = directory / "RULES.md"
        if static_template.exists():
            logger.debug("Selected static template: %s", static_template.name)
            return static_template

        return None

    def deploy_rules(self) -> None:
        """Auto-discover and deploy all rule files to ~/.ductor/.

        Scans _home_defaults/ for directories with RULES templates, selects
        the best variant for current auth state, and deploys to ~/.ductor/
        as CLAUDE.md, AGENTS.md, and/or GEMINI.md based on authentication.

        Deployment logic:
        - Claude authenticated → CLAUDE.md
        - Codex and/or Grok authenticated → AGENTS.md
        - Gemini authenticated → GEMINI.md
        """
        variant = self.get_variant_suffix()
        logger.info(
            "Deploying rule files (variant: %s, claude=%s, codex=%s, gemini=%s, grok=%s)",
            variant,
            self._claude_authenticated,
            self._codex_authenticated,
            self._gemini_authenticated,
            self._grok_authenticated,
        )

        template_dirs = self.discover_template_directories()
        deployed_count = 0

        for template_dir in template_dirs:
            # Calculate relative path from _home_defaults/
            try:
                rel_path = template_dir.relative_to(self._paths.home_defaults)
            except ValueError:
                logger.warning("Template dir outside home_defaults: %s", template_dir)
                continue

            # Find best template for this directory
            template = self.get_best_template(template_dir)
            if not template:
                logger.debug("No templates found in: %s", rel_path)
                continue

            # Deploy based on auth status
            dst_dir = self._paths.ductor_home / rel_path
            dst_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Deploy CLAUDE.md if Claude is authenticated
                if self._claude_authenticated:
                    claude_dst = dst_dir / "CLAUDE.md"
                    shutil.copy2(template, claude_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> CLAUDE.md", template.name)

                # Deploy AGENTS.md if Codex and/or Grok is authenticated
                if self._agents_md_needed:
                    agents_dst = dst_dir / "AGENTS.md"
                    shutil.copy2(template, agents_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> AGENTS.md", template.name)

                # Deploy GEMINI.md if Gemini is authenticated
                if self._gemini_authenticated:
                    gemini_dst = dst_dir / "GEMINI.md"
                    shutil.copy2(template, gemini_dst)
                    deployed_count += 1
                    logger.debug("Deployed: %s -> GEMINI.md", template.name)

            except OSError:
                logger.exception("Failed to deploy %s", template)

        logger.info(
            "Deployed %d rule files (Claude=%s, Codex=%s, Gemini=%s, Grok=%s)",
            deployed_count,
            self._claude_authenticated,
            self._codex_authenticated,
            self._gemini_authenticated,
            self._grok_authenticated,
        )

        # Cleanup: Remove stale files that don't match current auth status
        self._cleanup_stale_files()

    def _cleanup_stale_files(self) -> None:
        """Remove rule files that don't match current auth status.

        Removes CLAUDE.md, AGENTS.md, or GEMINI.md files for providers
        that are not currently authenticated. AGENTS.md is kept while either
        Codex or Grok is authenticated.
        """
        stale: list[tuple[str, str]] = []
        if not self._claude_authenticated:
            stale.append(("CLAUDE.md", "Claude"))
        if not self._agents_md_needed:
            stale.append(("AGENTS.md", "Codex/Grok"))
        if not self._gemini_authenticated:
            stale.append(("GEMINI.md", "Gemini"))

        for filename, provider_name in stale:
            removed = self._remove_files_by_name(filename)
            if removed > 0:
                logger.info(
                    "Cleaned up %d stale %s files (%s not authenticated)",
                    removed,
                    filename,
                    provider_name,
                )

    def _remove_files_by_name(self, filename: str) -> int:
        """Remove all files with given name in ~/.ductor/.

        Skips files inside ``workspace/cron_tasks/`` — those are user-owned
        rule files created per task and must not be deleted on auth-status
        changes.

        Args:
            filename: Name of files to remove (e.g., "CLAUDE.md" or "AGENTS.md")

        Returns:
            Number of files removed
        """
        cron_tasks_path = self._paths.ductor_home / "workspace" / "cron_tasks"
        removed_count = 0
        for file_path in self._paths.ductor_home.rglob(filename):
            if not file_path.is_file():
                continue
            # Protect user-owned cron task rule files
            if file_path.is_relative_to(cron_tasks_path):
                logger.debug("Skipping user-owned cron task file: %s", file_path)
                continue
            try:
                file_path.unlink()
                removed_count += 1
                logger.debug(
                    "Removed stale file: %s", file_path.relative_to(self._paths.ductor_home)
                )
            except OSError:
                logger.exception("Failed to remove stale file: %s", file_path)

        return removed_count
